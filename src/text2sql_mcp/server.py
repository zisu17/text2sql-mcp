"""Read-only MCP tools for the active metadata graph."""

from __future__ import annotations

import atexit
from functools import lru_cache
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .config import Config
from .contracts import GraphError
from .graph_repository import GraphRepository


mcp = MCPServer("text2sql-graph")


@lru_cache(maxsize=1)
def repository() -> GraphRepository:
    return GraphRepository(Config.from_env())


def _close_repository() -> None:
    if repository.cache_info().currsize:
        repository().close()


atexit.register(_close_repository)


def _call(method: Callable[..., dict], **kwargs: Any) -> dict:
    try:
        return method(**kwargs)
    except GraphError as error:
        raise ToolError(str(error)) from error


@mcp.tool(structured_output=True)
def graph_list_models() -> dict[str, Any]:
    """List active semantic models in the configured tenant."""
    return _call(repository().list_models)


@mcp.tool(structured_output=True)
def graph_search_definitions(query: str, kinds: list[str] | None = None,
                             model: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Search active datasets, fields, and metrics by text."""
    return _call(repository().search_definitions, query=query, kinds=kinds, model=model, limit=limit)


@mcp.tool(structured_output=True)
def graph_get_dataset(dataset: str, model: str | None = None) -> dict[str, Any]:
    """Get a dataset's fields, metrics, and physical source tables."""
    return _call(repository().get_dataset, dataset=dataset, model=model)


@mcp.tool(structured_output=True)
def graph_get_metric_lineage(dataset: str, metric: str,
                             model: str | None = None) -> dict[str, Any]:
    """Get value lineage and separate row-set dependencies for a metric."""
    return _call(repository().get_metric_lineage, dataset=dataset, metric=metric, model=model)


@mcp.tool(structured_output=True)
def graph_get_column_impact(source_id: str, catalog: str, schema: str,
                            table: str, column: str, model: str | None = None) -> dict[str, Any]:
    """Find metrics affected by a fully qualified physical column."""
    return _call(repository().get_column_impact, source_id=source_id, catalog=catalog,
                 schema=schema, table=table, column=column, model=model)


@mcp.tool(structured_output=True)
def graph_list_join_rules(dataset: str | None = None, model: str | None = None) -> dict[str, Any]:
    """List declared dataset joins and their status and cardinality claim."""
    return _call(repository().list_join_rules, dataset=dataset, model=model)


@mcp.tool(structured_output=True)
def graph_find_join_path(datasets: list[str], model: str | None = None,
                         max_hops: int = 4) -> dict[str, Any]:
    """Find the shortest approved join path between each adjacent pair of datasets.

    A pair without an approved path is listed in `unreachable` with `unapproved_paths` over draft or
    not-N:1 joins; using one is allowed but must be disclosed. `ambiguous` means several equally
    short paths exist; `reverse` marks a hop that multiplies rows. Advisory only.
    """
    return _call(repository().find_join_path, datasets=datasets, model=model, max_hops=max_hops)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
