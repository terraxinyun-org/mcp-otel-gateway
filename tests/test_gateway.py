import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mcp_types as types
import pytest
from mcp import Client, StdioServerParameters
from mcp.server import MCPServer
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mcp_otel_gateway.config import GatewayConfig, child_environment
from mcp_otel_gateway.server import make_server
from mcp_otel_gateway.telemetry import Telemetry

FIXTURE = Path(__file__).with_name("upstream_fixture.py")


@pytest.fixture
def receiver():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers.get("Content-Type"),
                             self.headers.get("Authorization"), data))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", received
    server.shutdown()
    server.server_close()
    thread.join()


def decoded(received):
    for _, _, _, data in received:
        request = ExportTraceServiceRequest.FromString(data)
        for resource in request.resource_spans:
            for scope in resource.scope_spans:
                yield from scope.spans


def attributes(span):
    return {a.key: getattr(a.value, a.value.WhichOneof("value")) for a in span.attributes}


async def run_gateway(tmp_path, receiver, upstream, *, mode="auto", exact_endpoint=False):
    endpoint, received = receiver
    config = tmp_path / "gateway.json"
    config.write_text(json.dumps(upstream))
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env.update(OTEL_EXPORTER_OTLP_ENDPOINT=endpoint,
               OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20collector-secret",
               OTEL_EXPORTER_OTLP_TIMEOUT="1")
    if exact_endpoint:
        env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = endpoint + "/v1/traces"
        env.pop("OTEL_EXPORTER_OTLP_ENDPOINT")
    params = StdioServerParameters(command=sys.executable,
                                  args=["-m", "mcp_otel_gateway.server", "--config", str(config)], env=env)
    secret = "SYNTHETIC_PRIVATE_TOOL_CONTENT_DO_NOT_EXPORT"
    async with Client(params, mode=mode) as client:
        listed = await client.list_tools()
        assert {t.name for t in listed.tools} == {"echo", "reject", "slow"}
        echo = next(t for t in listed.tools if t.name == "echo")
        assert "value" in echo.input_schema["properties"]
        result = await client.call_tool("echo", {"value": secret}, meta={
            "traceparent": "00-11111111111111111111111111111111-2222222222222222-01"
        })
        assert not result.is_error
        assert result.structured_content["value"] == secret
        assert result.structured_content["collector_secret_visible"] is False
        failure = await client.call_tool("reject", {"value": secret})
        assert failure.is_error and failure.content[0].text == secret
    # Shutdown flush must have delivered both spans to an actual HTTP receiver.
    spans = list(decoded(received))
    assert len(spans) == 2
    good = next(s for s in spans if attributes(s)["gen_ai.tool.name"] == "echo")
    bad = next(s for s in spans if attributes(s)["gen_ai.tool.name"] == "reject")
    assert good.trace_id.hex() == "1" * 32
    assert good.parent_span_id.hex() == "2" * 16
    assert good.end_time_unix_nano >= good.start_time_unix_nano > 0
    assert good.status.code == 1 and bad.status.code == 2
    assert attributes(bad)["error.type"] == "tool_error"
    for path, content_type, authorization, wire in received:
        assert path == "/v1/traces"
        assert content_type == "application/x-protobuf"
        assert authorization == "Bearer collector-secret"
        assert secret.encode() not in wire
        assert b"collector-secret" not in wire


async def test_stdio_roundtrip_and_otlp_wire(tmp_path, receiver):
    await run_gateway(tmp_path, receiver, {"command": sys.executable, "args": [str(FIXTURE)]})


async def test_legacy_client_and_exact_trace_endpoint(tmp_path, receiver):
    await run_gateway(tmp_path, receiver, {"command": sys.executable, "args": [str(FIXTURE)]},
                      mode="legacy", exact_endpoint=True)


async def test_streamable_http_upstream(tmp_path, receiver):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([sys.executable, str(FIXTURE), str(port)], stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if proc.poll() is not None:
                pytest.fail("HTTP fixture did not start")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            pytest.fail("HTTP fixture startup timed out")
        await run_gateway(tmp_path, receiver, {"transport": "streamable_http", "url": f"http://127.0.0.1:{port}/mcp"})
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_timeout_and_payload_privacy():
    upstream = MCPServer("slow")

    @upstream.tool()
    async def slow(value: str) -> str:
        await asyncio.sleep(1)
        return value

    @asynccontextmanager
    async def connect():
        async with Client(upstream) as c:
            yield c

    exporter = InMemorySpanExporter()
    telemetry = Telemetry("test", "agent", exporter)
    gateway = make_server(GatewayConfig(command="unused", timeout_seconds=0.05), telemetry, connect)
    try:
        async with Client(gateway) as client:
            result = await client.call_tool("slow", {"value": "private-content"})
            assert result.is_error
        telemetry.provider.force_flush()
        span, = exporter.get_finished_spans()
        assert span.attributes["error.type"] == "timeout"
        assert not span.events  # no exception stack/message payload
        assert "private-content" not in str(span.attributes)
    finally:
        telemetry.close()


async def test_export_failure_does_not_fail_tool():
    class BrokenExporter(SpanExporter):
        def export(self, spans):
            raise RuntimeError("backend unavailable")

    upstream = MCPServer("healthy")

    @upstream.tool()
    def ok() -> str:
        return "ok"

    @asynccontextmanager
    async def connect():
        async with Client(upstream) as c:
            yield c

    telemetry = Telemetry("test", "agent", BrokenExporter())
    try:
        async with Client(make_server(GatewayConfig(command="unused"), telemetry, connect)) as client:
            assert not (await client.call_tool("ok")).is_error
            assert telemetry.provider.force_flush()
            assert not (await client.call_tool("ok")).is_error
    finally:
        telemetry.close()


def test_config_and_secret_isolation(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "secret")
    monkeypatch.setenv("UPSTREAM_KEY", "allowed")
    config = GatewayConfig(command="tool-server", pass_env=["UPSTREAM_KEY"])
    env = child_environment(config)
    assert env["UPSTREAM_KEY"] == "allowed"
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env
    with pytest.raises(ValueError):
        GatewayConfig(transport="streamable_http", url="http://remote.example/mcp")
    with pytest.raises(ValueError):
        GatewayConfig(transport="streamable_http", url="https://user:secret@example.com/mcp")


def test_missing_export_target_is_not_silent(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    with pytest.raises(ValueError):
        Telemetry("test", "agent")


async def test_concurrent_calls_keep_separate_trace_context():
    upstream = MCPServer("concurrent")

    @upstream.tool()
    async def echo(value: str) -> str:
        await asyncio.sleep(0.01)
        return value

    @asynccontextmanager
    async def connect():
        async with Client(upstream) as c:
            yield c

    exporter = InMemorySpanExporter()
    telemetry = Telemetry("test", "agent", exporter)
    try:
        async with Client(make_server(GatewayConfig(command="unused"), telemetry, connect)) as client:
            results = await asyncio.gather(*[
                client.call_tool("echo", {"value": str(i)}, meta={
                    "traceparent": f"00-{i:032x}-{i:016x}-01"
                }) for i in range(1, 6)
            ])
            assert all(not r.is_error for r in results)
        telemetry.provider.force_flush()
        spans = exporter.get_finished_spans()
        assert len(spans) == 5
        assert {s.context.trace_id for s in spans} == set(range(1, 6))
        assert all(s.parent.span_id == s.context.trace_id for s in spans)
    finally:
        telemetry.close()
