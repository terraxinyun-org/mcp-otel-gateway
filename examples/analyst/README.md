# Headless Codex analyst

Review recorded MCP activity using your signed-in Codex CLI. This is an optional
batch analysis process outside the gateway's tool execution path. It requires no
additional Python dependency, kernel sensor or agent installed in the workload.
Codex inference uses your account's available usage and adds analysis latency;
it does not add that latency to the original tool call.

```text
Agent → MCP gateway → execution
             ↓
       OTLP logs / traces → telemetry backend
                                  ↓ bounded evidence snapshot
                           headless Codex analyst
                                  ↓ OTLP finding logs
                              Grafana / Loki
```

## Run an analysis

Install this repository and have a signed-in Codex CLI with a populated model
cache. The launcher uses your configured model unless `--model` selects another
cached model. Tested with Codex CLI 0.156.1; revalidate tool restrictions on upgrades.

Save task intent and authorization in an operator-owned context file. Do not use
an agent-generated description as trusted authorization context. Omitting context
leaves authorization unknown.

Analyze one or more saved Loki `query_range` JSON responses:

```bash
mcp-otel-analyze \
  --input /private/run-one-logs.json /private/run-two-logs.json \
  --context-file /private/task-context.txt \
  --output /private/analysis.json
```

Alternatively query a Grafana Loki data source directly:

```bash
mcp-otel-analyze \
  --grafana-url https://your-stack.grafana.net \
  --grafana-token-file /private/grafana-service-account.token \
  --datasource grafanacloud-logs \
  --agent your-agent-label \
  --lookback-minutes 15 \
  --context-file /private/task-context.txt \
  --output /private/analysis.json \
  --env-file /private/otel.json --send
```

Grafana reads use a service-account token with permission to query the data source.
OTLP writes use the separate ingestion credentials in `otel.json`. Input is limited
to 128 events / 256 KB after normalization, files to 4 MiB. Queries reaching the
event limit fail explicitly; narrow the time range or select an agent. They are
not silently truncated. Empty batches fail without launching Codex.

This command runs once. It does not install a polling service, change the gateway,
or automatically analyze future activity. An operator can schedule bounded batches;
re-running the same input still incurs another model call. No durable queue,
deduplication store or exactly-once delivery is included.

## Backend-neutral input

The core analyst also accepts a JSON array of normalized events:

```json
[
  {
    "timestamp_ns": "1790254214608186309",
    "agent_name": "engineering-agent",
    "instance_id": "gateway-instance-1",
    "trace_id": "11111111111111111111111111111111",
    "span_id": "2222222222222222",
    "event": "mcp.tool.completed",
    "tool": "run_command",
    "content": {"argv": ["pwd"], "stdout": "/workspace\n", "exit_code": 0},
    "capture_truncated": false
  }
]
```

Keep both `mcp.tool.started` and terminal events so file paths, arguments and results
are available. Preserve agent/instance identity for sequence correlation. Other
backends can transform their exported log records to this shape. OTLP JSON logs
are not accepted directly. The Grafana adapter currently selects the gateway's
default `service_name="mcp-otel-gateway"`.

## Findings and controls

Codex returns a narrative summary, disposition, limitations and up to twelve findings.
Each finding includes category (security, operational or coverage), activity type,
observed evidence, interpretation, recommendation and source evidence IDs. There
are no severity levels, confidence ratings or risk scores. Every referenced ID is
checked against the input; the interpretation still requires human review.

The versioned [activity analyst prompt](../../src/mcp_otel_gateway/prompts/activity_analyst.txt)
is the analyst's developer instruction, loaded with every headless invocation.
It asks the model to review action sequences against operator task context, look
for sensitive-data access, unexpected transfers, audit/monitoring changes, boundary
crossing, encoded execution and task deviation, and distinguish facts from inference.
Prompt version `codex-activity-v2` is saved with the report and exported findings.
The JSON schema organizes explanations and evidence; it is not a scoring rubric.

The process uses `codex exec --output-schema` with a temporary workspace,
`--ephemeral`, read-only sandbox mode, ignored user/project rules and user config,
no configured MCP servers, disabled execution/browser/plugin features, and a model
catalog with shell/patch tools disabled. Evidence goes through stdin, not process
arguments. OTLP/Grafana credentials are not passed in the child's environment.
CLI events must show a completed turn without tool/action events before the report
is accepted. These per-launch restrictions are not a substitute for OS isolation.

Source content is explicitly treated as untrusted data. Best-effort redaction runs
before model submission and again on generated findings. This is not comprehensive
PII detection or a guarantee against prompt injection. Data is sent to the model
through Codex; review your data handling requirements before using production logs.
The model cannot recover redacted output or observe hidden subprocess activity.

The output report is created with mode 0600 and contains redacted source evidence,
model identity, token usage, elapsed time, and the validated assessment. Existing
output files are never overwritten. Model timeouts, failed turns, action events,
invalid schemas and invented evidence IDs reject the report. References being valid
does not prove the model interpreted them correctly; human review remains necessary.

`--send` emits `agent.analysis.summary` and `agent.analysis.finding` as OTLP logs
under service `mcp-otel-analyst`. Findings carry source trace/span IDs and supporting
evidence. The log timestamp is analysis time; original event times remain in the
evidence. Export failure returns a failure exit code and keeps the saved report.
No blocking, remediation, process termination or alert notification is performed.

In Grafana Explore (Loki):

```logql
{service_name="mcp-otel-analyst"} | json | event="agent.analysis.finding" | prompt_version="codex-activity-v2"
```

The dashboard shows finding counts by activity type and category, plus a timeline
at report publication time. Counts are distinct finding IDs, not attacks, commands,
or scores. One finding can discuss a related sequence. Only observed categories
appear; no data does not establish safety. Earlier v1 rated reports remain in the
backend but are excluded from the current AI panels.

Reference: [Codex noninteractive CLI options](https://learn.chatgpt.com/docs/developer-commands).
