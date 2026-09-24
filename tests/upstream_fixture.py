"""Only used by integration tests: a real MCP subprocess, no cloud access."""

import asyncio
import os
import sys

import mcp_types as types
from mcp.server import MCPServer
from pydantic import BaseModel

server = MCPServer("fixture")


class EchoResponse(BaseModel):
    value: str
    collector_secret_visible: bool


@server.tool()
def echo(value: str) -> EchoResponse:
    return EchoResponse(value=value, collector_secret_visible="OTEL_EXPORTER_OTLP_HEADERS" in os.environ)


@server.tool()
def reject(value: str) -> types.CallToolResult:
    return types.CallToolResult(is_error=True, content=[types.TextContent(type="text", text=value)])


@server.tool()
async def slow() -> str:
    await asyncio.sleep(3)
    return "finished"


if __name__ == "__main__":
    if len(sys.argv) > 1:
        import uvicorn
        uvicorn.run(server.streamable_http_app(), host="127.0.0.1", port=int(sys.argv[1]), log_level="error")
    else:
        server.run()
