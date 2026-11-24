"""Stable errors and physical identifiers returned by tools."""

from __future__ import annotations

import re


class GraphError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)

    def __str__(self) -> str:
        return f"{self.code}: {super().__str__()}"


def column_key(row: dict, prefix: str = "") -> dict:
    return {key: row[prefix + key] for key in ("source_id", "catalog", "schema", "table", "column")}


# Same rule as the text2sql plugin's engine.SOURCE_NAME: a dataset source that is a bare
# [catalog.][schema.]table name is a table; anything else is a SQL query. Keep the two in sync.
SOURCE_NAME = re.compile(r"[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*){0,2}")


def is_table_source(source: str) -> bool:
    return bool(SOURCE_NAME.fullmatch(source.strip()))
