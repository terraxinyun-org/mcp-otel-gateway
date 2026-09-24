# MCP OpenTelemetry Gateway

A standalone, local MCP tool gateway. Replace an existing MCP tool-server entry
with this gateway to automatically emit an OpenTelemetry span for every tool call
that passes through it. The gateway forwards requests to the original server.

```text
Agent client --MCP/stdio--> Gateway --MCP--> Existing tool server
                              |
                              +--OTLP/HTTP protobuf--> Collector / trace backend
```

No vendor-specific service, model API, database, kernel sensor, privileged installation
or AI inference is required. This is a tools-only gateway, not a full MCP proxy or
host activity monitor. It does not capture local shell calls that bypass it,
model conversations, filesystem events or another MCP connection's traffic.

## Install

Python 3.11+:

```bash
git clone https://github.com/terraxinyun-org/mcp-otel-gateway.git
cd mcp-otel-gateway
python3 -m venv .venv
.venv/bin/pip install .
```

Install from this repository; the package is not published to PyPI. Runtime dependencies are the
official MCP SDK and OpenTelemetry SDK/HTTP exporter, with tested direct versions
pinned in `pyproject.toml`. No measured CPU/RAM overhead claim is made.

## Configure an upstream

Copy `examples/upstream.json` to a private configuration file and replace `command`
and `args` with your existing tool server's executable and arguments. Use absolute
paths. Set `cwd` if the upstream needs a specific working directory.

For an existing HTTP MCP server use `examples/upstream-http.json`. It supports
Streamable HTTP and static authorization from environment references:

```json
{
  "name": "company-tools",
  "agent_name": "engineering-agent",
  "transport": "streamable_http",
  "url": "https://tools.example.com/mcp",
  "headers_from_env": {"Authorization": "UPSTREAM_AUTHORIZATION"}
}
```

Supply `UPSTREAM_AUTHORIZATION` securely as the complete header value (for example
`Bearer …`). Do not put real credentials in the example files. OAuth interactive
login/token refresh is not implemented by this gateway. HTTP is allowed only on
loopback; remote upstreams and export destinations require HTTPS.

Local subprocesses receive basic runtime environment variables plus the explicit
`pass_env` allowlist. Exporter credentials are not inherited by default. This is
environment hygiene, NOT a sandbox: the upstream still runs as the same OS user
and can access that user's files, including credential files under HOME.

## Add it to your MCP client

Use `examples/mcp-client.json` as the client configuration template. Configure the
gateway executable, upstream file and OTLP destination. Client configuration
locations/formats vary; the example uses the common `mcpServers` JSON shape.

Replace the old tool-server connection rather than leaving both routes enabled.
The gateway preserves upstream tool names, schemas, descriptions, annotations
and tool results. Use one gateway entry per upstream. Restart the MCP connection
when changing upstream configuration.

The client starts the gateway over stdio. There is no new public listening port,
server URL or multi-tenant authentication layer in this release.

## Choose a telemetry destination

Supported output is **OTLP traces over HTTP/protobuf**. A compatible backend can
ingest this directly; other systems need an OpenTelemetry Collector exporter or
another supported bridge. This does not promise support for every vendor API.

| Environment variable | Meaning |
| --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Base URL, e.g. `http://127.0.0.1:4318`; SDK appends `/v1/traces` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Exact trace URL; overrides the base URL |
| `OTEL_EXPORTER_OTLP_HEADERS` | Export authorization headers; standard comma-separated, percent-encoded values |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | Trace-specific authorization headers |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | Export request timeout in seconds |
| `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` | Trace-specific timeout |
| `OTEL_EXPORTER_OTLP_CERTIFICATE` | Custom CA certificate path when required |
| `OTEL_SERVICE_NAME` | Service name, defaults to `mcp-otel-gateway` |
| `OTEL_RESOURCE_ATTRIBUTES` | Operator-controlled resource tags, e.g. `deployment.environment.name=dev` |

Set `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` if explicitly configuring a
protocol. gRPC is not bundled; use a Collector to bridge transports. A destination
is required so the gateway cannot start while silently exporting nowhere.

Jaeger, SigNoz and Grafana Tempo are examples of compatible trace backends. Their
credentials, endpoint paths and storage setup vary. Point the exporter at your
backend's OTLP trace endpoint and supply its required authorization headers.

## Local Jaeger demo

On a host with Docker Compose:

```bash
docker compose -f examples/compose.yaml up -d
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
.venv/bin/python examples/smoke.py
```

