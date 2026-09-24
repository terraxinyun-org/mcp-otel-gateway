"""Exercise actual MCP -> gateway -> upstream -> OTLP on a local backend.

Run after starting Jaeger; set OTEL_EXPORTER_OTLP_ENDPOINT in the environment.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters


async def main():
    if not (os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")):
        raise SystemExit("Set OTEL_EXPORTER_OTLP_ENDPOINT to your collector or Jaeger endpoint first")
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "upstream.json"
        config.write_text(json.dumps({
            "name": "demo-tools", "agent_name": "demo-agent",
            "command": sys.executable, "args": [str(Path(__file__).with_name("demo_tools.py"))],
        }))
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "mcp_otel_gateway.server", "--config", str(config)],
            env=dict(os.environ),
        )
        async with Client(params) as client:
            print("Tools:", [t.name for t in (await client.list_tools()).tools])
            result = await client.call_tool("add", {"a": 3, "b": 4})
            assert not result.is_error and result.structured_content == {"sum": 7}
            print("MCP result:", result.structured_content)
    print("Gateway exited and attempted its final telemetry flush.")
    print("Verify delivery in your backend: service=mcp-otel-gateway, operation=mcp.tools.call.")


if __name__ == "__main__":
    asyncio.run(main())
