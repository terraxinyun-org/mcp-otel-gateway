# Validation — 2026-09-24

## Version 0.4.0 — MCP execution and logs

- Twenty-one tests passed on Python 3.12; dependency checks and wheel build passed.
- Added in-process MCP tools for commands, text file reads and writes. No separate
  monitoring daemon or file-tail collector was deployed.
- Command tests cover real stdout/stderr, nonzero exit codes, timeout, bounded
  output, environment credential omission, and workspace path/symlink rejection
  for file tools. Command cwd restrictions are not an OS sandbox.
- Real subprocess MCP calls exported both OTLP protobuf logs and traces to an HTTP
  receiver; start/completion logs share their tool span's trace ID. Email content
  remained in the tool result but was absent from exported log/trace bytes.
- A live Codex CLI 0.156.1 run in an Ubuntu container used the restricted launcher
  and performed exactly five MCP calls: three commands, one write, one read.
  All ordinary operations succeeded; a deliberate `exit 7` was a tool error.
  No built-in `command_execution` or `file_change` events occurred in that run.
- The launcher disables shell/browser/apps/plugins/subagents, removes built-in
  apply-patch from its startup model catalog, and requires the execution MCP.
  It preapproves only the three allowlisted tools for its unattended invocation.
  This profile is version-specific, not global or tamper-proof enforcement.
- Independently queried Grafana Cloud Loki through the service-account API and
  retrieved all ten start/completion logs from the real agent run, including
  stdout/stderr, file operations, exit status and correlation IDs. Tempo ingestion
  and trace lookup had also been verified through the API.
- Updated the live dashboard with command and file log panels. No Fly image,
  global harness configuration, or real application workload was changed.

## Version 0.3.0 — optional content and saved runtime evidence

- Nineteen tests passed on Python 3.12, retaining the previous compatibility tests.
- Real MCP subprocess calls with capture enabled exported argument/result events
  over OTLP/HTTP protobuf. Text, structured content and tool-error results reached
  the caller unchanged. Email, bearer credential and an exact known environment
  secret were absent from the captured wire payload.
- Tests cover capture limits, sensitive nested keys, JSON-encoded credentials,
  and opaque payload omission. These are representative cases, not proof of
  comprehensive PII/secret filtering.
- Saved lab manifests exported through the official OTLP exporter with trace IDs,
  span IDs, parentage, timestamps, status and numeric token usage preserved.
  Commands and nested credential fields were filtered in the exported bytes.
- Saved prompt/final response and timed harness item capture was checked for
  explicit opt-in, event limits, original timestamps and redaction.
- Grafana dashboard JSON was generated locally. No authenticated Grafana import,
  live cloud ingestion, Fly deployment, or live harness reconfiguration was done.
- The evidence adapter is an explicit post-run import. Agent Observability's
  generation API, evaluation, guards and automatic runtime collection are not
  connected by this release.

## Version 0.2.0 — harness interoperability

- Fifteen tests passed on Python 3.12, including the original eight tests.
- Independent raw JSON-RPC clients negotiated protocol versions `2024-11-05`,
  `2025-03-26`, `2025-06-18`, and `2025-11-25`, listed tools, called a tool and
  produced OTLP telemetry through the gateway.
- Modern and legacy SDK clients connected through the authenticated downstream
  Streamable HTTP transport and successfully called the stdio upstream.
- Missing/incorrect authorization and an untrusted browser Origin were rejected.
- Graceful HTTP termination flushed the last buffered span to the test receiver.
- Six client templates were syntax-checked. Their formats were checked against
  official docs; this does not constitute six live harness/model tests.
- Wheel build and installed dependency checks passed. No live agent settings or
  production endpoints were modified.

## Version 0.1.0 — initial export validation

Version: 0.1.0. Python 3.12, MCP SDK 2.2.0, OpenTelemetry SDK/exporter 1.44.0.

- Eight tests passed. Real subprocess tests exercised MCP stdio and Streamable
  HTTP upstreams, legacy downstream handshake, argument/result forwarding,
  structured results, errors, and standard OTLP/HTTP protobuf decoding.
- Verified incoming trace parentage, independent concurrent traces, base and
  exact trace endpoint paths, export authorization headers, omission of secret
  fixture arguments/results, and upstream environment allowlisting.
- Verified timeout classification and that exporter failure does not fail tools.
- A separate Jaeger 2.21.0 process accepted the demo tool span over OTLP/HTTP.
  Its `/api/v3/services` returned `mcp-otel-gateway` and `/api/v3/traces` returned
  the `mcp.tools.call` span for `gen_ai.tool.name=add`, status OK.
- Jaeger release executables matched the published SHA-256 checksums. The test
  used a standalone binary because this Ubuntu container has no Docker CLI.
  The Compose example was not executed here.

No production telemetry backend or MCP client configuration was changed.
No performance benchmark, durable-delivery guarantee, end-to-end OAuth flow,
or support for non-tool MCP capabilities is claimed.
