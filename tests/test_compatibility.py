"""Wire tests do not depend on a proprietary harness or model subscription."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_otel_gateway.http import make_http_app
from test_gateway import receiver, decoded, attributes

FIXTURE = Path(__file__).with_name("upstream_fixture.py")
TOKEN = "synthetic-gateway-auth-token-for-tests-only"


def config_and_env(tmp_path, receiver):
    config = tmp_path / "upstream.json"
    config.write_text(json.dumps({"command": sys.executable, "args": [str(FIXTURE)]}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env.update(OTEL_EXPORTER_OTLP_ENDPOINT=receiver[0], OTEL_EXPORTER_OTLP_TIMEOUT="1",
               MCP_GATEWAY_AUTH_TOKEN=TOKEN)
    return config, env


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
async def test_raw_jsonrpc_client_versions(tmp_path, receiver, version):
    config, env = config_and_env(tmp_path, receiver)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "mcp_otel_gateway.server", "--config", str(config),
        env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def send(message):
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        await proc.stdin.drain()

    async def receive():
        return json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))

    try:
        await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": version, "capabilities": {},
            "clientInfo": {"name": "independent-test-harness", "version": "1.0"},
        }})
        initialized = await receive()
        assert initialized["result"]["protocolVersion"] == version
        assert "tools" in initialized["result"]["capabilities"]
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert "echo" in [t["name"] for t in (await receive())["result"]["tools"]]
        await send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "echo", "arguments": {"value": "wire-test"},
        }})
        result = (await receive())["result"]
        assert not result.get("isError", False)
        # Older protocol versions may represent structured output as text.
        assert "wire-test" in json.dumps(result["content"])
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except TimeoutError:
            proc.kill()
            await proc.wait()
    assert any(attributes(s).get("gen_ai.tool.name") == "echo" for s in decoded(receiver[1]))


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_authenticated_http_downstream(tmp_path, receiver, mode):
    config, env = config_and_env(tmp_path, receiver)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([
        sys.executable, "-m", "mcp_otel_gateway.server", "--config", str(config),
        "--transport", "streamable-http", "--port", str(port),
    ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        async with httpx2.AsyncClient(timeout=2) as unauthenticated:
            for _ in range(100):
                if proc.poll() is not None:
                    pytest.fail("Gateway exited before HTTP startup")
                try:
                    response = await unauthenticated.get(url)
                    break
                except httpx2.ConnectError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("Gateway HTTP startup timed out")
            assert response.status_code == 401
            assert (await unauthenticated.post(url, headers={"Authorization": "Bearer wrong"})).status_code == 401
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + TOKEN}) as http:
            async with Client(streamable_http_client(url, http_client=http), mode=mode) as client:
                assert "echo" in [t.name for t in (await client.list_tools()).tools]
                result = await client.call_tool("echo", {"value": "http-test"})
                assert not result.is_error
                assert result.structured_content == {"value": "http-test", "collector_secret_visible": False}
            # An authenticated browser on an untrusted origin still cannot use it.
            response = await http.post(url, headers={"Origin": "https://attacker.example"}, json={})
            assert response.status_code == 403
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    assert any(attributes(s).get("gen_ai.tool.name") == "echo" for s in decoded(receiver[1]))


def test_http_requires_token_and_valid_public_origin():
    from mcp.server import Server
    with pytest.raises(ValueError):
        make_http_app(Server("test"), "")
    with pytest.raises(ValueError):
        make_http_app(Server("test"), TOKEN, "https://user:password@example.com")
