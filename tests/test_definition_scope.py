"""t2s gets only the part of the active definition a command needs, never the whole model."""

import json
import unittest
from types import SimpleNamespace

from text2sql_mcp.contracts import GraphError
from text2sql_mcp.graph_repository import DEFINITION_LIMIT, GraphRepository, scope_datasets


def join(name, src, dst, status="approved", cols=("id",)):
    return {"name": name, "from_dataset": src, "to_dataset": dst, "from_columns": list(cols),
            "to_columns": list(cols), "status": status}


# Olist-like N→1 joins: order lines and payments point at orders, orders at customers.
JOINS = [
    join("lines_items", "order_lines", "order_items"), join("items_orders", "order_items", "orders"),
    join("items_products", "order_items", "products"), join("orders_customers", "orders", "customers"),
    join("payments_orders", "order_payments", "orders"), join("reviews_orders", "order_reviews", "orders"),
    join("items_sellers", "order_items", "sellers", status="draft"),
]


class ScopeTest(unittest.TestCase):
    def test_one_dataset_stays_alone(self):
        # Many datasets point at orders; none is needed to count orders.
        self.assertEqual(scope_datasets(JOINS, {"orders"}), {"orders"})

    def test_path_datasets_are_kept(self):
        self.assertEqual(scope_datasets(JOINS, {"order_lines", "customers"}),
                         {"order_lines", "order_items", "orders", "customers"})

    def test_common_base_is_kept_when_no_wanted_dataset_reaches_all(self):
        # customers and products only meet through order_items (and order_lines, which reaches it).
        self.assertEqual(scope_datasets(JOINS, {"customers", "products"}),
                         {"customers", "products", "order_items", "orders", "order_lines"})

    def test_both_paths_are_kept_so_ambiguity_is_seen(self):
        joins = JOINS + [join("lines_orders", "order_lines", "orders")]
        self.assertEqual(scope_datasets(joins, {"order_lines", "orders"}), {"order_lines", "order_items", "orders"})

    def test_draft_joins_count(self):
        self.assertEqual(scope_datasets(JOINS, {"order_items", "sellers"}), {"order_items", "sellers"})

    def test_hops_are_capped(self):
        chain = [join(f"j{i}", f"d{i}", f"d{i + 1}") for i in range(6)]
        self.assertEqual(scope_datasets(chain, {"d0", "d4"}), {"d0", "d1", "d2", "d3", "d4"})
        self.assertEqual(scope_datasets(chain, {"d0", "d5"}), {"d0", "d5"})  # 5 joins: t2s does not follow it

    def test_nothing_wanted(self):
        self.assertEqual(scope_datasets(JOINS, set()), set())


class Record:
    def __init__(self, value):
        self.value = value

    def data(self):
        return self.value


DATASETS = [("customers", "customers"), ("order_items", "order_items"), ("orders", "main.orders"),
            ("order_lines", "SELECT oi.order_id, p.id FROM order_items AS oi\nLEFT JOIN products AS p ON p.id = oi.id"),
    ("products", "products"),
            ("order_payments", "order_payments")]


