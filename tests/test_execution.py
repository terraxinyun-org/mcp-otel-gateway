import asyncio
import json
import os
import sys

from mcp import Client, StdioServerParameters
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

from mcp_otel_gateway.config import GatewayConfig
from mcp_otel_gateway.execution import make_execution_server
from test_gateway import receiver


async def test_execution_commands_failures_limits_and_file_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "private-export-credential")
    config = GatewayConfig(transport="runtime", runtime_workspace=str(tmp_path), runtime_output_bytes=1024)
    outside = tmp_path.parent / "outside-runtime-test.txt"
    outside.write_text("outside")
    (tmp_path / "escape").symlink_to(outside)
    async with Client(make_execution_server(config)) as client:
        assert {t.name for t in (await client.list_tools()).tools} == {"run_command", "read_file", "write_file"}
        call = await client.call_tool("run_command", {"argv": [sys.executable, "-c", "import os; print('hello'); print('OTEL_EXPORTER_OTLP_HEADERS' in os.environ)"]})
        assert call.structured_content["stdout"] == "hello\nFalse\n"
        failure = await client.call_tool("run_command", {"argv": [sys.executable, "-c", "import sys; print('failure',file=sys.stderr); sys.exit(7)"]})
        assert failure.is_error and failure.structured_content["exit_code"] == 7
        assert failure.structured_content["stderr"] == "failure\n"
        large = await client.call_tool("run_command", {"argv": [sys.executable, "-c", "print('x'*10000)"]})
        assert large.structured_content["stdout_truncated"]
        assert len(large.structured_content["stdout"]) == 1024
        timeout = await client.call_tool("run_command", {"argv": [sys.executable, "-c", "import time; time.sleep(10)"], "timeout_seconds": 0.1})
        assert timeout.is_error and timeout.structured_content["timed_out"]
        assert (await client.call_tool("run_command", {"argv": ["pwd"], "cwd": ".."})).is_error
        assert not (await client.call_tool("write_file", {"path": "nested/demo.txt", "content": "observed"})).is_error
        assert (await client.call_tool("read_file", {"path": "nested/demo.txt"})).structured_content["content"] == "observed"
        assert (await client.call_tool("read_file", {"path": "escape"})).is_error
        assert (await client.call_tool("write_file", {"path": "../outside-runtime-test.txt", "content": "changed"})).is_error
    assert outside.read_text() == "outside"


async def test_mcp_command_produces_correlated_otlp_logs_and_traces(tmp_path, receiver):
    endpoint, received = receiver
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps({"transport": "runtime", "runtime_workspace": str(tmp_path),
                                  "capture_content": True, "export_logs": True}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
    async with Client(StdioServerParameters(command=sys.executable,
        args=["-m", "mcp_otel_gateway.server", "--config", str(config)], env=env)) as client:
        result = await client.call_tool("run_command", {"argv": [sys.executable, "-c", "print('hello person@example.com')"]})
        assert result.structured_content["stdout"] == "hello person@example.com\n"
    logs = []
    trace_ids = set()
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    for path, content_type, _, wire in received:
        assert b"person@example.com" not in wire
        if path == "/v1/logs":
            request = ExportLogsServiceRequest.FromString(wire)
            logs.extend(log for r in request.resource_logs for s in r.scope_logs for log in s.log_records)
        elif path == "/v1/traces":
            request = ExportTraceServiceRequest.FromString(wire)
            trace_ids.update(span.trace_id for r in request.resource_spans for s in r.scope_spans for span in s.spans)
    assert len(logs) == 2 and len(trace_ids) == 1
    assert {l.trace_id for l in logs} == trace_ids
    assert {json.loads(l.body.string_value)["event"] for l in logs} == {"mcp.tool.started", "mcp.tool.completed"}
    assert "hello" in logs[-1].body.string_value
