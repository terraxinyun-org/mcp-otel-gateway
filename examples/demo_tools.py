"""Harmless upstream MCP server for trying the gateway."""

from mcp.server import MCPServer
from pydantic import BaseModel

server = MCPServer("demo-tools")


class SumResult(BaseModel):
    sum: int


@server.tool()
def add(a: int, b: int) -> SumResult:
    """Add two integers."""
    return SumResult(sum=a + b)


if __name__ == "__main__":
    server.run()
