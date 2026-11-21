"""Connection and scope settings for the graph reader."""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Config:
    uri: str
    user: str
    password: str
    database: str
    tenant: str
    default_model: str

    @classmethod
    def from_env(cls) -> "Config":
        required = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD")
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise ValueError("Missing environment variables: " + ", ".join(missing))
        return cls(
            uri=os.environ["NEO4J_URI"],
            user=os.environ["NEO4J_USER"],
            password=os.environ["NEO4J_PASSWORD"],
            database=os.getenv("NEO4J_DATABASE", "neo4j"),
            tenant=os.getenv("GRAPH_TENANT", "olist"),
            default_model=os.getenv("GRAPH_DEFAULT_MODEL", "olist_sales"),
        )
