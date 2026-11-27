"""Bearer-protected Streamable HTTP transport for a trusted reverse proxy."""

from __future__ import annotations

import hmac
import os
from typing import Mapping

from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .contracts import GraphError
from .server import mcp, repository

_STATUS = {"NOT_FOUND": 404, "NO_ACTIVE_VERSION": 404, "DB_UNAVAILABLE": 503, "INVALID_INPUT": 400}


_NAMES = 500  # names per request; one question names a handful


def _names(body: object) -> dict | None:
    """The request's datasets/tables/metrics lists, or None when the body has another shape."""
    if not isinstance(body, dict) or not isinstance(body.get("definition", True), bool):
        return None
    lists = {key: body.get(key, []) for key in ("datasets", "tables", "metrics")}
    ok = all(isinstance(v, list) and len(v) <= _NAMES and all(isinstance(x, str) and 0 < len(x) <= 200 for x in v)
             for v in lists.values())
    return lists if ok else None


async def _definition(scope: Scope, receive: Receive) -> Response:
    """POST /definition[/{model}]: the part of the active definition one t2s command needs, never the whole
    model. The JSON body names datasets, physical tables and metrics. Not an MCP tool, so no LLM calls it."""
    if scope["method"] != "POST":
        return PlainTextResponse("Method Not Allowed", status_code=405, headers={"Allow": "POST"})
    model = scope["path"][len("/definition"):].lstrip("/") or None
    try:
        body = await Request(scope, receive).json()
    except ValueError:
        body = None
    lists = _names(body)
    if lists is None:
        return JSONResponse({"error": {"code": "INVALID_INPUT", "message": (
            "Body must be a JSON object with optional datasets, tables and metrics lists of names "
            f"(at most {_NAMES} each) and an optional boolean definition")}}, status_code=400)
    try:
        # The Neo4j driver is synchronous; keep it off the event loop.
        result = await run_in_threadpool(repository().get_definition, model=model,
                                         with_definition=body.get("definition", True), **lists)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except GraphError as error:
        return JSONResponse({"error": {"code": error.code, "message": error.args[0]}},
                            status_code=_STATUS.get(error.code, 500))


class BearerAuth:
    """Require one exact Bearer token on every endpoint except the health check."""

    def __init__(self, app: ASGIApp, token: str, host: str = ""):
        self.app = app
        self.expected = f"Bearer {token}".encode("ascii")
        self.host = host  # the MCP app checks Host itself; /definition is answered here, so check it the same way

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
        if path == "/definition" or path.startswith("/definition/"):
            if not self._host_allowed(scope):  # same allow-list as TransportSecuritySettings on /mcp
                await PlainTextResponse("Invalid Host header", status_code=421)(scope, receive, send)
                return
            await (await _definition(scope, receive))(scope, receive, send)
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
