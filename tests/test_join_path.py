"""Join paths follow only the joins t2s accepts: approved and N:1."""

import re
import unittest
from types import SimpleNamespace

from text2sql_mcp.contracts import GraphError
from text2sql_mcp.graph_repository import GraphRepository


class Record:
    def __init__(self, value):
        self.value = value

    def data(self):
        return self.value


JOINS = [  # name, from, to, status, n_to_1
    ("items_orders", "order_items", "orders", "approved", True),
    ("orders_customers", "orders", "customers", "approved", True),
    ("items_products", "order_items", "products", "approved", True),
    ("items_sellers", "order_items", "sellers", "draft", True),
    ("orders_reviews", "orders", "reviews", "approved", False),
    ("payments_orders", "payments", "orders", "approved", True),
    ("payments_customers", "payments", "customers", "approved", True),
]
DATASETS = {name for join in JOINS for name in join[1:3]} | {"geolocation"}


class GraphTx:
    """Answers the path query from JOINS, applying only the filters the Cypher text asks for."""

    def __init__(self):
        self.queries = []

    def run(self, cypher, params):
        self.queries.append((cypher, params))
        if "ActiveVersion" in cypher:
            return [Record({"version": "v1", "roots": 1})]
        if "d.ref IN $refs" in cypher:
            return [Record({"ref": ref}) for ref in params["refs"] if ref in DATASETS]
        limit = int(re.search(r"JOINS\*\.\.(\d+)", cypher).group(1))
        joins = [j for j in JOINS
                 if ("r.status = 'approved'" not in cypher or j[3] == "approved")
                 and ("r.n_to_1 = true" not in cypher or j[4])]
        frontier, found = [(params["start"], [])], []
        for _ in range(limit):
            following = []
            for at, path in frontier:
                for join in joins:
                    if join in path or at not in join[1:3]:
                        continue
                    other = join[2] if at == join[1] else join[1]
                    (found if other == params["end"] else following).append((other, [*path, join]))
            if found:
                break
            frontier = following
        return [Record({"hops": [{"name": j[0], "from": j[1], "to": j[2], "from_columns": ["k"],
                                  "to_columns": ["k"], "n_to_1": j[4], "status": j[3]} for j in path]})
                for _, path in sorted(found, key=lambda item: [j[0] for j in item[1]])]


def _repo(tx):
    repo = GraphRepository.__new__(GraphRepository)
    repo.config = SimpleNamespace(tenant="olist", default_model="olist_sales")
    repo._read = lambda action, limit=None: action(tx)
    return repo


class JoinPathTest(unittest.TestCase):
    def test_path_reports_direction_per_hop(self):
        result = _repo(GraphTx()).find_join_path(["orders", "products"])
        segment = result["segments"][0]
        self.assertEqual((result["version_id"], result["unreachable"]), ("v1", []))
        self.assertFalse(segment["ambiguous"])
        self.assertEqual([(h["name"], h["from"], h["to"], h["reverse"]) for h in segment["paths"][0]],
                         [("items_orders", "order_items", "orders", True),
                          ("items_products", "order_items", "products", False)])

    def test_each_adjacent_pair_is_a_segment(self):
        result = _repo(GraphTx()).find_join_path(["order_items", "orders", "customers"])
        self.assertEqual([(s["from"], s["to"], len(s["paths"][0])) for s in result["segments"]],
                         [("order_items", "orders", 1), ("orders", "customers", 1)])

    def test_draft_only_pair_is_unreachable_with_the_draft_path(self):
        result = _repo(GraphTx()).find_join_path(["order_items", "sellers"])
        self.assertEqual(result["segments"], [])
        [pair] = result["unreachable"]
        self.assertEqual((pair["from"], pair["to"], pair["has_more"]), ("order_items", "sellers", False))
        self.assertEqual([[(h["name"], h["status"]) for h in p] for p in pair["unapproved_paths"]],
                         [[("items_sellers", "draft")]])

    def test_approved_join_without_key_target_is_unreachable(self):
        result = _repo(GraphTx()).find_join_path(["orders", "reviews"])
        [pair] = result["unreachable"]
        self.assertEqual([[(h["name"], h["n_to_1"]) for h in p] for p in pair["unapproved_paths"]],
                         [[("orders_reviews", False)]])

    def test_equally_short_paths_are_all_returned(self):
        unique = _repo(GraphTx()).find_join_path(["order_items", "payments"])["segments"][0]
        self.assertEqual((unique["ambiguous"], len(unique["paths"])), (False, 1))
        JOINS.append(("items_customers", "order_items", "customers", "approved", True))
        try:
            segment = _repo(GraphTx()).find_join_path(["order_items", "payments"])["segments"][0]
        finally:
            JOINS.pop()
        self.assertTrue(segment["ambiguous"])
        self.assertEqual([[h["name"] for h in path] for path in segment["paths"]],
                         [["items_customers", "payments_customers"], ["items_orders", "payments_orders"]])

    def test_hop_limit_is_validated_then_inlined_and_values_are_parameters(self):
        tx = GraphTx()
        result = _repo(tx).find_join_path(["products", "customers"], max_hops=2)
        self.assertEqual([(u["from"], u["to"], u["unapproved_paths"]) for u in result["unreachable"]],
                         [("products", "customers", [])])
        cypher, params = tx.queries[-2]  # the approved-only query; the fallback runs after it
        self.assertIn("JOINS*..2]", cypher)
        self.assertNotIn("products", cypher)
        self.assertEqual((params["start"], params["end"]), ("products", "customers"))

    def test_invalid_input_is_rejected(self):
        repo = _repo(GraphTx())
        for datasets, hops in ((["orders"], 4), (["orders"] * 7, 4), (["orders", ""], 4),
                               ("orders", 4), (["orders", "orders"], 4),
                               (["orders", "customers"], 0), (["orders", "customers"], 7),
                               (["orders", "customers"], True), (["orders", "customers"], "4; MATCH (n)")):
            with self.assertRaisesRegex(GraphError, "INVALID_INPUT", msg=(datasets, hops)):
                repo.find_join_path(datasets, max_hops=hops)

    def test_unknown_dataset_is_not_found(self):
        with self.assertRaisesRegex(GraphError, "NOT_FOUND.*absent"):
            _repo(GraphTx()).find_join_path(["orders", "absent"])
        result = _repo(GraphTx()).find_join_path(["orders", "geolocation"])
        self.assertEqual([(u["from"], u["to"], u["unapproved_paths"]) for u in result["unreachable"]],
                         [("orders", "geolocation", [])])


if __name__ == "__main__":
    unittest.main()