Open http://localhost:16686 and search for service `mcp-otel-gateway`.
The demo tool returns `{"sum": 7}`; its span is named `mcp.tools.call`.
The smoke command checks the MCP result; verify trace arrival in the UI.

This Jaeger setup is local-only and uses **ephemeral in-memory storage**.
It is not a production deployment. Docker must run on the same network host as
the gateway for these loopback settings. `examples/collector.yaml` is an alternative
Collector configuration for decoding and inspecting spans without a UI.

## What is exported

Each call produces a CLIENT span with timestamps, duration, status and these
attributes. Successful calls are OK; tool errors, timeouts, cancellations and
upstream failures are ERROR. Errors contain a category, not an exception message.

| Attribute | Source |
| --- | --- |
| `gen_ai.operation.name=execute_tool` | Gateway |
| `gen_ai.tool.name` | Requested upstream tool name, capped at 256 characters |
| `gen_ai.agent.name` | Operator configuration; not verified agent identity |
| `agent_monitor.upstream.name` | Operator's upstream label |
| `agent_monitor.evidence.source=gateway_observed` | Gateway |
| `agent_monitor.content_capture=false` | Gateway |
| `agent_monitor.parent_context_supplied` | Whether the caller supplied traceparent |
| `error.type` | Error category, when applicable |

Resource attributes include `service.name`, `service.version`, a random
`service.instance.id` for this gateway process and
`agent_monitor.coverage=gateway_tool_calls_only`.

Arguments, outputs, tool descriptions, URLs, headers, credentials and exception
stacks are NOT copied into spans. Tool names and operator-supplied resource labels
are still metadata: do not put secrets into them. This is content omission, not
a universal PII redaction engine. The upstream still receives full arguments and
the requesting client receives full tool results.

Optional W3C `traceparent` / `tracestate` in a tools/call request's `_meta` join
the caller's trace. Without valid context each call starts a new trace. The gateway
forwards its resulting trace context in upstream `_meta`; the upstream must
instrument/extract it to continue the trace. Baggage and unrelated metadata are
not forwarded. Trace context is untrusted correlation data, never authorization.
The SDK's evolving GenAI names are isolated to this package's instrumentation.

## Reliability and limits

- Export is asynchronous: an in-memory queue of 2,048 spans, batches of up to 128,
  and a 1-second scheduling interval. Tool calls do not wait for backend export.
- All gateway calls are sampled even if the parent is unsampled. Export failure,
  queue overflow or process termination can still lose telemetry. This is not a
  durable or tamper-proof audit log. Use a local Collector with persistent queues
  for stronger delivery, and monitor both SDK/Collector diagnostics.
- Failed export logs a warning to stderr; tool execution continues. The SDK has
  bounded request/retry behavior. Closing the gateway attempts to flush traces.
- Tool errors preserve upstream results. Transport failures return a sanitized
  error. A timeout does not undo side effects already performed by the upstream.
- Supported upstream tools must be ordinary request/response tools. Resources,
  prompts, roots, sampling, elicitation, subscriptions, task-based tools,
  continuation workflows and progress forwarding are not supported. This is not
  a transparent replacement for servers requiring those capabilities.
- Tools/list is forwarded on request; list-change notifications are not relayed.
- This release exports traces only, not a separate metrics/logs pipeline. It does
  not perform threat detection, AI review, enforcement or microVM provisioning.
- No cloud service or live MCP client configuration
  is changed by installing this package.

## Verification

```bash
.venv/bin/pip install '.[test]'
.venv/bin/pytest -q
```

Integration tests launch the real gateway and upstream subprocesses, exercise
stdio and Streamable HTTP, decode exported OTLP protobuf at a local HTTP receiver,
and verify tool results, error status, trace parentage, authorization headers and
omission of sensitive argument/result values. Other tests cover timeout behavior,
export failure isolation and credential allowlisting.

### GitHub Actions

`ci/github-actions-tests.yml` contains a CI template for Python 3.11, 3.12 and
3.13. It is not active yet: the token used for the initial publication does not
have GitHub's `workflow` permission. A maintainer with workflow write access can
move it to `.github/workflows/tests.yml` to enable tests on pushes and pull
requests. The current local validation results are in [VALIDATION.md](VALIDATION.md).

References: [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk),
[OTel Python exporters](https://opentelemetry.io/docs/languages/python/exporters/),
[Jaeger quickstart](https://www.jaegertracing.io/docs/2.21/getting-started/).

## License

[MIT](LICENSE). Copyright (c) 2026 Terraxinyun.
