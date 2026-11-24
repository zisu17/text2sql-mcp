"""Opt-in checks against the published Olist graph."""

import os
import unittest

from text2sql_mcp.config import Config
from text2sql_mcp.graph_repository import GraphRepository


@unittest.skipUnless(os.getenv("GRAPH_INTEGRATION_TEST") == "1", "requires local Neo4j")
class PublishedGraphTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = GraphRepository(Config.from_env())

    @classmethod
    def tearDownClass(cls):
        cls.repo.close()

    def test_value_and_join_lineage_are_distinct(self):
        result = self.repo.get_metric_lineage("order_lines", "revenue")
        fields = {item["ref"]: item for item in result["fields"]}
        self.assertIn({"source_id": "olist", "catalog": "main", "schema": "main",
                       "table": "order_items", "column": "price"},
                      fields["order_lines.price"]["value_columns"])
        self.assertIn({"source_id": "olist", "catalog": "main", "schema": "main",
                       "table": "orders", "column": "order_status"},
                      fields["order_lines.order_status"]["value_columns"])
        impact = self.repo.get_column_impact("olist", "main", "main", "orders", "order_id")
        self.assertIn("row_set_candidate", {item["impact"] for item in impact["impacts"]})

    def test_all_six_queries_and_active_version(self):
        models = self.repo.list_models()
        self.assertTrue(models["models"])
        version = models["models"][0]["version_id"]
        calls = [self.repo.search_definitions("매출"), self.repo.get_dataset("order_lines"),
                 self.repo.get_model_snapshot(),
                 self.repo.get_metric_lineage("order_lines", "revenue"),
                 self.repo.get_column_impact("olist", "main", "main", "orders", "order_id"),
                 self.repo.list_join_rules()]
        self.assertTrue(all(item["version_id"] == version for item in calls))
        self.assertEqual(calls[1]["source_kind"], "sql_query")
        self.assertEqual(calls[1]["tables_basis"], "field_lineage")
        self.assertGreater(len(calls[1]["tables"]), 1)
        orders = next(item for item in calls[2]["definition"]["datasets"] if item["name"] == "orders")
        self.assertTrue({"order_id", "order_status"} <= {f["name"] for f in orders["fields"]})
        self.assertEqual(len(calls[5]["rules"]), 9)
        self.assertIn("n_to_1", calls[5]["rules"][0])

    def test_calculated_dataset_definitions_include_registered_sources(self):
        result = self.repo.get_model_snapshot()
        datasets = {item["name"]: item for item in result["definition"]["datasets"]}
        source = datasets["order_lines"]["source"]
        for name in ("order_items", "orders"):
            table = datasets[name]["source"]
            self.assertIn(table, source)


if __name__ == "__main__":
    unittest.main()
