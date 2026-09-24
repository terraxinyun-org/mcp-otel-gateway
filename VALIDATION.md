# Validation — 2026-09-24

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
