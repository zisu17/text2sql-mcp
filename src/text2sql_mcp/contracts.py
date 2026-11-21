"""Stable errors and physical identifiers returned by tools."""

from __future__ import annotations


class GraphError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def __str__(self) -> str:
        return f"{self.code}: {super().__str__()}"


def column_key(row: dict, prefix: str = "") -> dict:
    return {key: row[prefix + key] for key in ("source_id", "catalog", "schema", "table", "column")}
