"""Lineage keeps every role of a metric->field edge and falls back to the old single `role`."""

import unittest
from types import SimpleNamespace

from text2sql_mcp.graph_repository import GraphRepository


class Record:
    def __init__(self, value):
        self.value = value

    def data(self):
        return self.value


def col(table, column):
    return {"source_id": "olist", "catalog": "main", "schema": "main", "table": table, "column": column}


EDGES = [  # field ref, roles (None: graph published before roles), role, physical column
    ("orders.order_status", ["measure", "condition"], "measure", col("orders", "order_status")),
    ("orders.order_id", None, "join", col("orders", "order_id")),
    ("order_lines.order_id", ["measure", "join"], "measure", col("order_items", "order_id")),
    ("order_lines.price", None, None, col("order_items", "price")),
]


class FakeTx:
    def run(self, cypher, params):
        if "ActiveVersion" in cypher:
            return [Record({"version": "v1", "roots": 1})]
        if "m.expression AS expression" in cypher:
            return [Record({"id": "metric:orders.m", "ref": "m", "dataset": "orders", "name": "m",
                            "description": "", "expression": "x", "status": "approved"})]
        if "RETURN c.name AS name" in cypher:
            return [Record({"name": params["column"]})]
        if "f.ref AS field" in cypher:
            return [Record({"dataset": "orders", "metric": "m", "status": "approved", "field": ref,
                            "roles": roles, "role": role}) for ref, roles, role, _ in EDGES]
        return [Record({"id": ref, "ref": ref, "roles": roles, "role": role,
                        **{("column" if k == "column" else k): v for k, v in c.items()}})
                for ref, roles, role, c in EDGES]


def _repo():
    repo = GraphRepository.__new__(GraphRepository)
    repo.config = SimpleNamespace(tenant="olist", default_model="olist_sales")
    repo._read = lambda action, limit=None: action(FakeTx())
    return repo


class LineageRolesTest(unittest.TestCase):
    def test_metric_lineage_reads_roles_with_fallback(self):
        result = _repo().get_metric_lineage("orders", "m")
        fields = {f["ref"]: f for f in result["fields"]}
        self.assertEqual(fields["orders.order_status"]["roles"], ["measure", "condition"])
        self.assertEqual(fields["orders.order_id"]["roles"], ["join"])  # role only (old graph)
        self.assertEqual(fields["order_lines.price"]["roles"], ["unknown"])
        self.assertEqual(fields["orders.order_id"]["value_columns"], [])
        # A field that is both a value and a join key is kept on both sides.
        self.assertEqual(fields["order_lines.order_id"]["value_columns"], [col("order_items", "order_id")])
        self.assertEqual([c["column"] for c in result["row_set_candidates"]],
                         [col("orders", "order_id"), col("order_items", "order_id")])

    def test_column_impact_reads_roles_with_fallback(self):
        impacts = {i["field"]: i for i in _repo().get_column_impact(
            "olist", "main", "main", "orders", "order_id")["impacts"]}
        self.assertEqual((impacts["orders.order_id"]["impact"], impacts["orders.order_id"]["row_set_candidate"]),
                         ("row_set_candidate", True))
        self.assertEqual((impacts["order_lines.order_id"]["impact"], impacts["order_lines.order_id"]["row_set_candidate"]),
                         ("value", True))
        self.assertEqual((impacts["orders.order_status"]["impact"], impacts["orders.order_status"]["row_set_candidate"]),
                         ("value", False))


if __name__ == "__main__":
    unittest.main()
