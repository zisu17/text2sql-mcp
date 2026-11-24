"""An active graph version can supply an executable model without a file."""

import json
import unittest
from types import SimpleNamespace

from text2sql_mcp.contracts import GraphError
from text2sql_mcp.graph_repository import RESULT_LIMIT, SNAPSHOT_LIMIT, GraphRepository


class Record:
    def __init__(self, value):
        self.value = value

    def data(self):
        return self.value


class FakeTx:
    def __init__(self, description="orders"):
        self.description = description

    def run(self, cypher, params):
        if "ActiveVersion" in cypher:
            rows = [{"version": "v1", "roots": 1}]
        elif "RETURN m.ref AS name" in cypher:
            rows = [{"name": "olist_sales", "description": "sales", "ossie_version": "0.3.0",
                     "revision": 2, "instructions": ""}]
        elif "RETURN d.ref AS name" in cypher:
            rows = [{"name": "orders", "source": "orders", "primary_key": ["order_id"],
                     "unique_keys": [], "description": self.description, "instructions": ""}]
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
        repo._read = lambda action, limit=None: action(FakeTx())
        result = repo.get_model_snapshot()
        self.assertEqual(result["version_id"], "v1")
        self.assertEqual(result["definition"]["datasets"][0]["source"], "orders")
        metric = result["definition"]["datasets"][0]["metrics"][0]
        self.assertEqual(metric["expression"]["sql"], "COUNT(order_id)")
        self.assertEqual(metric["nonportable_extensions"][0]["data"]["status"], "approved")
        self.assertLess(len(json.dumps(result)), 128 * 1024)


class FakeSession:
    def __init__(self, tx):
        self.tx = tx

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute_read(self, work):
        return work(self.tx)


def _repo(tx):
    """A repository whose real _read (size limit included) runs against a fake driver."""
    repo = GraphRepository.__new__(GraphRepository)
    repo.config = SimpleNamespace(tenant="olist", default_model="olist_sales", database="neo4j")
    repo.driver = SimpleNamespace(session=lambda database: FakeSession(tx))
    return repo


class CountingTx(FakeTx):
    def __init__(self):
        super().__init__()
        self.queries = 0

    def run(self, cypher, params):
        self.queries += 1
        return super().run(cypher, params)


class SnapshotReuseTest(unittest.TestCase):
    def test_same_active_version_is_built_once(self):
        tx = CountingTx()
        repo = _repo(tx)
        first = repo.get_model_snapshot()
        built = tx.queries
        self.assertEqual(repo.get_model_snapshot(), first)
        self.assertEqual(tx.queries - built, 1)  # only the ActiveVersion read

    def test_oversized_snapshot_is_not_kept(self):
        repo = _repo(FakeTx("x" * (SNAPSHOT_LIMIT + 1)))
        with self.assertRaises(GraphError):
            repo.get_model_snapshot()
        self.assertEqual(repo._snapshots, {})


class SnapshotLimitTest(unittest.TestCase):
    def test_snapshot_may_exceed_the_tool_limit(self):
        result = _repo(FakeTx("x" * (RESULT_LIMIT + 1))).get_model_snapshot()
        self.assertGreater(len(json.dumps(result)), RESULT_LIMIT)

    def test_snapshot_over_one_mebibyte_is_rejected(self):
        with self.assertRaisesRegex(GraphError, "RESULT_TOO_LARGE.*exceeds 1 MiB"):
            _repo(FakeTx("x" * (SNAPSHOT_LIMIT + 1))).get_model_snapshot()

    def test_tools_keep_the_128_kib_limit(self):
        repo = _repo(None)
        with self.assertRaisesRegex(GraphError, "RESULT_TOO_LARGE.*128 KiB"):
            repo._read(lambda tx: {"blob": "x" * (RESULT_LIMIT + 1)})
