"""Bearer-protected Streamable HTTP transport for a trusted reverse proxy."""

from __future__ import annotations

import hmac
import os
from typing import Mapping

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .server import mcp


class BearerAuth:
    """Require one exact Bearer token on every endpoint except the health check."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == "/healthz":
            await PlainTextResponse("ok")(scope, receive, send)
            return
        if any(name.lower() == b"origin" for name, _ in scope.get("headers", [])):
            await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
            return
        credentials = [value for name, value in scope.get("headers", [])
                       if name.lower() == b"authorization"]
        if len(credentials) != 1 or not hmac.compare_digest(credentials[0], self.expected):
            await PlainTextResponse("Unauthorized", status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(env: Mapping[str, str] | None = None) -> ASGIApp:
    settings = os.environ if env is None else env
    token = settings.get("MCP_API_TOKEN", "")
    host = settings.get("MCP_PUBLIC_HOST") or settings.get("RAILWAY_PUBLIC_DOMAIN", "")
    if len(token) < 32 or not token.isascii() or any(char.isspace() for char in token):
        raise ValueError("MCP_API_TOKEN must be an ASCII token of at least 32 characters")
    if host and ("://" in host or any(char in host for char in "/:* \t\r\n")):
        raise ValueError("MCP_PUBLIC_HOST or RAILWAY_PUBLIC_DOMAIN must be a bare hostname")
    if host:
        security = TransportSecuritySettings(
            allowed_hosts=[host, f"{host}:*"], allowed_origins=[]
        )
    elif settings.get("RAILWAY_PROJECT_ID"):
        # The public domain is assigned after Railway's first deployment.
        # Railway terminates public HTTP at its reverse proxy; BearerAuth also
        # rejects browser origins while the hostname is not yet known.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        raise ValueError("MCP_PUBLIC_HOST or RAILWAY_PUBLIC_DOMAIN is required")
    mcp_app = mcp.streamable_http_app(
        transport_security=security, stateless_http=True, json_response=True,
    )
    return BearerAuth(mcp_app, token)


app = create_app()
