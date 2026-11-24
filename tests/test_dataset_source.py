"""get_dataset decides table vs SQL source with the plugin's rule (engine.SOURCE_NAME)."""

import unittest
from types import SimpleNamespace

from text2sql_mcp.contracts import GraphError, is_table_source
from text2sql_mcp.graph_repository import GraphRepository


# Same case table as semantic-definitions tests/test_plan.py SOURCE_CASES.
SOURCE_CASES = [
    ("orders", True),
    ("  main.orders  ", True),
    ("warehouse.main.orders", True),
    ("a.b.c.d", False),
    ("SELECT order_id FROM orders", False),
    ("select order_id from orders", False),
    ("WITH t AS (SELECT 1 AS x) SELECT x FROM t", False),
    ("(SELECT order_id FROM orders)", False),
    ("-- 주문\nSELECT order_id FROM orders", False),
    ("/* 주문 */ SELECT order_id FROM orders", False),
]


class Record:
    def __init__(self, value):
        self.value = value

    def data(self):
        return self.value


class FakeTx:
    def __init__(self, source):
        self.source = source
        self.tables_query = None

    def run(self, cypher, params):
        if "ActiveVersion" in cypher:
            return [Record({"version": "v1", "roots": 1})]
        if "d.source AS source" in cypher:
            return [Record({"id": "dataset:orders", "ref": "orders", "name": "orders",
                            "description": "", "primary_key": ["order_id"], "source": self.source})]
        if "DERIVED_FROM" in cypher:
            self.tables_query = cypher
        return []


def _dataset(source):
    repo = GraphRepository.__new__(GraphRepository)
    repo.config = SimpleNamespace(tenant="olist", default_model="olist_sales")
    tx = FakeTx(source)
    repo._read = lambda action, limit=None: action(tx)
    return repo.get_dataset("orders"), tx


class DatasetSourceTest(unittest.TestCase):
    def test_rule_matches_plugin_cases(self):
        for source, table in SOURCE_CASES:
            with self.subTest(source=source):
                self.assertIs(is_table_source(source), table)

    def test_source_kind_and_lineage_query(self):
        for source, table in SOURCE_CASES:
            with self.subTest(source=source):
                result, tx = _dataset(source)
                self.assertEqual(result["source_kind"], "table" if table else "sql_query")
                self.assertEqual(result["source_sql"], None if table else source)
                self.assertEqual(result["tables_basis"], "direct" if table else "field_lineage")
                # A table source reads Dataset->Table; a SQL source walks field lineage instead.
                self.assertEqual("-[:HAS]->" in tx.tables_query, not table)

    def test_missing_source_is_schema_incompatible(self):
        for source in (None, "", "   "):
            with self.subTest(source=source):
                with self.assertRaisesRegex(GraphError, "SCHEMA_INCOMPATIBLE"):
                    _dataset(source)


if __name__ == "__main__":
    unittest.main()
