import json
import os
import sys

from mcp import Client, StdioServerParameters
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import SpanExportResult

from mcp_otel_gateway.capture import Capture
from mcp_otel_gateway.evidence import convert_manifest, load_run
from test_gateway import FIXTURE, attributes, decoded, receiver


async def test_opt_in_capture_redacts_wire_preserves_result(tmp_path, receiver):
    endpoint, received = receiver
    config = tmp_path / "gateway.json"
    config.write_text(json.dumps({"command": sys.executable, "args": [str(FIXTURE)],
                                  "capture_content": True}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
    env["PRIVATE_ACCESS_TOKEN"] = "synthetic-exact-environment-secret"
    payload = 'deploy release-42 for person@example.com Bearer synthetic-bearer-value synthetic-exact-environment-secret'
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "mcp_otel_gateway.server", "--config", str(config)], env=env)
    async with Client(params) as client:
        result = await client.call_tool("echo", {"value": payload})
        assert result.structured_content["value"] == payload
        failure = await client.call_tool("reject", {"value": payload})
        assert failure.content[0].text == payload and failure.is_error
    spans = list(decoded(received))
    assert len(spans) == 2
    for span in spans:
        assert attributes(span)["agent_monitor.content_capture"] is True
        assert {e.name for e in span.events} == {"mcp.tool.arguments", "mcp.tool.result"}
        for event in span.events:
            content = attributes(event)["agent_monitor.content.json"]
            assert "release-42" in content
            json.loads(content)
    wire = b"".join(x[3] for x in received)
    for value in (b"person@example.com", b"synthetic-bearer-value", b"synthetic-exact-environment-secret"):
        assert value not in wire


def test_capture_limits_sensitive_keys_and_binary():
    capture = Capture(512)
    value = {"password": "unrecognizable-secret", "nested": {"api_key": "another-secret"},
             "blob": "binary-payload", "text": "useful", "items": list(range(1000))}
    encoded, truncated = capture.encode(value)
    assert truncated and len(encoded) <= 512
    assert "unrecognizable-secret" not in encoded and "binary-payload" not in encoded
    json.loads(encoded)
    encoded, truncated = capture.encode("a" * 70000)
    assert truncated and "OMITTED" in encoded
    assert capture.encode({"ok": 7}) == ('{"ok":7}', False)
    assert 'hidden-value' not in capture.text('{"password":"hidden-value"}')


def test_lab_evidence_export_preserves_correlation_and_redacts(receiver):
    endpoint, received = receiver
    start = 1750000000000000000
    manifest = {"resource": {"service.name": "agent-lab", "api_key": "resource-private"},
                "scopes": [["lab/kernel", [{
                    "traceId": "1" * 32, "spanId": "2" * 16, "parentSpanId": "3" * 16,
                    "name": "exec curl token=secret-from-command", "kind": 1,
                    "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + 50),
                    "status": {"code": 2}, "attributes": [
                        {"key": "proc.cmdline", "value": {"stringValue": "curl token=secret-from-command"}},
                        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "27"}},
                        {"key": "nested", "value": {"stringValue": '{"password":"private-nested"}'}},
                    ]}]]]}
    spans = convert_manifest(manifest)
    exporter = OTLPSpanExporter(endpoint=endpoint + "/v1/traces")
    try:
        assert exporter.export(spans) == SpanExportResult.SUCCESS
    finally:
        exporter.shutdown()
    span, = list(decoded(received))
    assert span.trace_id.hex() == "1" * 32 and span.span_id.hex() == "2" * 16
    assert span.parent_span_id.hex() == "3" * 16
    assert span.start_time_unix_nano == start and span.end_time_unix_nano == start + 50
    assert span.status.code == 2
    assert attributes(span)["gen_ai.usage.input_tokens"] == 27
    for forbidden in (b"secret-from-command", b"resource-private", b"private-nested"):
        assert forbidden not in received[0][3]


def test_saved_run_content_is_opt_in_bounded_and_redacted(tmp_path):
    start = 1750000000000000000
    root = {"traceId": "1" * 32, "spanId": "2" * 16, "name": "run", "kind": 1,
            "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + 1000)}
    (tmp_path / "spans.json").write_text(json.dumps({"resource": {"service.name": "lab"},
                                                   "scopes": [["lab", [root]]]}))
    (tmp_path / "prompt.txt").write_text("check inventory for person@example.com")
    (tmp_path / "final.txt").write_text("Inventory checked")
    timed = [{"received_ns": start + i + 1, "event": {"type": "item.completed", "item": {
        "id": str(i), "type": "command_execution", "command": "check-inventory",
        "aggregated_output": "token=do-not-export inventory passed", "exit_code": 0,
    }}} for i in range(150)]
    (tmp_path / "timed-events.jsonl").write_text("\n".join(json.dumps(x) for x in timed))
    span, = convert_manifest(load_run(tmp_path))
    assert not span.events
    span, = convert_manifest(load_run(tmp_path, True), True)
    assert len(span.events) == 128
    assert span.attributes["agent_monitor.content_events_omitted"] == 24
    content = str([dict(e.attributes) for e in span.events])
    assert "inventory" in content and "Inventory checked" in content
    assert "person@example.com" not in content and "do-not-export" not in content
    assert span.events[0].timestamp == start
    assert span.events[-1].timestamp == start + 1000
