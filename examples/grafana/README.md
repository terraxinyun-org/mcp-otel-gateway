# Grafana Cloud: gateway and runtime evidence

## Browser setup

1. Open your Grafana Cloud stack. In the OpenTelemetry setup guide choose direct
   SDK ingestion. Copy the generated OTLP/HTTP endpoint and authentication settings
   to a private environment file on the machine running the exporter.
2. Use a Cloud Access Policy token with `traces:write`. A Grafana service-account
   token for managing dashboards is a different credential. Never commit either.
3. In Dashboards > New > Import, upload `agent-monitoring.json`, select the existing
   Grafana Cloud Traces / Tempo data source, and import. The dashboard has tables
   for tool calls, failures, and imported runtime evidence. It starts empty.
4. Launch the gateway with the OTLP settings and exercise a tool. Open a trace
   from the results to inspect attributes and captured span events.

Example environment settings (placeholders, not working credentials):

```bash
export OTEL_SERVICE_NAME=mcp-otel-gateway
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT='https://YOUR-OTLP-GATEWAY.grafana.net/otlp'
export OTEL_EXPORTER_OTLP_HEADERS='Authorization=Basic%20BASE64-OF-INSTANCE-ID-COLON-TOKEN'
```

Copy your actual endpoint and instance ID from the **OpenTelemetry** connection
settings; do not substitute a Tempo gRPC hostname or assume its instance ID is
the OTLP instance ID. The SDK appends `/v1/traces` to the base endpoint. To specify
the complete URL use `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` instead. Do not set both
unless you intend the trace-specific setting to override the base URL.

Use a permission-0600 private environment file, outside your repo, or your secret
manager. Source only a file you own and trust into the process starting the gateway.
The same environment settings work with the evidence exporter below.

```bash
.venv/bin/python examples/smoke.py
```

The smoke test checks tool routing, not cloud ingestion. Verify arrival in Grafana.
These steps do not install Alloy. For an existing local Alloy collector, use
`http://127.0.0.1:4318` and configure cloud credentials on Alloy instead.

## Optional MCP arguments and results

Add these fields to your private upstream JSON, then restart its MCP connection:

```json
{"capture_content": true, "capture_max_chars": 8192}
```

This is an addition to your existing upstream configuration, not a complete file.
Arguments and text/structured results become `mcp.tool.arguments` and
`mcp.tool.result` span events. Their JSON payload is `agent_monitor.content.json`.
Binary/image/audio/resource payloads are omitted. Limits may omit large payloads.
Default capture remains off. Actual tool arguments/results are never modified.

Redaction is **best effort**, covering sensitive dictionary keys, common token
formats, authorization strings, emails, and known secret-valued environment
variables. It is not comprehensive PII/secret detection: arbitrary business data,
encoded secrets, unusual formats, and secrets the exporter does not know can remain.
Treat captured content as sensitive even after filtering. No raw capture mode.

## Import existing lab evidence

The lab saves `spans.json` with a `resource` dictionary and `scopes` pairs containing
OTLP-style JSON spans with hexadecimal IDs. The adapter accepts this specific
manifest format, not arbitrary OTLP envelopes or arbitrary harness histories.

```bash
# Validate and count without sending or printing content:
.venv/bin/mcp-otel-evidence --input /private/run/spans.json

# Export process/file/network/model metadata already captured by the lab:
.venv/bin/mcp-otel-evidence --input /private/run/spans.json --send

# Include saved user prompt, final response, and completed harness items:
.venv/bin/mcp-otel-evidence --run-dir /private/run --capture-content --send
```

Run directories may contain `prompt.txt`, `final.txt`, and `timed-events.jsonl`
with records `{received_ns, event}`. Only explicit files in that directory are
read; the adapter does not scan global histories. At most 128 content events are
attached to the root, including recorded commands/output and MCP items. Content
is bounded and filtered before export. It cannot reconstruct missing model calls,
hidden reasoning, browser actions, or unrecorded approvals. Timestamp provenance
is retained: collector receipt timing must not be described as exact tool timing.

Original trace/span IDs, parents, times and statuses are preserved. Standard scalar
attributes are supported; original events/links and non-scalar attributes are not
imported. Sensor scope is preserved; it describes the input source, not independently
verified ownership. A detector alert is not proof of malicious behavior.

This is a manual **post-run export**, not streaming or durable delivery. Exporting
again reuses IDs, but backend deduplication is not guaranteed. When importing old
runs, choose their original time range in Grafana; cloud backends may reject data
outside their accepted time window. No trace timestamp is rewritten to hide age.

## Full Grafana Agent Observability

These traces and content events can be inspected in Tempo. They do **not** populate
the specialized Agent Observability conversation/evaluation UI automatically.
That product requires a separate generation API endpoint, instance ID, and token
with `sigil:write`, plus its SDK integration in the instrumented application.
This package does not fabricate generations from shell events, install that SDK,
configure evaluators/guards, or enforce allow/deny decisions.

Official references:
- https://grafana.com/docs/opentelemetry/grafana-cloud/
- https://grafana.com/docs/grafana-cloud/observe-and-act/agent-observability/configure/sdk/
- https://grafana.com/docs/grafana-cloud/learn-and-build/visualizations/panels-visualizations/visualizations/traces/

The dashboard JSON is a starter template. Its schema/queries are checked locally;
import and rendering in your authenticated Grafana stack require live verification.
