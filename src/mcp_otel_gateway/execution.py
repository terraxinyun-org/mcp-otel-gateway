"""MCP execution tools. The caller's OS/microVM supplies the security boundary."""

import asyncio
import json
import os
from pathlib import Path
import signal

import mcp_types as types
from mcp.server import MCPServer


def result(value, error=False):
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(value))],
                                structured_content=value, is_error=error)


def make_execution_server(config):
    root = Path(config.runtime_workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Runtime workspace must be a directory")
    server = MCPServer("txy-execution")

    def workspace_path(value):
        path = (root / value).resolve()
        path.relative_to(root)
        return path

    @server.tool()
    async def run_command(argv: list[str], cwd: str = ".", timeout_seconds: float = 30) -> types.CallToolResult:
        """Run a command and return stdout, stderr, exit code and timeout status.

        argv is an argument list; use ['/bin/sh','-c',SCRIPT] when a shell is needed.
        cwd must be inside the configured workspace. Commands run with the server's
        OS permissions: cwd restriction is NOT a filesystem or network sandbox.
        Output is bounded. Background process groups are terminated after the call.
        """
        if not argv or not argv[0] or not all(isinstance(x, str) and "\0" not in x for x in argv):
            return result({"error": "invalid_argv"}, True)
        if not 0 < timeout_seconds <= min(300, config.timeout_seconds - 1):
            return result({"error": "invalid_timeout"}, True)
        try:
            directory = workspace_path(cwd)
        except (ValueError, OSError):
            return result({"error": "cwd_outside_workspace"}, True)
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT") if k in os.environ}
        proc = None
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"stdout": False, "stderr": False}

        async def drain(stream, name):
            while chunk := await stream.read(4096):
                available = max(0, config.runtime_output_bytes - len(buffers[name]))
                buffers[name].extend(chunk[:available])
                if len(chunk) > available:
                    truncated[name] = True

        def terminate_group():
            if proc:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        try:
            proc = await asyncio.create_subprocess_exec(*argv, cwd=directory, env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True)
            readers = [asyncio.create_task(drain(proc.stdout, "stdout")), asyncio.create_task(drain(proc.stderr, "stderr"))]
            timed_out = False
            try:
                await asyncio.wait_for(proc.wait(), timeout_seconds)
            except TimeoutError:
                timed_out = True
            finally:
                terminate_group()
                await proc.wait()
                try:
                    await asyncio.wait_for(asyncio.gather(*readers), 2)
                except TimeoutError:
                    truncated = {"stdout": True, "stderr": True}
            value = {"argv": argv, "cwd": str(directory), "exit_code": proc.returncode,
                     "timed_out": timed_out,
                     **{k: bytes(v).decode("utf-8", errors="replace") for k, v in buffers.items()},
                     "stdout_truncated": truncated["stdout"], "stderr_truncated": truncated["stderr"]}
            return result(value, timed_out or proc.returncode != 0)
        except asyncio.CancelledError:
            terminate_group()
            if proc:
                await proc.wait()
            raise
        except (ValueError, OSError):
            return result({"error": "command_start_failed"}, True)

    @server.tool()
    def read_file(path: str) -> types.CallToolResult:
        """Read a UTF-8 text file inside the workspace, up to 64 KiB."""
        try:
            target = workspace_path(path)
            with target.open("rb") as stream:
                content = stream.read(65537)
            return result({"path": path, "content": content[:65536].decode("utf-8", errors="replace"),
                           "truncated": len(content) > 65536})
        except (ValueError, OSError):
            return result({"error": "file_unavailable_or_outside_workspace"}, True)

    @server.tool()
    def write_file(path: str, content: str) -> types.CallToolResult:
        """Create or replace a UTF-8 file inside the workspace, up to 128 KiB."""
        data = content.encode("utf-8")
        if len(data) > 131072:
            return result({"error": "content_too_large"}, True)
        try:
            target = workspace_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return result({"path": path, "bytes_written": len(data)})
        except (ValueError, OSError):
            return result({"error": "file_unavailable_or_outside_workspace"}, True)

    return server