class FakeTx:
    """Answers the repository's fixed Cypher. Detail queries honour the $names filter."""

    def __init__(self, description="orders"):
        self.description, self.queries = description, []

    def run(self, cypher, params):
        self.queries.append(cypher)
        names = set(params.get("names") or ())
        if "ActiveVersion" in cypher:
            rows = [{"version": "v1", "roots": 1}]
        elif "RETURN m.ref AS name" in cypher:
            rows = [{"name": "olist_sales", "description": "sales", "ossie_version": "0.3.0",
                     "revision": 12, "instructions": ""}]
        elif "RETURN d.ref AS name, d.source AS source ORDER BY name" in cypher:
            rows = [{"name": n, "source": s} for n, s in DATASETS]
        elif "CASE WHEN m.dataset IS NULL" in cypher:
            rows = [{"dataset": "orders", "name": "order_count", "expression": None},
                    {"dataset": "order_lines", "name": "revenue", "expression": None},
                    {"dataset": None, "name": "aov", "expression": "SUM(order_lines.price) / COUNT(orders.order_id)"}]
        elif "RETURN j.name AS name" in cypher:
            rows = JOINS
        elif "RETURN d.ref AS name" in cypher:
            rows = [{"name": n, "source": s, "primary_key": ["id"], "unique_keys": [],
                     "description": self.description, "instructions": ""} for n, s in DATASETS if n in names]
        elif "RETURN f.dataset AS dataset" in cypher:
            rows = [{"dataset": n, "name": "id", "expression": "id", "datatype": "String",
                     "description": "key", "instructions": ""} for n, _ in DATASETS if n in names]
        elif "RETURN m.dataset AS dataset" in cypher:
            rows = [{"dataset": "orders", "name": "order_count", "expression": "COUNT(id)", "datatype": "Integer",
                     "description": "count", "instructions": "", "status": "approved"}] if "orders" in names else []
            rows += [{"dataset": None, "name": n, "expression": "SUM(order_lines.price) / COUNT(orders.order_id)",
                      "datatype": "Decimal", "description": "", "instructions": "", "status": "draft"}
                     for n in params.get("top") or ()]
        else:
            raise AssertionError(cypher)
        return [Record(row) for row in rows]


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


def names(result):
    return sorted(d["name"] for d in result["definition"]["datasets"])


class DefinitionTest(unittest.TestCase):
    def test_tables_and_metrics_pick_their_datasets(self):
        result = _repo(FakeTx()).get_definition(tables=["MAIN.ORDERS", "geolocation"], metrics=["revenue"])
        self.assertEqual((result["version_id"], result["revision"]), ("v1", 12))
        self.assertEqual(result["defined_tables"], ["orders"])  # geolocation is outside the definition
        # products comes along: order_lines reads it in its SQL source, and t2s checks those tables.
        self.assertEqual(names(result), ["order_items", "order_lines", "orders", "products"])
        self.assertEqual(result["datasets_total"], len(DATASETS))
        rel = sorted(r["name"] for r in result["definition"]["relationships"])
        self.assertEqual(rel, ["items_orders", "items_products", "lines_items"])  # only joins inside the scope
        self.assertEqual(result["definition"]["nonportable_extensions"][0]["data"]["revision"], 12)

    def test_dataset_names_ignore_case_and_unknowns_are_reported(self):
        result = _repo(FakeTx()).get_definition(datasets=["Customers", "nope"], metrics=["orders.nope", "ghost"])
        self.assertEqual(names(result), ["customers"])
        self.assertEqual(result["unknown_datasets"], ["nope"])
        self.assertEqual(result["unknown_metrics"], ["orders.nope", "ghost"])

    def test_model_metric_brings_the_datasets_it_reads(self):
        result = _repo(FakeTx()).get_definition(metrics=["aov"])
        self.assertEqual(names(result), ["order_items", "order_lines", "orders", "products"])
        self.assertEqual([m["name"] for m in result["definition"]["metrics"]], ["aov"])

    def test_metadata_only(self):
        tx = FakeTx()
        result = _repo(tx).get_definition(tables=["orders", "x"], with_definition=False)
        self.assertNotIn("definition", result)
        self.assertEqual(result["defined_tables"], ["orders"])
        self.assertFalse(any("WHERE f.dataset IN $names" in q for q in tx.queries))

    def test_index_is_read_once_per_version(self):
        tx = FakeTx()
        repo = _repo(tx)
        repo.get_definition(datasets=["orders"])
        before = len(tx.queries)
        repo.get_definition(datasets=["orders"])
        index = [q for q in tx.queries[before:] if "ORDER BY name" in q and "d.source AS source ORDER BY" in q]
        self.assertEqual(index, [])

    def test_size_follows_the_request(self):
        result = _repo(FakeTx("x" * 1000)).get_definition(datasets=["orders"])
        self.assertLess(len(json.dumps(result)), 4000)
        with self.assertRaisesRegex(GraphError, "RESULT_TOO_LARGE.*1 MiB"):
            _repo(FakeTx("x" * (DEFINITION_LIMIT + 1))).get_definition(datasets=["orders"])


if __name__ == "__main__":
    unittest.main()
