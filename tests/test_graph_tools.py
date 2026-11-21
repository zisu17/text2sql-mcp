"""Exercise the MCP boundary without requiring a Neo4j instance."""

import asyncio
import unittest
from unittest.mock import patch

from mcp import Client

from text2sql_mcp.contracts import GraphError
from text2sql_mcp.server import mcp


EXPECTED_TOOLS = {
    "graph_list_models", "graph_search_definitions", "graph_get_dataset",
    "graph_get_metric_lineage", "graph_get_column_impact",
    "graph_list_join_rules", "graph_get_concept_mapping",
}


class FakeRepository:
    def search_definitions(self, **kwargs):
        return {"tenant": "olist", "model": "olist_sales", "version_id": "v1",
                "items": [{"kind": "Metric", "ref": "revenue"}], "has_more": False}

    def get_dataset(self, **kwargs):
        raise GraphError("NOT_FOUND", "dataset was not found in the active graph")


class MCPToolsTest(unittest.TestCase):
    def test_tools_list_and_structured_result(self):
        async def run():
            async with Client(mcp) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual({tool.name for tool in tools}, EXPECTED_TOOLS)
                self.assertTrue(all(not any(word in tool.name for word in
                                            ("write", "load", "publish", "cypher")) for tool in tools))
                with patch("text2sql_mcp.server.repository", return_value=FakeRepository()):
                    result = await client.call_tool("graph_search_definitions", {"query": "매출"})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["items"][0]["ref"], "revenue")
        asyncio.run(run())

    def test_domain_error_is_tool_error(self):
        async def run():
            async with Client(mcp) as client:
                with patch("text2sql_mcp.server.repository", return_value=FakeRepository()):
                    result = await client.call_tool("graph_get_dataset", {"dataset": "absent"})
                self.assertTrue(result.is_error)
                self.assertIn("NOT_FOUND", result.content[0].text)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
