"""Check the public HTTP boundary without connecting to Neo4j."""

import os
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from text2sql_mcp.contracts import GraphError


class FakeRepository:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def get_definition(self, model=None, datasets=(), tables=(), metrics=(), with_definition=True):
        self.calls.append((model, list(datasets), list(tables), list(metrics), with_definition))
        if self.error:
            raise self.error
        out = {"tenant": "olist", "model": model or "olist_sales", "version_id": "v1", "defined_tables": []}
        return {**out, "definition": {"name": "olist_sales", "datasets": []}} if with_definition else out

    def list_models(self):
        return {"tenant": "olist", "models": [{"model": "olist_sales", "version_id": "v1"}], "has_more": False}


class HTTPAppTest(unittest.TestCase):
    def test_railway_first_deploy_without_domain(self):
        with patch.dict(os.environ, {"RAILWAY_PROJECT_ID": "project-id", "MCP_API_TOKEN": "t" * 40}):
            from text2sql_mcp.http_app import create_app

        app = create_app({"RAILWAY_PROJECT_ID": "project-id", "MCP_API_TOKEN": "t" * 40})
        with TestClient(app, base_url="http://temporary.railway.internal") as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            self.assertEqual(client.get("/mcp").status_code, 401)
            self.assertEqual(client.get("/mcp", headers={
                "Authorization": "Bearer " + "t" * 40,
                "Origin": "https://other.example.test",
            }).status_code, 403)
            # No public domain yet: like /mcp, /definition does not check Host.
            with patch("text2sql_mcp.http_app.repository", return_value=FakeRepository()):
                self.assertEqual(client.post("/definition", json={}, headers={
                    "Authorization": "Bearer " + "t" * 40}).status_code, 200)

    def test_health_auth_host_and_origin(self):
        token = "t" * 40
        with patch.dict(os.environ, {"MCP_PUBLIC_HOST": "mcp.example.test", "MCP_API_TOKEN": token}):
            from text2sql_mcp.http_app import create_app
        app = create_app({"MCP_PUBLIC_HOST": "mcp.example.test", "MCP_API_TOKEN": token})

        with TestClient(app, base_url="http://mcp.example.test") as client:
            health = client.get("/healthz")
            self.assertEqual((health.status_code, health.text), (200, "ok"))
            denied = client.get("/mcp")
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(denied.headers["www-authenticate"], "Bearer")
            duplicate = client.get("/mcp", headers=[("Authorization", f"Bearer {token}"),
                                                    ("Authorization", f"Bearer {token}")])
            self.assertEqual(duplicate.status_code, 401)
            authorized = {"Authorization": f"Bearer {token}"}
            bad_host = client.get("/mcp", headers={**authorized, "Host": "other.example.test"})
            self.assertEqual(bad_host.status_code, 421)
            bad_origin = client.get("/mcp", headers={**authorized,
                                                    "Origin": "https://other.example.test"})
            self.assertEqual(bad_origin.status_code, 403)
            mcp_headers = {
                **authorized, "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            initialized = client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }, headers=mcp_headers)
            self.assertEqual(initialized.status_code, 200)
            client.post("/mcp", json={"jsonrpc": "2.0",
                                        "method": "notifications/initialized"}, headers=mcp_headers)
            listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                               "method": "tools/list"}, headers=mcp_headers)
            self.assertEqual(listed.status_code, 200)
            names = {tool["name"] for tool in listed.json()["result"]["tools"]}
            self.assertEqual(len(names), 7)
            self.assertNotIn("graph_get_model_snapshot", names)
            repo = FakeRepository()
            with patch("text2sql_mcp.server.repository", return_value=repo):
                called = client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "graph_list_models", "arguments": {}},
                }, headers=mcp_headers)
            self.assertEqual(called.status_code, 200)
            result = called.json()["result"]
            self.assertFalse(result.get("isError", False))
            self.assertEqual(result["structuredContent"], repo.list_models())
            self.assertEqual(result["content"], [])

    def test_definition_route(self):
        token = "t" * 40
        env = {"MCP_PUBLIC_HOST": "mcp.example.test", "MCP_API_TOKEN": token}
        with patch.dict(os.environ, env):
            from text2sql_mcp.http_app import create_app
        authorized = {"Authorization": f"Bearer {token}"}
        body = {"datasets": ["order_lines"], "tables": ["orders"], "metrics": ["revenue"]}
        with TestClient(create_app(env), base_url="http://mcp.example.test") as client:
            self.assertEqual(client.post("/definition", json=body).status_code, 401)
            self.assertEqual(client.post("/definition", json=body, headers={"Authorization": "Bearer wrong"}).status_code, 401)
            self.assertEqual(client.post("/definition", json=body, headers={
                **authorized, "Origin": "https://other.example.test"}).status_code, 403)
            with patch("text2sql_mcp.http_app.repository", return_value=FakeRepository()):
                # Same Host allow-list as /mcp (DNS-rebinding protection).
                self.assertEqual(client.post("/definition", json=body, headers={
                    **authorized, "Host": "other.example.test"}).status_code, 421)
                self.assertEqual(client.post("/definition", json=body, headers={
                    **authorized, "Host": "mcp.example.test:443"}).status_code, 200)
            repo = FakeRepository()
            with patch("text2sql_mcp.http_app.repository", return_value=repo):
                default = client.post("/definition", json=body, headers=authorized)
                named = client.post("/definition/other_model", json={"tables": ["orders"], "definition": False},
                                    headers=authorized)
                got = client.get("/definition", headers=authorized)
                bad = [client.post("/definition", content=raw, headers={**authorized, "Content-Type": "application/json"})
                       for raw in (b"[]", b"not json", b'{"datasets": "orders"}', b'{"datasets": [1]}',
                                   b'{"definition": "yes"}', ('{"tables": [%s]}' % ",".join(['"t"'] * 501)).encode())]
            self.assertEqual(default.status_code, 200)
            self.assertEqual(default.headers["cache-control"], "no-store")
            self.assertIn("definition", default.json())
            self.assertEqual(named.json()["model"], "other_model")
            self.assertNotIn("definition", named.json())
            self.assertEqual(repo.calls, [(None, ["order_lines"], ["orders"], ["revenue"], True),
                                          ("other_model", [], ["orders"], [], False)])
            self.assertEqual(got.status_code, 405)
            self.assertEqual([r.status_code for r in bad], [400] * 6)
            self.assertNotIn("snapshot", client.app.__class__.__name__.lower())
            self.assertEqual(client.get("/snapshot", headers=authorized).status_code, 404)  # removed in 0.4.0
            for code, status in (("NOT_FOUND", 404), ("NO_ACTIVE_VERSION", 404),
                                 ("DB_UNAVAILABLE", 503), ("RESULT_TOO_LARGE", 500)):
                failing = FakeRepository(GraphError(code, "reason"))
                with patch("text2sql_mcp.http_app.repository", return_value=failing):
                    response = client.post("/definition", json=body, headers=authorized)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {"error": {"code": code, "message": "reason"}})


if __name__ == "__main__":
    unittest.main()
