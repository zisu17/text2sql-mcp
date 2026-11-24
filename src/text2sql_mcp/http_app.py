"""Bearer-protected Streamable HTTP transport for a trusted reverse proxy."""

from __future__ import annotations

import hmac
import os
from typing import Mapping

from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .contracts import GraphError
from .server import mcp, repository

_STATUS = {"NOT_FOUND": 404, "NO_ACTIVE_VERSION": 404, "DB_UNAVAILABLE": 503}


async def _snapshot(scope: Scope) -> Response:
    """GET /snapshot[/{model}]: the executable model for t2s. Not an MCP tool, so no LLM can call it."""
    if scope["method"] != "GET":
        return PlainTextResponse("Method Not Allowed", status_code=405, headers={"Allow": "GET"})
    model = scope["path"][len("/snapshot"):].lstrip("/") or None
    try:
        # The Neo4j driver is synchronous; keep it off the event loop.
        return JSONResponse(await run_in_threadpool(repository().get_model_snapshot, model=model))
    except GraphError as error:
        return JSONResponse({"error": {"code": error.code, "message": error.args[0]}},
                            status_code=_STATUS.get(error.code, 500))


class BearerAuth:
    """Require one exact Bearer token on every endpoint except the health check."""

    def __init__(self, app: ASGIApp, token: str, host: str = ""):
        self.app = app
        self.expected = f"Bearer {token}".encode("ascii")
        self.host = host  # the MCP app checks Host itself; /snapshot is answered here, so check it the same way

    def _host_allowed(self, scope: Scope) -> bool:
        if not self.host:
            return True
        hosts = [value.decode("latin-1") for name, value in scope.get("headers", []) if name.lower() == b"host"]
        return len(hosts) == 1 and (hosts[0] == self.host or hosts[0].startswith(self.host + ":"))

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
        path = scope.get("path", "")
        if path == "/snapshot" or path.startswith("/snapshot/"):
            if not self._host_allowed(scope):  # same allow-list as TransportSecuritySettings on /mcp
                await PlainTextResponse("Invalid Host header", status_code=421)(scope, receive, send)
                return
            await (await _snapshot(scope))(scope, receive, send)
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
    return BearerAuth(mcp_app, token, host)


app = create_app()
