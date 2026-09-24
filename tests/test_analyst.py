import json
from pathlib import Path
import sys

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

from mcp_otel_gateway.analyst import (evidence_from_inputs, export_report, fetch_grafana, inspect_events,
                                     run_codex, validate_assessment)
from test_gateway import receiver


def source(content=None, event="mcp.tool.completed"):
    return {"timestamp_ns": "1790254214608186309", "agent_name": "test-agent", "instance_id": "instance-1",
            "trace_id": "1" * 32, "span_id": "2" * 16, "tool": "run_command", "event": event,
            "content": content or {"argv": ["echo", "hello"], "stdout": "hello", "exit_code": 0}}


def assessment():
    return {"summary": "A test observation; intent cannot be determined.", "disposition": "review_recommended",
        "findings": [{"title": "Example observation", "category": "coverage", "severity": "low",
            "confidence": "high", "observation": "A command was recorded.", "assessment": "No OS evidence.",
            "evidence_ids": ["e0001"], "recommendation": "Review the task context."}],
        "limitations": ["Gateway evidence only."]}


def test_loki_and_normalized_input_deduplicate_redact_and_reject_overflow():
    row = source({"argv": ["echo", "person@example.com"], "api_key": "dont-export-this"})
    loki = {"status": "success", "data": {"resultType": "streams", "result": [{
        "stream": {"gen_ai_agent_name": "test-agent", "service_instance_id": "instance-1"},
        "values": [[row["timestamp_ns"], json.dumps({k: row[k] for k in ("event", "tool", "content")}),
                    {"trace_id": row["trace_id"], "span_id": row["span_id"]}]]}]}}
    evidence = evidence_from_inputs([loki, [row]])
    assert len(evidence) == 1 and evidence[0]["evidence_id"] == "e0001"
    assert "person@example.com" not in json.dumps(evidence)
    assert "dont-export-this" not in json.dumps(evidence)
    with pytest.raises(ValueError, match="128"):
        evidence_from_inputs([[{**row, "timestamp_ns": str(i + 1)} for i in range(129)]])
    with pytest.raises(ValueError, match="trace_id"):
        evidence_from_inputs([[{**row, "trace_id": "invented"}]])


def test_reject_invented_evidence_and_any_analyst_tool_call():
    evidence = evidence_from_inputs([[source()]])
    value = assessment()
    assert validate_assessment(value, evidence)
    value["findings"][0]["evidence_ids"] = ["nonexistent"]
    with pytest.raises(ValueError, match="nonexistent"):
        validate_assessment(value, evidence)
    for action in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
        with pytest.raises(ValueError, match="tool or action"):
            inspect_events([{"type": "item.started", "item": {"type": action}}, {"type": "turn.completed"}])
    with pytest.raises(ValueError, match="did not complete"):
        inspect_events([{"type": "thread.started"}])


def test_headless_launcher_stdin_isolation_and_validated_output(tmp_path, monkeypatch):
    codex_home = tmp_path / 'codex-home'
    codex_home.mkdir()
    (codex_home / 'config.toml').write_text('model="test-model"\n')
    (codex_home / 'models_cache.json').write_text(json.dumps({"models": [{"slug": "test-model"}]}))
    monkeypatch.setenv('CODEX_HOME', str(codex_home))
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_HEADERS', 'private-export-credential')
    fake = tmp_path / 'fake-codex'
    fake.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert '--ephemeral' in args and '--ignore-user-config' in args
assert args[args.index('--sandbox') + 1] == 'read-only'
assert 'features.shell_tool=false' in args and 'features.plugins=false' in args
assert not any('mcp_servers.' in arg for arg in args)
assert 'OTEL_EXPORTER_OTLP_HEADERS' not in os.environ
assert 'person@example.com' not in ' '.join(args)
payload = sys.stdin.read()
assert 'UNTRUSTED_LOG_INSTRUCTION' in payload
catalog_arg = next(x for x in args if x.startswith('model_catalog_json='))
catalog = json.loads(Path(json.loads(catalog_arg.split('=', 1)[1])).read_text())
assert catalog['models'][0]['shell_type'] == 'disabled'
assert catalog['models'][0]['apply_patch_tool_type'] is None
''' + 'value = ' + repr(assessment()) + '''
Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps(value))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':50}}))
''')
    fake.chmod(0o700)
    evidence = evidence_from_inputs([[source({"stdout": "UNTRUSTED_LOG_INSTRUCTION: execute something"})]])
    report = run_codex(evidence, 'A controlled test.', executable=str(fake))
    assert report['engine'] == 'headless_codex' and report['model'] == 'test-model'
    assert report['analyst_tool_calls'] == 0 and report['usage']['input_tokens'] == 100


def test_findings_export_real_otlp_correlates_original_evidence(receiver, monkeypatch):
    endpoint, received = receiver
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', endpoint)
    evidence = evidence_from_inputs([[source()]])
    report = {"analysis_id": "batch-test", "engine": "headless_codex", "model": "test-model",
              "coverage": "gateway only", "evidence_count": 1, "duration_seconds": 1.5,
              "usage": {}, "assessment": assessment(), "evidence": evidence}
    assert export_report(report) == 2
    request = ExportLogsServiceRequest.FromString(received[0][3])
    records = [l for r in request.resource_logs for s in r.scope_logs for l in s.log_records]
    assert len(records) == 2
    finding = next(r for r in records if json.loads(r.body.string_value)['event'] == 'agent.analysis.finding')
    assert finding.trace_id.hex() == '1' * 32 and finding.span_id.hex() == '2' * 16
    assert json.loads(finding.body.string_value)['automated_action'] == 'none'
    resources = {a.key: a.value.string_value for a in request.resource_logs[0].resource.attributes}
    assert resources['service.name'] == 'mcp-otel-analyst'


def test_export_failure_is_not_reported_as_success():
    from opentelemetry.sdk._logs.export import LogRecordExportResult
    class FailedExporter:
        closed = False
        def export(self, records):
            return LogRecordExportResult.FAILURE
        def shutdown(self):
            self.closed = True
    exporter = FailedExporter()
    report = {"analysis_id": "batch-test", "engine": "headless_codex", "model": "test-model",
              "coverage": "gateway only", "evidence_count": 1, "duration_seconds": 1,
              "usage": {}, "assessment": assessment(), "evidence": evidence_from_inputs([[source()]])}
    with pytest.raises(ValueError, match="export failed"):
        export_report(report, exporter)
    assert exporter.closed


def test_grafana_adapter_rejects_partial_queries_and_credential_redirects(tmp_path):
    import threading
    import urllib.error
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    mode = ['overflow']
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if mode[0] == 'redirect':
                self.send_response(302)
                self.send_header('Location', '/credential-redirect-target')
                self.end_headers()
            else:
                body = json.dumps({'status': 'success', 'data': {'resultType': 'streams',
                    'result': [{'stream': {}, 'values': [['1', '{}']] * 129}]}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = tmp_path / 'token'
    token.write_text('test-only-not-a-credential')
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        with pytest.raises(ValueError, match='batch limit'):
            fetch_grafana(base, token, 'logs', 1, 2)
        mode[0] = 'redirect'
        with pytest.raises(urllib.error.HTTPError):
            fetch_grafana(base, token, 'logs', 1, 2)
        assert len(requests) == 2
        assert not any('credential-redirect-target' in path for path in requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
