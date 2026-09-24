"""Headless Codex review of captured MCP evidence; no execution or remediation tools."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import tomllib
from typing import Literal
import urllib.parse
import urllib.request

from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .capture import Capture
from .config import validate_url

PROMPT_VERSION = "codex-activity-v2"
MAX_BYTES = 4 * 1024 * 1024
EVENTS = {"mcp.tool.started", "mcp.tool.completed", "mcp.tool.failed",
          "mcp.tool.timeout", "mcp.tool.cancelled"}
INSTRUCTIONS = (Path(__file__).parent / "prompts" / "activity_analyst.txt").read_text()


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    category: Literal["security", "operational", "coverage"]
    activity_type: Literal["credential_access", "data_transfer", "audit_tampering", "monitoring_changes",
                           "encoded_execution", "workspace_boundary", "task_deviation", "prompt_injection",
                           "execution_failure", "visibility_gap", "other"]
    observation: str = Field(min_length=1, max_length=1800)
    assessment: str = Field(min_length=1, max_length=1800)
    evidence_ids: list[str] = Field(min_length=1, max_length=16)
    recommendation: str = Field(min_length=1, max_length=1200)


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=2500)
    disposition: Literal["no_clear_concern", "review_recommended", "insufficient_evidence"]
    findings: list[Finding] = Field(max_length=12)
    limitations: list[str] = Field(min_length=1, max_length=12)


def read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Input exceeds 4 MiB; split it into smaller batches")
    return json.loads(raw)


def evidence_from_inputs(documents):
    """Accept Loki query_range responses or backend-neutral normalized event lists."""
    rows = []
    for document in documents:
        if isinstance(document, list):
            rows.extend(document)
            continue
        if document.get("status") != "success" or document.get("data", {}).get("resultType") != "streams":
            raise ValueError("Expected a successful Loki stream response or normalized event list")
        for stream in document["data"]["result"]:
            for value in stream["values"]:
                labels = {**stream["stream"], **(value[2] if len(value) > 2 else {})}
                body = json.loads(value[1])
                if not isinstance(body, dict) or body.get("event") not in EVENTS:
                    continue
                rows.append({"timestamp_ns": value[0], "agent_name": labels.get("gen_ai_agent_name", "unknown"),
                    "instance_id": labels.get("service_instance_id", "unknown"),
                    "trace_id": labels.get("trace_id", ""), "span_id": labels.get("span_id", ""),
                    "event": body["event"], "tool": body.get("tool", "unknown"),
                    "content": body.get("content"),
                    "capture_truncated": labels.get("agent_monitor_content_truncated") == "true"})
    capture = Capture(16384)
    unique = {}
    for row in rows:
        if row.get("event") not in EVENTS:
            continue
        for key, length in (("trace_id", 32), ("span_id", 16)):
            if not re.fullmatch(r"[0-9a-fA-F]{%d}" % length, str(row.get(key, ""))) or not int(row[key], 16):
                raise ValueError("Source evidence requires valid trace_id and span_id")
        timestamp = int(row["timestamp_ns"])
        if not 0 < timestamp < 2**64:
            raise ValueError("Invalid evidence timestamp")
        encoded, truncated = capture.encode(row.get("content"))
        clean = {"timestamp_ns": str(timestamp),
                 "agent_name": capture.text(str(row.get("agent_name", "unknown")))[:256],
                 "instance_id": capture.text(str(row.get("instance_id", "unknown")))[:256],
                 "trace_id": row["trace_id"].lower(), "span_id": row["span_id"].lower(),
                 "event": row["event"], "tool": capture.text(str(row.get("tool", "unknown")))[:256],
                 "content": json.loads(encoded),
                 "capture_truncated": bool(row.get("capture_truncated")) or truncated}
        key = json.dumps(clean, sort_keys=True)
        unique[key] = clean
    evidence = sorted(unique.values(), key=lambda r: (int(r["timestamp_ns"]), r["trace_id"], r["event"]))
    if not 1 <= len(evidence) <= 128:
        raise ValueError("Analysis requires 1..128 evidence events; split larger batches")
    for index, row in enumerate(evidence, 1):
        row["evidence_id"] = f"e{index:04d}"
    if len(json.dumps(evidence).encode()) > 256000:
        raise ValueError("Evidence exceeds 256 KB model input limit; split the batch")
    return evidence


def restricted_catalog(codex_dir, selected=None):
    path = codex_dir / "config.toml"
    config = tomllib.loads(path.read_text()) if path.exists() else {}
    model = selected or config.get("model")
    models = read_json(codex_dir / "models_cache.json")["models"]
    if not model or model not in {m["slug"] for m in models}:
        raise ValueError("Select a model from this signed-in Codex CLI's model cache")
    for model_info in models:
        model_info.update(shell_type="disabled", apply_patch_tool_type=None,
            experimental_supported_tools=[], supports_search_tool=False, node_repl_disabled=True)
    return model, {"models": models}


def validate_assessment(value, evidence):
    assessment = Assessment.model_validate(value)
    known = {e["evidence_id"] for e in evidence}
    for finding in assessment.findings:
        if not set(finding.evidence_ids) <= known:
            raise ValueError("Analyst cited nonexistent evidence; report rejected")
    return assessment


def inspect_events(events):
    usage = None
    completed = False
    for event in events:
        item = event.get("item", {})
        if item and item.get("type") not in {"agent_message", "reasoning"}:
            raise ValueError("Unexpected analyst tool or action event; report rejected")
        if event.get("type") in {"error", "turn.failed"}:
            raise ValueError("Codex analysis failed; no findings published")
        if event.get("type") == "turn.completed":
            completed = True
            usage = event.get("usage")
    if not completed:
        raise ValueError("Codex did not complete an analysis turn")
    return usage


def run_codex(evidence, operator_context="", *, model=None, timeout=180, executable="codex"):
    codex_dir = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
    model, catalog = restricted_catalog(codex_dir, model)
    if len(operator_context) > 8192:
        raise ValueError("Operator context exceeds 8192 characters")
    context = Capture(8192).text(operator_context)
    payload = {"operator_context": context, "coverage": "MCP gateway observations only; no independent OS telemetry",
               "evidence": evidence}
    batch_id = hashlib.sha256(json.dumps([PROMPT_VERSION, model, payload], sort_keys=True).encode()).hexdigest()[:24]
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="txy-codex-analyst-") as temporary:
        directory = Path(temporary)
        (directory / "models.json").write_text(json.dumps(catalog))
        (directory / "schema.json").write_text(json.dumps(Assessment.model_json_schema()))
        output = directory / "assessment.json"
        command = [executable, "exec", "--ignore-user-config", "--ignore-rules", "--strict-config",
            "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "--json", "--color", "never",
            "-C", str(directory), "-m", model, "--output-schema", str(directory / "schema.json"),
            "--output-last-message", str(output), "-c", 'approval_policy="never"',
            "-c", 'model_reasoning_effort="medium"', "-c", 'web_search="disabled"',
            "-c", "developer_instructions=" + json.dumps(INSTRUCTIONS),
            "-c", "model_catalog_json=" + json.dumps(str(directory / "models.json"))]
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "browser_use",
                        "browser_use_external", "computer_use", "code_mode", "image_generation",
                        "view_image", "shell_snapshot", "in_app_browser"):
            command += ["-c", f"features.{feature}=false"]
        command += ["-c", "features.code_mode_host=true", "-"]
        # Pass evidence on stdin, never in process argv. Do not inherit OTLP/Grafana secrets.
        env = {k: v for k, v in os.environ.items() if k in {
            "PATH", "HOME", "CODEX_HOME", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY",
            "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}}
        with (directory / "events.jsonl").open("w+") as events_file, (directory / "stderr.txt").open("w+") as err:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=events_file, stderr=err,
                                    env=env, text=True, start_new_session=True)
            try:
                proc.communicate("Analyse this evidence batch. Return only the required JSON.\n" + json.dumps(payload), timeout=timeout)
            except BaseException:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                raise
            if proc.returncode:
                raise ValueError("Headless Codex exited unsuccessfully; check CLI login and model availability")
            if events_file.tell() > MAX_BYTES:
                raise ValueError("Analyst event output exceeded 4 MiB")
            events_file.seek(0)
            usage = inspect_events([json.loads(line) for line in events_file if line.strip()])
        assessment = validate_assessment(read_json(output), evidence)
    # Redact model-generated text again before saving or exporting it.
    safe, truncated = Capture(65536).encode(assessment.model_dump())
    if truncated:
        raise ValueError("Analyst response exceeds capture bounds")
    assessment = validate_assessment(json.loads(safe), evidence)
    return {"analysis_id": batch_id, "engine": "headless_codex", "model": model,
        "prompt_version": PROMPT_VERSION, "analysed_at_ns": str(time.time_ns()),
        "duration_seconds": round(time.monotonic() - started, 2), "usage": usage,
        "analyst_tool_calls": 0, "coverage": payload["coverage"],
        "evidence_count": len(evidence), "assessment": assessment.model_dump(), "evidence": evidence}


def export_report(report, exporter=None):
    from opentelemetry._logs import LogRecord, SeverityNumber
    from opentelemetry.sdk._logs import ReadableLogRecord
    from opentelemetry.sdk._logs.export import LogRecordExportResult
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    if exporter is None:
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            raise ValueError("OTLP log export endpoint required")
        validate_url(endpoint)
        if os.getenv("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")) != "http/protobuf":
            raise ValueError("Analyst logs require http/protobuf")
        exporter = OTLPLogExporter()
    resource = Resource.create({"service.name": "mcp-otel-analyst", "service.version": __version__})
    evidence = {e["evidence_id"]: e for e in report["evidence"]}
    records = []
    now = time.time_ns()
    assessment = report["assessment"]
    bodies = [{"event": "agent.analysis.summary", "summary": assessment["summary"],
               "disposition": assessment["disposition"], "limitations": assessment["limitations"],
               "finding_count": len(assessment["findings"]), "evidence_count": report["evidence_count"],
               "duration_seconds": report["duration_seconds"],
               "model_usage": {key.removesuffix("_tokens"): value for key, value in (report["usage"] or {}).items()}}]
    for index, finding in enumerate(assessment["findings"], 1):
        sources = [evidence[key] for key in finding["evidence_ids"]]
        bodies.append({"event": "agent.analysis.finding", "finding_id": f"{report['analysis_id']}-{index}",
            **finding, "source_trace_ids": sorted({e["trace_id"] for e in sources}),
            "source_agents": sorted({e["agent_name"] for e in sources}),
            "evidence": sources})
    for body in bodies:
        body.update(analysis_id=report["analysis_id"], engine=report["engine"], model=report["model"],
                    prompt_version=report["prompt_version"], coverage=report["coverage"], automated_action="none")
        encoded, truncated = Capture(65536).encode(body)
        if truncated:
            # Keep findings usable if full source payloads do not fit a single log.
            body["evidence"] = [{k: e[k] for k in ("evidence_id", "timestamp_ns", "trace_id", "span_id", "tool", "event")}
                                for e in body.get("evidence", [])]
            encoded, truncated = Capture(65536).encode(body)
        if truncated:
            raise ValueError("Finding is too large to export")
        source = evidence[body["evidence_ids"][0]] if body.get("evidence_ids") else None
        record = LogRecord(timestamp=now, observed_timestamp=now, body=encoded, severity_number=SeverityNumber.INFO,
            trace_id=int(source["trace_id"], 16) if source else 0,
            span_id=int(source["span_id"], 16) if source else 0,
            attributes={"event.name": body["event"], "agent_monitor.analysis.id": report["analysis_id"],
                "agent_monitor.analysis.engine": "headless_codex", "agent_monitor.evidence.source": "ai_assessment",
                "agent_monitor.analysis.prompt_version": report["prompt_version"],
                "agent_monitor.finding.activity_type": body.get("activity_type", "summary"),
                "agent_monitor.redaction": "best_effort"})
        records.append(ReadableLogRecord(record, resource, InstrumentationScope("txy.mcp.analyst", __version__)))
    try:
        if exporter.export(records) != LogRecordExportResult.SUCCESS:
            raise ValueError("Finding export failed; saved report can be retried")
    finally:
        exporter.shutdown()
    return len(records)


def fetch_grafana(base_url, token_file, datasource, start_ns, end_ns, agent=None):
    validate_url(base_url)
    if urllib.parse.urlsplit(base_url).query:
        raise ValueError("Grafana base URL must not contain a query")
    query = '{service_name="mcp-otel-gateway"}'
    if agent:
        query += " | gen_ai_agent_name=" + json.dumps(agent)
    params = urllib.parse.urlencode({"query": query, "start": str(start_ns), "end": str(end_ns),
                                    "direction": "forward", "limit": "129"})
    url = base_url.rstrip('/') + '/api/datasources/proxy/uid/' + urllib.parse.quote(datasource, safe='') + '/loki/api/v1/query_range?' + params
    # Do not forward a Grafana credential through redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + Path(token_file).read_text().strip()})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        content = response.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("Grafana response too large; narrow the time range")
    document = json.loads(content)
    if sum(len(s["values"]) for s in document.get("data", {}).get("result", [])) >= 129:
        raise ValueError("Grafana query hit the batch limit; narrow the range or select an agent")
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, nargs='+', help="Loki JSON responses or normalized event JSON arrays")
    source.add_argument("--grafana-url", help="Fetch a bounded snapshot using Grafana's Loki proxy")
    parser.add_argument("--grafana-token-file", type=Path)
    parser.add_argument("--datasource", default="grafanacloud-logs")
    parser.add_argument("--agent", help="Exact source agent label for Grafana query")
    parser.add_argument("--lookback-minutes", type=int, default=15)
    parser.add_argument("--context-file", type=Path, help="Operator-provided task/authorization context")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--env-file", type=Path, help="Private JSON containing only OTEL_ settings")
    parser.add_argument("--send", action="store_true", help="Export AI findings as OTLP logs")
    args = parser.parse_args()
    if not 1 <= args.lookback_minutes <= 1440 or not 10 <= args.timeout <= 600:
        parser.error("lookback must be 1..1440 minutes and timeout 10..600 seconds")
    if args.output.exists() or not args.output.parent.is_dir():
        parser.error("Output must be a new file in an existing directory")
    try:
        if args.env_file:
            for key, value in read_json(args.env_file).items():
                if key.startswith("OTEL_") and isinstance(value, str):
                    os.environ[key] = value
        if args.input:
            documents = [read_json(path) for path in args.input]
        else:
            if not args.grafana_token_file:
                parser.error("--grafana-token-file is required with --grafana-url")
            end = time.time_ns()
            documents = [fetch_grafana(args.grafana_url, args.grafana_token_file, args.datasource,
                         end - args.lookback_minutes * 60 * 10**9, end, args.agent)]
        evidence = evidence_from_inputs(documents)
        context = "Task authorization was not provided."
        if args.context_file:
            with args.context_file.open() as stream:
                context = stream.read(8193)
        report = run_codex(evidence, context, model=args.model, timeout=args.timeout)
        # Exclusive creation avoids silently overwriting an earlier analysis.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(report, stream, indent=2)
        exported = export_report(report) if args.send else 0
        print(json.dumps({"analysis_id": report["analysis_id"], "model": report["model"],
            "evidence_count": report["evidence_count"], "findings": len(report["assessment"]["findings"]),
            "duration_seconds": report["duration_seconds"], "usage": report["usage"],
            "analyst_tool_calls": report["analyst_tool_calls"], "exported_logs": exported,
            "report": str(args.output)}))
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        # Pydantic and HTTP errors may contain untrusted evidence or URLs; keep diagnostics bounded.
        print(json.dumps({"error": type(exc).__name__, "message": "Analysis or export failed; no automatic action taken."}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
