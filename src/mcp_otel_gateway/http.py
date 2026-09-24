"""Authenticated single-trust-domain HTTP transport for MCP clients."""

import secrets
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings


class BearerAuth:
    """Check every HTTP request, including discovery and session operations.

    A shared token grants access to this gateway's upstream. This is not OAuth,
    per-user delegation or tenant isolation. Lifespan messages pass unchanged.
    """

    def __init__(self, app, token: str, on_shutdown=None):
        if not token.isascii() or not 32 <= len(token) <= 4096 or any(c.isspace() for c in token):
            raise ValueError("MCP_GATEWAY_AUTH_TOKEN must be 32+ non-whitespace ASCII characters")
        self.app = app
        self.expected = ("Bearer " + token).encode("ascii")
        self.on_shutdown = on_shutdown

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan" and self.on_shutdown:
            async def send_lifespan(message):
                if message["type"] == "lifespan.shutdown.complete":
                    await self.on_shutdown()
                await send(message)
            await self.app(scope, receive, send_lifespan)
            return
        if scope["type"] == "http":
            values = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
            if len(values) != 1 or not secrets.compare_digest(values[0], self.expected):
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"), (b"www-authenticate", b"Bearer"),
                ]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def make_http_app(server, token: str, public_origin: str | None = None, on_shutdown=None):
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if public_origin:
        parsed = urlsplit(public_origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("public origin must be an HTTPS origin without credentials, path or query")
        hosts.append(parsed.netloc)
        origins.append("https://" + parsed.netloc)
    app = server.streamable_http_app(
        json_response=True, stateless_http=False, max_sessions=100,
        session_idle_timeout=300,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins,
        ),
    )
    return BearerAuth(app, token, on_shutdown)
