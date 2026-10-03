# Grafana Cloud: gateway and runtime evidence

## Grafana Cloud quickstart

This produces **four real MCP tool calls** in a temporary workspace: a successful
command, a file write, a file read, and a command that deliberately exits 7.
It sends four OTLP traces and eight start/completion logs to **your** Grafana Cloud
stack. The demo is a scripted MCP client, not an AI agent or a live status probe.
You need Python 3.11+, Git, and a Grafana Cloud stack; no Fly account, Alloy,
Docker, model login, or extra collector is needed.

1. In Grafana Cloud, open your stack's **OpenTelemetry** connection/setup page.
   Choose direct OTLP/HTTP ingestion. Copy the **OTLP endpoint URL** (normally
   `https://...grafana.net/otlp`) and the **OTLP instance ID** from that same
   page. Create a **Cloud Access Policy token** scoped to this stack with
   `traces:write` and `logs:write`. Copy it when shown. The token that manages
   Grafana dashboards is a different credential. If the setup guide offers an
   Alloy install, skip it for this direct SDK demo. Grafana's
   [OTLP setup instructions](https://grafana.com/docs/grafana-cloud/observe-and-act/agent-observability/get-started/grafana-cloud/#open-telemetry-card-on-the-grafana-cloud-portal)
   explain where the OTLP endpoint and instance ID come from; the Agent
   Observability product described on that page is not required here.
2. On any Linux/macOS machine or Ubuntu container with outbound HTTPS, run:

   ```bash
   git clone https://github.com/terraxinyun-org/mcp-otel-gateway.git
   cd mcp-otel-gateway
   python3 -m venv .venv
   .venv/bin/python -m pip install .
   python3 examples/grafana/configure.py
   .venv/bin/python examples/grafana/smoke.py
   ```

   The configure script prompts for the three Grafana values and writes
   `~/.config/mcp-otel-gateway/grafana.json` with mode `0600`, outside the repo.
   It builds the OTLP Basic authorization header for you; neither the token nor
   the header is printed. If that file already exists, the script refuses to
   overwrite it; use `--output /private/new-path.json` and pass the same path to
   `smoke.py --env-file /private/new-path.json` when rotating credentials.
3. In your Grafana stack, open **Explore**, select the **Logs / Loki** data
   source, set the time range to **Last 15 minutes**, and run:

   ```logql
   {service_name="mcp-otel-gateway"}
   ```

   Expand the `mcp.tool.completed` rows. You should see `run_command`,
   `write_file`, and `read_file`, including the successful command output and
   the expected exit code 7. The harmless file content is demo data. Next
   select **Traces / Tempo** in Explore and search for service
   `mcp-otel-gateway` (or TraceQL
   `{ resource.service.name = "mcp-otel-gateway" }`).
4. To see the same data in panels, go to **Dashboards → New → Import**, upload
   [`agent-monitoring.json`](agent-monitoring.json) from this clone, map
   `DS_LOKI` to your Logs/Loki source and `DS_TEMPO` to your Traces/Tempo
   source, then import. Keep **Last 15 minutes** selected. The command, file,
   all-log, tool-call, and failed-call panels should contain the demo. The AI
   analysis panels remain empty until you separately run the optional
   [headless Codex analyst](../analyst/README.md); it is not part of this demo.

If the demo prints successful tool calls but Grafana is empty, wait up to a few
minutes and refresh Explore. Confirm the time range, stack, data source, and
`service_name` spelling. A gateway warning about failed telemetry export usually
means the endpoint, OTLP instance ID, token, or scopes are wrong. The endpoint
must be the **OTLP base URL** ending in `/otlp`; do not use a Tempo gRPC host.
If only one signal arrives, verify that both `traces:write` and `logs:write` are
on the token. Local tool success alone does not prove cloud ingestion.

This first demo has no agent heartbeat and does not install the gateway into
your AI harness. For a real agent, follow [runtime mode](../runtime/README.md)
or the [harness templates](../../COMPATIBILITY.md); only tools that actually
pass through the gateway are observed.

## Manual exporter configuration

If you already have OTLP credentials in your own secret manager, the equivalent
environment variables are:

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

Use a private file outside your repo or your secret manager. The example
`configure.py` script above writes a JSON env-file that the gateway's `--env-file`
option reads directly. A shell-style file must be sourced only if you own and
trust it. The same OTLP settings work with the evidence exporter below.

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

The dashboard JSON is a starter template. Tempo/Loki queries have been verified
through an authenticated Grafana Cloud API with live gateway data. Each new stack
still needs its own data-source selection, credentials and ingestion checks.
