"""Run an isolated Codex test with execution routed through the TXY MCP server.

Requires a signed-in Codex CLI, its model cache, and this package installed.
The generated configuration applies to this invocation, not every Codex session.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib


def toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(k + "=" + toml(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(toml(v) for v in value) + "]"
    if isinstance(value, bool):
        return str(value).lower()
    return json.dumps(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--otel-env-file", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--prompt-file", type=Path, required=True)
    args = parser.parse_args()
    codex_dir = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    config_path = codex_dir / "config.toml"
    user_config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    model = args.model or user_config.get("model")
    models = json.loads((codex_dir / "models_cache.json").read_text())["models"]
    if not model or model not in {m["slug"] for m in models}:
        parser.error("Select a model present in this signed-in CLI's model cache")
    for info in models:
        info.update(shell_type="disabled", apply_patch_tool_type=None,
                    experimental_supported_tools=[], supports_search_tool=False,
                    node_repl_disabled=True)
    workspace = args.workspace.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="txy-mcp-only-") as temporary:
        directory = Path(temporary)
        catalog = directory / "models.json"
        catalog.write_text(json.dumps({"models": models}))
        gateway = directory / "runtime.json"
        gateway.write_text(json.dumps({"transport": "runtime", "runtime_workspace": str(workspace),
                                      "name": "txy-execution", "agent_name": "codex-mcp-only",
                                      "capture_content": True, "export_logs": True}))
        mcp = {"command": sys.executable, "args": ["-m", "mcp_otel_gateway.server", "--config", str(gateway),
               "--env-file", str(args.otel_env_file.resolve())], "required": True,
               "startup_timeout_sec": 20, "tool_timeout_sec": 70,
               "enabled_tools": ["run_command", "read_file", "write_file"],
               "tools": {name: {"approval_mode": "approve"}
                         for name in ("run_command", "read_file", "write_file")}}
        command = ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--strict-config",
                   "--sandbox", "workspace-write", "--skip-git-repo-check", "--json", "--color", "never",
                   "-C", str(workspace), "-m", model, "-c", 'approval_policy="never"',
                   "-c", 'model_reasoning_effort="low"', "-c", "model_catalog_json=" + toml(str(catalog)),
                   "-c", "mcp_servers.txy_execution=" + toml(mcp), "-c", 'web_search="disabled"']
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "browser_use",
                        "browser_use_external", "computer_use", "code_mode", "image_generation",
                        "view_image", "shell_snapshot", "in_app_browser"):
            command += ["-c", f"features.{feature}=false"]
        # Code-mode is the model's tool dispatcher, not an alternate shell. Its
        # host must remain enabled for models which dispatch MCP calls through it.
        command += ["-c", "features.code_mode_host=true", args.prompt_file.read_text()]
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, timeout=300)
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
