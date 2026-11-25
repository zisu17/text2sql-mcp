"""Definition search: BM25 full-text relevance first, exact ref/name matches on top, and the old
substring search when the graph was published before the full-text index."""

import unittest
from types import SimpleNamespace

from neo4j.exceptions import Neo4jError

from text2sql_mcp.contracts import GraphError
from text2sql_mcp.graph_repository import FULLTEXT_INDEX, GraphRepository, lucene_query


class Record:
    def __init__(self, value):
        self.value = value

    def data(self):
        return self.value


NODES = [  # ref, name, label, BM25 score the index would give for "price", synonyms
    ("order_items.price", "price", "Field", 0.9, []),
    ("order_lines.price", "price", "Field", 1.4, []),
    ("order_lines.price_band", "price_band", "Field", 0.2, []),
    ("price", "price", "Metric", 0.5, []),
    ("aaa.unit_price", "unit_price", "Field", 0.1, []),
    ("all_order_count", "all_order_count", "Metric", 0.3, ["주문 건수"]),
    ("payment_count", "payment_count", "Metric", 0.8, ["결제 건수"]),
]


class FakeTx:
    """Applies the ordering the Cypher text asks for, so the test fails if the ORDER BY loses it."""

    def __init__(self):
        self.cypher, self.params = None, None

    def run(self, cypher, params):
        if "ActiveVersion" in cypher:
            return [Record({"version": "v1", "roots": 1})]
        if "count(n) AS total" in cypher:
            return [Record({"total": len(NODES) + 1000})]  # other models' definitions share the index
        self.cypher, self.params = cypher, params
        q = params["query"]
        bm25 = "SEARCH n IN" in cypher
        hits = [n for n in NODES if q in n[0].lower() or q in n[1].lower() or any(q in s for s in n[4])
                or (bm25 and set(q.split()) & {w for s in n[4] for w in s.split()})]

        ranks_synonyms = "any(s IN coalesce(n.synonyms,[]) WHERE toLower(s) = $query) THEN 2" in cypher

        def key(n):
            rank = 0 if n[0].lower() == q else 1 if n[1].lower() == q else 2 if ranks_synonyms and q in n[4] else 3
            if bm25:
                return (rank, -n[3], n[0]) if "ORDER BY rank, score DESC" in cypher else (0, 0, n[0])
            return (rank, 0, n[0]) if "ORDER BY rank, ref, id" in cypher else (0, 0, n[0])
        rows = []
        for n in sorted(hits, key=key)[:params["take"]]:
            row = {"id": n[0], "ref": n[0], "name": n[1], "description": "", "synonyms": n[4],
                   "status": None, "labels": ["Definition", n[2]]}
            rows.append(Record({**row, "score": n[3]} if bm25 else row))
        return rows


def _repo(tx, missing_index=False):
    repo = GraphRepository.__new__(GraphRepository)
    repo.config = SimpleNamespace(tenant="olist", default_model="olist_sales")

    def read(action, limit=None):
        if missing_index and action.__name__ == "bm25":
            raise GraphError("INDEX_UNAVAILABLE", "Full-text index is missing or not online")
        return action(tx)
    repo._read = read
    return repo


class SearchTest(unittest.TestCase):
    def test_defaults_to_one_result_and_advertises_more(self):
        for missing_index in (False, True):
            with self.subTest(missing_index=missing_index):
                tx = FakeTx()
                result = _repo(tx, missing_index=missing_index).search_definitions("price")
                self.assertEqual([i["ref"] for i in result["items"]], ["price"])
                self.assertTrue(result["has_more"])
                self.assertEqual(tx.params["take"], 2)

    def test_bm25_orders_exact_matches_then_relevance(self):
        tx = FakeTx()
        result = _repo(tx).search_definitions("Price", limit=3)
        self.assertEqual(result["search"], "bm25")
        # exact ref, then exact name ordered by BM25 score, then the rest
        self.assertEqual([i["ref"] for i in result["items"]], ["price", "order_lines.price", "order_items.price"])
        self.assertEqual(result["items"][1]["score"], 1.4)
        self.assertTrue(result["has_more"])
        self.assertIn(f"FULLTEXT INDEX {FULLTEXT_INDEX} FOR $lucene", tx.cypher)
        self.assertIn("CYPHER 25", tx.cypher)
        self.assertIn("n.tenant = $tenant AND n.model = $model AND n.version = $version", tx.cypher)
        self.assertEqual((tx.params["lucene"], tx.params["query"]), ("price", "price"))
        # Candidates cover every indexed node, so the tenant/model filter never runs on a cut-off list.
        self.assertIn("LIMIT $candidates", tx.cypher)
        self.assertEqual(tx.params["candidates"], len(NODES) + 1000)

    def test_exact_synonym_beats_a_higher_bm25_score(self):
        result = _repo(FakeTx()).search_definitions("주문 건수", limit=2)
        self.assertEqual([i["ref"] for i in result["items"]], ["all_order_count", "payment_count"])
        substring = _repo(FakeTx(), missing_index=True).search_definitions("주문 건수", limit=2)
        self.assertEqual([i["ref"] for i in substring["items"]], ["all_order_count"])

    def test_missing_index_falls_back_to_substring_search(self):
        tx = FakeTx()
        result = _repo(tx, missing_index=True).search_definitions("Price", limit=3)
        self.assertEqual(result["search"], "substring")
        self.assertEqual([i["ref"] for i in result["items"]], ["price", "order_items.price", "order_lines.price"])
        self.assertNotIn("score", result["items"][0])
        self.assertIn("CONTAINS $query", tx.cypher)

    def test_other_errors_do_not_fall_back(self):
        repo = _repo(FakeTx())
        repo._read = lambda action, limit=None: (_ for _ in ()).throw(GraphError("DB_UNAVAILABLE", "down"))
        with self.assertRaisesRegex(GraphError, "DB_UNAVAILABLE"):
            repo.search_definitions("price")

    def test_missing_index_error_is_recognised(self):
        error = Neo4jError._hydrate_neo4j(code="Neo.ClientError.Statement.ArgumentError",
                                          message=f"The index '{FULLTEXT_INDEX}' does not exist.")

        class Session:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute_read(self, work):
                raise error

        repo = GraphRepository.__new__(GraphRepository)
        repo.config = SimpleNamespace(tenant="olist", default_model="olist_sales", database="neo4j")
        repo.driver = SimpleNamespace(session=lambda database: Session())
        with self.assertLogs("text2sql_mcp.graph_repository", "WARNING"):
            with self.assertRaisesRegex(GraphError, "INDEX_UNAVAILABLE"):
                repo._read(lambda tx: {})

    def test_lucene_query_escapes_operators(self):
        self.assertEqual(lucene_query("매출 revenue"), "매출 OR revenue")
        self.assertEqual(lucene_query('a+b (c) "d" AND x:y'), 'a\\+b OR \\(c\\) OR \\"d\\" OR and OR x\\:y')
        self.assertEqual(lucene_query("order_lines.revenue"), "order_lines.revenue")


if __name__ == "__main__":
    unittest.main()
