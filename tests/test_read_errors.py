"""Neo4j failures map to stable GraphError codes, and logs carry no query values."""

import unittest
from types import SimpleNamespace

from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

from text2sql_mcp.contracts import GraphError
from text2sql_mcp.graph_repository import GraphRepository

SECRET = "param-value-4f9c"


def server_error(code):
    # The driver builds the matching subclass from the server code (AuthError, CypherSyntaxError, ...).
    return Neo4jError._hydrate_neo4j(code=code, message=f"failed near '{SECRET}'")


class FailingSession:
    def __init__(self, error):
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute_read(self, work):
        raise self.error


def _repo(error):
    repo = GraphRepository.__new__(GraphRepository)
    repo.config = SimpleNamespace(tenant="olist", default_model="olist_sales", database="neo4j")
    repo.driver = SimpleNamespace(session=lambda database: FailingSession(error))
    return repo


class ReadErrorTest(unittest.TestCase):
    def test_codes(self):
        cases = [
            (server_error("Neo.ClientError.Statement.SyntaxError"), "SCHEMA_INCOMPATIBLE"),
            (server_error("Neo.ClientError.Statement.ParameterMissing"), "SCHEMA_INCOMPATIBLE"),
            (server_error("Neo.ClientError.Schema.ConstraintValidationFailed"), "SCHEMA_INCOMPATIBLE"),
            (server_error("Neo.ClientError.Security.Unauthorized"), "DB_UNAVAILABLE"),
            (server_error("Neo.ClientError.Security.Forbidden"), "DB_UNAVAILABLE"),
            (server_error("Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"), "DB_UNAVAILABLE"),
            (server_error("Neo.TransientError.General.DatabaseUnavailable"), "DB_UNAVAILABLE"),
            (server_error("Neo.DatabaseError.General.UnknownError"), "DB_UNAVAILABLE"),
            (ServiceUnavailable("routing failed"), "DB_UNAVAILABLE"),
            (SessionExpired("gone"), "DB_UNAVAILABLE"),
            (OSError("network down"), "DB_UNAVAILABLE"),
        ]
        for error, code in cases:
            with self.subTest(error=type(error).__name__, code=getattr(error, "code", None)):
                with self.assertLogs("text2sql_mcp.graph_repository", "WARNING") as logs:
                    with self.assertRaises(GraphError) as raised:
                        _repo(error).list_models()
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertFalse(any(SECRET in line for line in logs.output))

    def test_log_names_the_server_code(self):
        with self.assertLogs("text2sql_mcp.graph_repository", "WARNING") as logs:
            with self.assertRaises(GraphError):
                _repo(server_error("Neo.ClientError.Security.Unauthorized")).list_models()
        self.assertIn("Neo.ClientError.Security.Unauthorized (AuthError)", logs.output[0])


if __name__ == "__main__":
    unittest.main()
