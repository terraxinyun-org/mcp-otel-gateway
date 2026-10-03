"""Run harmless MCP commands/files and send their logs and traces to Grafana."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import Client, StdioServerParameters


async def run(env_file: Path):
    if not env_file.is_file():
        raise SystemExit(f"Missing private config: {env_file}. Run python3 examples/grafana/configure.py first.")
    with tempfile.TemporaryDirectory(prefix="mcp-grafana-demo-") as directory:
        config = Path(directory) / "runtime.json"
        config.write_text(json.dumps({
            "name": "grafana-demo",
            "agent_name": "demo-agent",
            "transport": "runtime",
            "runtime_workspace": directory,
            "capture_content": True,
            "export_logs": True,
        }))
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_otel_gateway.server", "--config", str(config),
                  "--env-file", str(env_file)],
            env=dict(os.environ),
        )
        async with Client(params) as client:
            actions = [
                ("run_command", {"argv": [sys.executable, "-c", "print('grafana-mcp-demo: command completed')"]}, False),
                ("write_file", {"path": "demo.txt", "content": "harmless demo data\n"}, False),
                ("read_file", {"path": "demo.txt"}, False),
                ("run_command", {"argv": [sys.executable, "-c", "raise SystemExit(7)"]}, True),
            ]
            for name, arguments, expected_error in actions:
                result = await client.call_tool(name, arguments)
                if result.is_error != expected_error:
                    raise RuntimeError(f"Unexpected result from {name}: {result.structured_content}")
                detail = result.structured_content or {}
                print(f"{name}: {'expected exit 7' if expected_error else 'OK'}"
                      + (f" ({detail.get('stdout', '').strip()})" if detail.get("stdout") else ""))
    print("Gateway exited and flushed telemetry at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    print("In Grafana, choose Last 15 minutes; search Loki for {service_name=\"mcp-otel-gateway\"}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path,
                        default=Path.home() / ".config/mcp-otel-gateway/grafana.json")
    args = parser.parse_args()
    asyncio.run(run(args.env_file.expanduser().resolve()))
