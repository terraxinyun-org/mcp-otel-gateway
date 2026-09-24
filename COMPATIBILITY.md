# Harness compatibility

The gateway is model-agnostic and harness-agnostic at the MCP tool protocol layer.
It does not depend on a Claude/Codex SDK or require a model subscription.

**Compatibility target:** standard `tools/list` and ordinary `tools/call` over
stdio or authenticated Streamable HTTP. This is not a claim to support every
harness feature, every MCP capability, or to observe built-in tools that bypass
MCP. Harnesses without MCP support need an adapter.

## Client configurations

All local examples use the same gateway executable and upstream configuration.
Replace absolute-path placeholders and merge the entry into existing settings;
do not replace your entire configuration. Keep each client's normal tool approval
and trust controls. Restart/reconnect the server after configuration changes.

| Harness | Example | Configuration location |
| --- | --- | --- |
| Claude Code | [JSON](examples/harnesses/claude-code.json) | Project `.mcp.json`, or `claude mcp add` |
| Codex | [TOML](examples/harnesses/codex.toml) | MCP section of `~/.codex/config.toml`, or `codex mcp add` |
| Gemini CLI | [JSON](examples/harnesses/gemini-cli.json) | `mcpServers` in `settings.json` |
| Cursor | [JSON](examples/harnesses/cursor.json) | Project `.cursor/mcp.json` or user `~/.cursor/mcp.json` |
| VS Code | [JSON](examples/harnesses/vscode.json) | Workspace `.vscode/mcp.json` |
| OpenCode | [JSON](examples/harnesses/opencode.json) | `mcp` section in `opencode.json` / `opencode.jsonc` |
| Custom MCP client | [Generic JSON](examples/mcp-client.json) | Use the client's stdio transport or HTTP connection API |

These templates follow the official formats below. **They have not each been
tested through a live model session in those applications.** Protocol integration
tests are independent of product-specific configuration and organization policies.

## URL-based clients

Start the gateway with Streamable HTTP enabled. Provide a random, private token
of at least 32 characters in `MCP_GATEWAY_AUTH_TOKEN`, via your secret manager or
protected process environment. Do not put it into source control.

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
# Provision MCP_GATEWAY_AUTH_TOKEN securely before starting this command.
mcp-otel-gateway --config /absolute/path/to/upstream.json \
  --transport streamable-http --port 8765
```

Clients connect to `http://127.0.0.1:8765/mcp` and send
`Authorization: Bearer <MCP_GATEWAY_AUTH_TOKEN>` on every request. This is distinct
from upstream authorization and OTLP export credentials. The token is never added
to telemetry or passed to a stdio upstream by default.

For example, Codex supports an environment reference rather than a literal token:

```toml
[mcp_servers.monitored-tools]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "MCP_GATEWAY_AUTH_TOKEN"
```

Other clients use their documented HTTP URL and header fields. Gemini CLI uses
`httpUrl`; Claude Code, Cursor and VS Code have HTTP transport settings; OpenCode
uses `type: "remote"` and supports disabling OAuth when using static headers.
Environment interpolation syntax is client-specific; do not assume the same
`${VAR}` syntax everywhere. Clients that require OAuth discovery cannot use this
static-token endpoint without an additional authorization integration.

The server binds only to loopback. For a remote harness, place an HTTPS reverse
proxy on the same host and pass `--public-origin https://gateway.example.com`.
Preserve `Authorization`, HTTP methods, MCP headers and streaming responses.
Public-origin configuration only sets the allowed Host/Origin values; it does
not configure DNS, TLS, a proxy or public ingress. No remote service is deployed
by the package. Legacy standalone HTTP+SSE transport is not supported.

HTTP mode is **one trust domain per gateway**: all authorized clients share the
configured upstream connection, access and resource labels. The shared token
is not per-user identity, delegation or tenant separation. Use separate processes
and credentials for mutually untrusted users/tenants.

## Tested behavior

- Actual gateway and upstream subprocesses, not only mocked handlers.
- Raw newline-delimited JSON-RPC over stdio at protocol revisions `2024-11-05`,
  `2025-03-26`, `2025-06-18`, and `2025-11-25`.
- Current SDK auto-negotiation and legacy initialization.
- Authenticated downstream Streamable HTTP and stdio/HTTP upstreams.
- Missing/wrong HTTP authorization rejected; untrusted browser Origin rejected.
- Tool schemas, structured results, errors, concurrent calls and trace parentage.
- OTLP protobuf output and final-buffer flushing on graceful HTTP shutdown.

Supported standard tool calls work regardless of which model chooses them.
Full MCP resources/prompts, roots, sampling, elicitation, progress forwarding,
task/continuation workflows and tool-list-change notifications remain unsupported.

## Monitoring boundary

A successful MCP connection does not mean the entire agent is monitored. Only
calls routed through this gateway produce spans. Built-in shell, file editing,
browser tools and direct model/API connections require harness instrumentation,
runtime sensors, or execution through a monitored sandbox.

## Official configuration references

- [Claude Code](https://code.claude.com/docs/en/mcp)
- [Codex](https://developers.openai.com/codex/mcp/)
- [Gemini CLI](https://geminicli.com/docs/tools/mcp-server/)
- [Cursor](https://cursor.com/docs/mcp)
- [VS Code](https://code.visualstudio.com/docs/agent-customization/mcp-servers)
- [OpenCode](https://opencode.ai/docs/mcp-servers/)
