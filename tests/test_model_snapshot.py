"""An active graph version can supply an executable model without a file."""

import json
import unittest
from types import SimpleNamespace

from text2sql_mcp.graph_repository import GraphRepository


class Record:
    def __init__(self, value):
        self.value = value

    def data(self):
        return self.value


class FakeTx:
    def run(self, cypher, params):
        if "ActiveVersion" in cypher:
            rows = [{"version": "v1", "roots": 1}]
        elif "RETURN m.ref AS name" in cypher:
            rows = [{"name": "olist_sales", "description": "sales", "ossie_version": "0.3.0",
                     "revision": 2, "instructions": ""}]
        elif "RETURN d.ref AS name" in cypher:
            rows = [{"name": "orders", "source": "orders", "primary_key": ["order_id"],
                     "unique_keys": [], "description": "orders", "instructions": ""}]
        elif "RETURN f.dataset AS dataset" in cypher:
            rows = [{"dataset": "orders", "name": "order_id", "expression": "order_id",
                     "datatype": "String", "description": "key", "instructions": ""}]
        elif "RETURN m.dataset AS dataset" in cypher:
            rows = [{"dataset": "orders", "name": "order_count", "expression": "COUNT(order_id)",
                     "datatype": "Integer", "description": "count", "instructions": "",
                     "status": "approved"}]
        elif "RETURN j.name AS name" in cypher:
            rows = []
        else:
            raise AssertionError(cypher)
        return [Record(row) for row in rows]


class ModelSnapshotTest(unittest.TestCase):
    def test_snapshot_contains_active_executable_definition(self):
        repo = GraphRepository.__new__(GraphRepository)
        repo.config = SimpleNamespace(tenant="olist", default_model="olist_sales")
        repo._read = lambda action: action(FakeTx())
        result = repo.get_model_snapshot()
        self.assertEqual(result["version_id"], "v1")
        self.assertEqual(result["definition"]["datasets"][0]["source"], "orders")
        metric = result["definition"]["datasets"][0]["metrics"][0]
        self.assertEqual(metric["expression"]["sql"], "COUNT(order_id)")
        self.assertEqual(metric["nonportable_extensions"][0]["data"]["status"], "approved")
        self.assertLess(len(json.dumps(result)), 128 * 1024)
