"""Check the public HTTP boundary without connecting to Neo4j."""

import os
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient


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
            self.assertEqual(len(listed.json()["result"]["tools"]), 6)


if __name__ == "__main__":
    unittest.main()
