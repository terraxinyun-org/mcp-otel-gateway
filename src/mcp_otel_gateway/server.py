"""Single-upstream MCP tool gateway with stdio or authenticated Streamable HTTP."""

import argparse
import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import mcp_types as types
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.trace import SpanKind, Status, StatusCode

from . import __version__
from .config import GatewayConfig, child_environment, read_config
from .telemetry import Telemetry
from .http import make_http_app

PROPAGATOR = TraceContextTextMapPropagator()
LOG = logging.getLogger("mcp_otel_gateway")


@asynccontextmanager
async def upstream_client(config: GatewayConfig):
    if config.transport == "stdio":
        params = StdioServerParameters(command=config.command, args=config.args,
                                       cwd=config.cwd, env=child_environment(config))
        async with Client(params, read_timeout_seconds=config.timeout_seconds) as client:
            yield client
    else:
        headers = {name: os.environ[env_name] for name, env_name in config.headers_from_env.items()}
        async with httpx2.AsyncClient(headers=headers, follow_redirects=False,
                                     timeout=config.timeout_seconds) as http:
            transport = streamable_http_client(config.url, http_client=http)
            async with Client(transport, read_timeout_seconds=config.timeout_seconds) as client:
                yield client


def error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(is_error=True, content=[types.TextContent(type="text", text=message)])


def make_server(config: GatewayConfig, telemetry: Telemetry, connect=None) -> Server:
    connect = connect or (lambda: upstream_client(config))

    @asynccontextmanager
    async def lifespan(server):
        async with connect() as client:
            yield client

    async def list_tools(ctx, params):
        # Forward pagination, schemas and annotations; no extra monitoring tools.
        return await ctx.lifespan_context.list_tools(cursor=params.cursor if params else None,
                                                     cache_mode="bypass")

    async def call_tool(ctx, params):
        # Only trace context is accepted as parent metadata, never arbitrary baggage.
        # Parentage is correlation, NOT a trusted tenant/agent identity.
        meta = params.meta or {}
        carrier = {key: meta[key] for key in ("traceparent", "tracestate")
                   if isinstance(meta.get(key), str)}
        parent = PROPAGATOR.extract(carrier)
        attrs = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": params.name[:256],
            "gen_ai.agent.name": config.agent_name,
            "agent_monitor.upstream.name": config.name,
            "agent_monitor.evidence.source": "gateway_observed",
            "agent_monitor.content_capture": False,
            "agent_monitor.parent_context_supplied": bool(carrier.get("traceparent")),
        }
        # Fixed span name avoids embedding caller-controlled content in span names.
        with telemetry.tracer.start_as_current_span(
            "mcp.tools.call", context=parent, kind=SpanKind.CLIENT, attributes=attrs,
            record_exception=False, set_status_on_exception=False,
        ) as span:
            if params.task is not None or params.input_responses is not None or params.request_state is not None:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", "unsupported_continuation")
                return error_result("Task-based and multi-round-trip tools are not supported by this gateway.")
            forwarded = {}
            PROPAGATOR.inject(forwarded)
            try:
                async with asyncio.timeout(config.timeout_seconds):
                    result = await ctx.lifespan_context.call_tool(
                        params.name, params.arguments,
                        meta=forwarded,
                    )
                if result.is_error:
                    span.set_status(Status(StatusCode.ERROR))
                    span.set_attribute("error.type", "tool_error")
                else:
                    span.set_status(Status(StatusCode.OK))
                # Preserve text, binary content, structured output and metadata.
                return result
            except asyncio.CancelledError:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", "cancelled")
                raise
            except TimeoutError:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", "timeout")
                return error_result("Upstream tool timed out. Its external side effects may still have occurred; do not retry blindly.")
            except Exception:
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("error.type", "upstream_error")
                LOG.warning("Upstream MCP call failed; exception details omitted to protect payloads.")
                return error_result("Upstream MCP call failed. Check the upstream service.")

    return Server("mcp-otel-gateway", version=__version__, lifespan=lifespan,
                  on_list_tools=list_tools, on_call_tool=call_tool)


async def serve(config, telemetry, transport="stdio", host="127.0.0.1", port=8765, public_origin=None):
    server = make_server(config, telemetry)
    if transport == "stdio":
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    else:
        import uvicorn
        # Flush before Uvicorn re-raises SIGTERM after lifespan shutdown.
        async def flush():
            await asyncio.to_thread(telemetry.provider.force_flush, timeout_millis=5000)
        app = make_http_app(server, os.environ.get("MCP_GATEWAY_AUTH_TOKEN", ""), public_origin,
                            on_shutdown=flush)
        await uvicorn.Server(uvicorn.Config(
            app, host=host, port=port, log_level="warning", access_log=False,
            proxy_headers=False,
        )).serve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", choices=["127.0.0.1", "::1"], default="127.0.0.1",
                        help="HTTP binds to loopback; use an HTTPS reverse proxy for remote clients")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--public-origin", help="HTTPS origin accepted behind a reverse proxy")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s: %(message)s")
    telemetry = None
    try:
        config = read_config(args.config)
        telemetry = Telemetry(config.name, config.agent_name)
        asyncio.run(serve(config, telemetry, args.transport, args.host, args.port, args.public_origin))
    except KeyboardInterrupt:
        pass
    except Exception:
        # Validation errors can contain supplied secrets. Never print the config.
        LOG.error("Gateway stopped. Check config, required environment, upstream access and OTLP settings.")
        sys.exit(1)
    finally:
        if telemetry:
            telemetry.close()


if __name__ == "__main__":
    main()
