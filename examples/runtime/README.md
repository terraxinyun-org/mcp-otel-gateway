# Commands, files and logs through MCP

Runtime mode exposes exactly three tools in the gateway process: `run_command`,
`read_file`, and `write_file`. There is no separate host monitoring daemon.

```text
Agent -> MCP gateway -> command/file operation
                    -> OTLP traces -> Tempo (or another compatible backend)
                    -> OTLP logs   -> Loki (or another compatible backend)
```

## Configure runtime mode

Create a private JSON config using an existing absolute workspace directory:

```json
{
  "name": "execution",
  "agent_name": "my-agent",
  "transport": "runtime",
  "runtime_workspace": "/absolute/path/to/workspace",
  "capture_content": true,
  "export_logs": true,
  "runtime_output_bytes": 16384
}
```

Start it as your harness's MCP stdio server:

```bash
mcp-otel-gateway --config /private/runtime.json --env-file /private/otel.json
```

The optional private environment file is a JSON object containing standard
`OTEL_...` settings, including endpoint and authentication headers. It is not
shell code. Keep it outside the workspace/repository and restrict access to it.
Use `OTEL_EXPORTER_OTLP_ENDPOINT` for both signals, or separate
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` values.
Log-specific headers/protocol/timeouts use the standard OTel environment settings.
Grafana Cloud needs `logs:write` and `traces:write` ingestion permissions.

Every routed tool produces a trace and, when logs are enabled, correlated
`mcp.tool.started` / `mcp.tool.completed` logs. Timeouts, cancellations and upstream
failures emit error logs. With content capture enabled, logs include bounded,
best-effort-redacted input and output. Commands return separate stdout/stderr,
exit code, timeout and truncation flags. Output logs are emitted at completion,
not as a live stream of every stdout byte. Use a short command timeout for
interactive tasks; the default is 30 seconds.

The gateway still supports ordinary upstream MCP servers: set `export_logs: true`
on an existing stdio/HTTP config to add logs alongside its existing traces.

## Require the route in Codex

`run_codex.py` launches one isolated CLI invocation using only this execution MCP
server. It ignores ordinary user config/rules, disables built-in shell, browser,
apps, plugins and subagent tools, and uses a copy of the installed model catalog
with built-in shell and apply-patch capability removed. The MCP connection is
required: startup fails if it is unavailable. Only the three execution tools are
allowlisted and preapproved for this deliberately unattended invocation.

```bash
.venv/bin/python examples/runtime/run_codex.py \
  --workspace /absolute/path/to/disposable-workspace \
  --otel-env-file /private/otel.json \
  --prompt-file /private/task.txt
```

Requires a signed-in CLI with `models_cache.json`, and a configured model (or
`--model`). Uses workspace-write sandbox policy. For models that require Code
Mode, its dispatcher remains enabled to invoke the MCP tools; its built-in Node
REPL is disabled in the catalog. This is configuration for the tested CLI version,
not a protocol-level enforcement feature or a guarantee for all harness versions.
Managed organizational requirements still apply. Revalidate after upgrades.

The launcher changes neither the user's global configuration nor existing
sessions. Other harnesses need their own built-in tool restrictions. Adding MCP
alone does not remove their shell or editing tools.

## Limits and deployment boundary

- Runtime command execution is for POSIX hosts. It runs with the gateway's OS
  permissions. A working-directory check is **not a filesystem/network sandbox**;
  a command can access whatever that OS identity can access. Use a dedicated
  unprivileged identity and a microVM/container for untrusted execution.
- The runtime clears non-basic environment variables from command children, but
  same-user processes can access readable files and potentially process state.
  Do not treat this as protection of credentials from hostile same-user code.
- File tools reject paths resolved outside the configured workspace. They are
  convenience tools, not an independently hardened filesystem security boundary.
- Commands use a fresh process group, which is killed on timeout/cancellation or
  after completion. This is best-effort cleanup, not containment of processes that
  deliberately create another session. No background-session API is provided.
- stdout/stderr default to 16 KiB each; text file reads are bounded at 64 KiB and
  writes at 128 KiB. Redaction can omit larger captured payloads. No universal
  PII/secret detection guarantee is made.
- Traces/logs use independent bounded in-memory batch queues. Export failure does
  not block execution. This is mandatory routing, not fail-closed audit delivery.
- There is no collection of arbitrary host logs, invisible subprocess internals,
  model conversations, or activity outside these MCP operations.

For Grafana, import the [dashboard template](../grafana/agent-monitoring.json),
select Tempo and Loki data sources, and view its command/file log panels.
