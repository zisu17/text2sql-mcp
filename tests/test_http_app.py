"""Check the public HTTP boundary without connecting to Neo4j."""

import os
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from text2sql_mcp.contracts import GraphError


class FakeRepository:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def get_model_snapshot(self, model=None):
        self.calls.append(model)
        if self.error:
            raise self.error
        return {"tenant": "olist", "model": model or "olist_sales", "version_id": "v1",
                "definition": {"name": "olist_sales", "description": "매출", "datasets": []}}

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
            # No public domain yet: like /mcp, /snapshot does not check Host.
            with patch("text2sql_mcp.http_app.repository", return_value=FakeRepository()):
                self.assertEqual(client.get("/snapshot", headers={
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

    def test_snapshot_route(self):
        token = "t" * 40
        env = {"MCP_PUBLIC_HOST": "mcp.example.test", "MCP_API_TOKEN": token}
        with patch.dict(os.environ, env):
            from text2sql_mcp.http_app import create_app
        authorized = {"Authorization": f"Bearer {token}"}
        with TestClient(create_app(env), base_url="http://mcp.example.test") as client:
            self.assertEqual(client.get("/snapshot").status_code, 401)
            self.assertEqual(client.get("/snapshot", headers={"Authorization": "Bearer wrong"}).status_code, 401)
            self.assertEqual(client.get("/snapshot", headers={
                **authorized, "Origin": "https://other.example.test"}).status_code, 403)
            with patch("text2sql_mcp.http_app.repository", return_value=FakeRepository()):
                # Same Host allow-list as /mcp (DNS-rebinding protection).
                self.assertEqual(client.get("/snapshot", headers={
                    **authorized, "Host": "other.example.test"}).status_code, 421)
                self.assertEqual(client.get("/snapshot", headers={
                    **authorized, "Host": "mcp.example.test:443"}).status_code, 200)
            repo = FakeRepository()
            with patch("text2sql_mcp.http_app.repository", return_value=repo):
                default = client.get("/snapshot", headers=authorized)
                named = client.get("/snapshot/other_model", headers=authorized)
                conditional = {"If-None-Match": default.headers["etag"]}
                cached = client.get("/snapshot", headers={**authorized, **conditional})
                self.assertEqual((cached.status_code, cached.content), (304, b""))
                self.assertEqual(client.get("/snapshot", headers=conditional).status_code, 401)
                self.assertEqual(client.get("/snapshot/other_model", headers={**authorized, **conditional}).status_code, 200)
                posted = client.post("/snapshot", headers=authorized)
            self.assertEqual(default.status_code, 200)
            self.assertEqual(default.headers["content-type"], "application/json")
            self.assertEqual(default.json(), FakeRepository().get_model_snapshot())
            self.assertEqual(named.json()["model"], "other_model")
            self.assertEqual(repo.calls, [None, "other_model", None, "other_model"])
            self.assertEqual(posted.status_code, 405)
            for code, status in (("NOT_FOUND", 404), ("NO_ACTIVE_VERSION", 404),
                                 ("DB_UNAVAILABLE", 503), ("RESULT_TOO_LARGE", 500)):
                failing = FakeRepository(GraphError(code, "reason"))
                with patch("text2sql_mcp.http_app.repository", return_value=failing):
                    response = client.get("/snapshot", headers=authorized)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {"error": {"code": code, "message": "reason"}})


if __name__ == "__main__":
    unittest.main()
