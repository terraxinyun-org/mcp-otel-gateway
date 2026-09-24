"""Export a saved lab evidence manifest using standard OTLP/HTTP protobuf.

Input: {resource: {key: value}, scopes: [[scope_name, [OTLP JSON spans...]], ...]}.
Explicit file input only. Does not discover or read agent histories automatically.
"""

import argparse
import json
import logging
import re
from pathlib import Path

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Event
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

from .capture import Capture, SENSITIVE
from .telemetry import make_exporter


def identifier(value, digits):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{%d}" % digits, value) or not int(value, 16):
        raise ValueError("Invalid trace/span identifier")
    return int(value, 16)


def convert_manifest(manifest, capture_content=False):
    capture = Capture()

    def clean_attrs(attrs):
        result = {}
        for key, value in attrs.items():
            if len(result) >= 128:
                break
            if not isinstance(value, (str, bool, int, float)):
                continue
            safe_key = capture.text(str(key))[:256]
            if SENSITIVE.search(str(key)) and not key.startswith("gen_ai.usage."):
                result[safe_key] = "[REDACTED]"
            elif isinstance(value, str):
                # JSON-encoded paths/commands may contain nested credentials.
                try:
                    nested = json.loads(value)
                except (ValueError, TypeError):
                    nested = None
                result[safe_key] = capture.encode(nested)[0] if isinstance(nested, (dict, list)) else capture.text(value)[:8192]
            else:
                result[safe_key] = value
        return result

    resource = Resource(clean_attrs(manifest["resource"]))
    spans = []
    for scope, records in manifest["scopes"]:
        for record in records:
            if len(spans) >= 10000:
                raise ValueError("Manifest exceeds 10000 spans; split it before export")
            tid = identifier(record["traceId"], 32)
            sid = identifier(record["spanId"], 16)
            context = SpanContext(tid, sid, False, TraceFlags(1))
            parent = (SpanContext(tid, identifier(record["parentSpanId"], 16), False, TraceFlags(1))
                      if record.get("parentSpanId") else None)
            start, end = int(record["startTimeUnixNano"]), int(record["endTimeUnixNano"])
            if not 0 < start <= end < 2**64:
                raise ValueError("Invalid span times")
            raw_attrs = {}
            for attr in record.get("attributes", []):
                val = attr["value"]
                for field in ("stringValue", "boolValue", "intValue", "doubleValue"):
                    if field in val:
                        raw_attrs[attr["key"]] = int(val[field]) if field == "intValue" else val[field]
                        break
            attrs = clean_attrs(raw_attrs)
            attrs.update({"agent_monitor.evidence.source": "imported_lab_manifest",
                          "agent_monitor.evidence.scope": capture.text(scope)[:256],
                          "agent_monitor.redaction": "best_effort"})
            # Fixed name; source names often contain full shell commands.
            attrs["agent_monitor.original_span_name"] = capture.text(record["name"])[:512]
            code = record.get("status", {}).get("code", 0)
            status = {0: StatusCode.UNSET, 1: StatusCode.OK, 2: StatusCode.ERROR}[code]
            kind = {0: SpanKind.INTERNAL, 1: SpanKind.INTERNAL, 2: SpanKind.SERVER,
                    3: SpanKind.CLIENT, 4: SpanKind.PRODUCER, 5: SpanKind.CONSUMER}[record.get("kind", 1)]
            events = []
            if capture_content:
                for event in record.get("captured_events", [])[:128]:
                    timestamp = int(event["timestamp"])
                    if not 0 < timestamp < 2**64:
                        raise ValueError("Invalid content timestamp")
                    encoded, truncated = capture.encode(event["body"])
                    events.append(Event("agent.recorded_content", {
                        "agent_monitor.content.json": encoded,
                        "agent_monitor.content.truncated": truncated,
                        "agent_monitor.redaction": "best_effort",
                    }, timestamp=timestamp))
            spans.append(ReadableSpan(
                "agent.runtime.evidence", context=context, parent=parent, resource=resource,
                attributes=attrs, kind=kind, status=Status(status), start_time=start, end_time=end,
                instrumentation_scope=InstrumentationScope(capture.text(scope)[:256]),
                events=events,
            ))
    if not spans:
        raise ValueError("No spans in manifest")
    return spans


def load_run(directory, capture_content=False):
    def read_bounded(path):
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Run file exceeds 32 MiB")
        return path.read_text()

    manifest = json.loads(read_bounded(directory / "spans.json"))
    if not capture_content:
        return manifest
    roots = [record for _, records in manifest["scopes"] for record in records
             if not record.get("parentSpanId")]
    if len(roots) != 1:
        raise ValueError("Content capture requires exactly one run root")
    root = roots[0]
    events = []
    for filename, role, when in (("prompt.txt", "user", root["startTimeUnixNano"]),
                                 ("final.txt", "assistant", root["endTimeUnixNano"])):
        path = directory / filename
        if path.is_file():
            events.append({"timestamp": when, "body": {"source": "saved_harness_output",
                           "role": role, "text": read_bounded(path)}})
    path = directory / "timed-events.jsonl"
    omitted = 0
    if path.is_file():
        for line in read_bounded(path).splitlines():
            if not line.strip():
                continue
            timed = json.loads(line)
            event = timed["event"]
            if event.get("type") != "item.completed":
                continue
            if len(events) >= 128:
                omitted += 1
                continue
            item = event.get("item", {})
            # Only fields needed for recorded conversation and execution evidence.
            body = {key: item[key] for key in (
                "id", "type", "text", "command", "aggregated_output", "exit_code", "status",
                "changes", "server", "tool", "arguments", "result", "error",
            ) if key in item}
            body["source"] = "saved_harness_output"
            events.append({"timestamp": timed["received_ns"], "body": body})
    root["captured_events"] = sorted(events, key=lambda e: int(e["timestamp"]))
    root.setdefault("attributes", []).extend([
        {"key": "agent_monitor.content_capture", "value": {"boolValue": True}},
        {"key": "agent_monitor.content_events_omitted", "value": {"intValue": str(omitted)}},
    ])
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--run-dir", type=Path, help="Saved lab run directory containing spans.json")
    parser.add_argument("--capture-content", action="store_true",
                        help="Include bounded, best-effort-redacted saved harness content")
    parser.add_argument("--send", action="store_true", help="Export; default only validates and counts")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    exporter = None
    try:
        if args.run_dir:
            manifest = load_run(args.run_dir, args.capture_content)
        else:
            if args.input.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("Manifest exceeds 32 MiB")
            manifest = json.loads(args.input.read_text())
        spans = convert_manifest(manifest, args.capture_content)
        if not args.send:
            print(f"Validated {len(spans)} spans; no telemetry sent. Add --send to export.")
            return
        exporter = make_exporter()
        for offset in range(0, len(spans), 80):
            if exporter.export(spans[offset:offset + 80]) != SpanExportResult.SUCCESS:
                raise RuntimeError("Export failed")
        print(f"Exporter reported success for {len(spans)} spans. Verify ingestion in your backend.")
    except Exception:
        parser.exit(1, "Evidence export failed. Check manifest, endpoint, credentials and connectivity; details omitted.\n")
    finally:
        if exporter:
            exporter.shutdown()


if __name__ == "__main__":
    main()
