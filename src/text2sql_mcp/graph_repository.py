"""Fixed, parameterized Cypher for the active semantic graph."""

from __future__ import annotations

import json
from typing import Any, Callable

from neo4j import GraphDatabase, unit_of_work
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from .config import Config
from .contracts import GraphError, column_key


ACTIVE = """
MATCH (a:ActiveVersion {tenant:$tenant, model:$model})
OPTIONAL MATCH (a)-[:POINTS_TO]->(root:Model {tenant:$tenant, model:$model})
RETURN a.version_id AS version_id, collect(root.version_id) AS targets
"""


def _rows(tx: Any, cypher: str, **parameters: Any) -> list[dict]:
    return [record.data() for record in tx.run(cypher, parameters)]


def _one(rows: list[dict], kind: str) -> dict:
    if not rows:
        raise GraphError("NOT_FOUND", f"{kind} was not found in the active graph")
    if len(rows) != 1:
        raise GraphError("SCHEMA_INCOMPATIBLE", f"Duplicate {kind} in the active graph")
    return rows[0]


class GraphRepository:
    def __init__(self, config: Config):
        self.config = config
        self.driver = GraphDatabase.driver(config.uri, auth=(config.user, config.password),
                                           connection_timeout=3, max_transaction_retry_time=5)

    def close(self) -> None:
        self.driver.close()

    def _read(self, action: Callable[[Any], dict]) -> dict:
        try:
            with self.driver.session(database=self.config.database) as session:
                result = session.execute_read(unit_of_work(timeout=5)(action))
        except (Neo4jError, ServiceUnavailable, OSError) as error:
            raise GraphError("DB_UNAVAILABLE", "Neo4j query failed or timed out") from error
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 128 * 1024:
            raise GraphError("RESULT_TOO_LARGE", "Graph result exceeds 128 KiB")
        return result

    def _scope(self, tx: Any, model: str | None) -> dict:
        scope = {"tenant": self.config.tenant, "model": model or self.config.default_model}
        rows = _rows(tx, ACTIVE, **scope)
        if not rows:
            raise GraphError("NO_ACTIVE_VERSION", "No active graph for this model")
        version = rows[0]["version_id"]
        if not version or rows[0]["targets"] != [version]:
            raise GraphError("SCHEMA_INCOMPATIBLE", "ActiveVersion pointer is missing or inconsistent")
        scope["version_id"] = version
        return scope

    def list_models(self) -> dict:
        def work(tx: Any) -> dict:
            rows = _rows(tx, """
                MATCH (a:ActiveVersion {tenant:$tenant})-[:POINTS_TO]->(m:Model)
                WHERE a.version_id = m.version_id AND a.model = m.model AND a.tenant = m.tenant
                RETURN a.model AS model, a.version_id AS version_id,
                       m.name AS name, m.description AS description
                ORDER BY model LIMIT 51
            """, tenant=self.config.tenant)
            return {"tenant": self.config.tenant, "models": rows[:50], "has_more": len(rows) > 50}
        return self._read(work)

    def search_definitions(self, query: str, kinds: list[str] | None = None,
                           model: str | None = None, limit: int = 20) -> dict:
        if not query.strip():
            raise GraphError("INVALID_INPUT", "query must not be empty")
        allowed = {"Dataset", "Field", "Metric", "Concept"}
        selected = kinds or sorted(allowed)
        if set(selected) - allowed or not 1 <= limit <= 50:
            raise GraphError("INVALID_INPUT", "Invalid kinds or limit (1..50)")

        def work(tx: Any) -> dict:
            scope = self._scope(tx, model)
            rows = _rows(tx, """
                MATCH (n:Definition {tenant:$tenant, model:$model, version_id:$version_id})
                WHERE any(kind IN labels(n) WHERE kind IN $kinds)
                  AND (toLower(coalesce(n.ref,'')) CONTAINS $query
                       OR toLower(coalesce(n.name,'')) CONTAINS $query
                       OR toLower(coalesce(n.description,'')) CONTAINS $query
                       OR any(s IN coalesce(n.synonyms,[]) WHERE toLower(s) CONTAINS $query))
                RETURN n.id AS id, n.ref AS ref, n.name AS name,
                       n.description AS description, n.synonyms AS synonyms,
                       n.status AS status, labels(n) AS labels
                ORDER BY ref, id LIMIT $take
            """, **scope, kinds=selected, query=query.strip().lower(), take=limit + 1)
            items = [{**{k: v for k, v in row.items() if k != "labels"},
                      "kind": next(kind for kind in selected if kind in row["labels"])}
                     for row in rows[:limit]]
            return {**scope, "items": items, "has_more": len(rows) > limit}
        return self._read(work)

    def get_dataset(self, dataset: str, model: str | None = None) -> dict:
        def work(tx: Any) -> dict:
            scope = self._scope(tx, model)
            p = {**scope, "dataset": dataset}
            ds = _one(_rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version_id:$version_id,ref:$dataset})
                RETURN d.id AS id, d.ref AS ref, d.name AS name,
                       d.description AS description, d.primary_key AS primary_key
            """, **p), "dataset")
            fields = _rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version_id:$version_id,ref:$dataset})
                      -[:HAS]->(f:Field {tenant:$tenant,model:$model,version_id:$version_id})
                RETURN f.id AS id, f.ref AS ref, f.name AS name, f.description AS description,
                       f.datatype AS datatype, f.expression AS expression ORDER BY f.ref
            """, **p)
            metrics = _rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version_id:$version_id,ref:$dataset})
                      -[:HAS]->(m:Metric {tenant:$tenant,model:$model,version_id:$version_id})
                RETURN m.id AS id, m.ref AS ref, m.name AS name,
                       m.description AS description, m.status AS status ORDER BY m.ref
            """, **p)
            sources = _rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version_id:$version_id,ref:$dataset})
                OPTIONAL MATCH (d)-[:HAS_SOURCE_QUERY]->(q:SourceQuery {tenant:$tenant,model:$model,version_id:$version_id})
                RETURN q.sql AS sql, q.id AS query_id
            """, **p)
            source = _one(sources, "dataset source")
            tables = _rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version_id:$version_id,ref:$dataset})
                OPTIONAL MATCH (d)-[:HAS_SOURCE_QUERY]->(q:SourceQuery {tenant:$tenant,model:$model,version_id:$version_id})
                WITH d, q
                MATCH (reader)-[:READS_FROM]->(t:Table {tenant:$tenant})
                WHERE reader = d OR reader = q
                RETURN DISTINCT t.source_id AS source_id, t.catalog AS catalog,
                       t.schema AS schema, t.name AS table, t.kind AS kind
                ORDER BY source_id, catalog, schema, table
            """, **p)
            return {**scope, "dataset": ds, "fields": fields, "metrics": metrics,
                    "source_kind": "sql_query" if source["query_id"] else "table",
                    "source_sql": source["sql"], "tables": tables}
        return self._read(work)

    def get_metric_lineage(self, dataset: str, metric: str,
                           model: str | None = None) -> dict:
        def work(tx: Any) -> dict:
            scope = self._scope(tx, model)
            p = {**scope, "dataset": dataset, "metric": metric}
            m = _one(_rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version_id:$version_id,ref:$dataset})
                      -[:HAS]->(m:Metric {tenant:$tenant,model:$model,version_id:$version_id,ref:$metric})
                RETURN m.id AS id, m.ref AS ref, m.dataset AS dataset,
                       m.name AS name, m.description AS description,
                       m.expression AS expression, m.status AS status
            """, **p), "metric")
            field_rows = _rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version_id:$version_id,ref:$dataset})
                      -[:HAS]->(m:Metric {tenant:$tenant,model:$model,version_id:$version_id,ref:$metric})
                      -[u:USES_FIELD]->(f:Field {tenant:$tenant,model:$model,version_id:$version_id})
                OPTIONAL MATCH (f)-[:VALUE_DERIVED_FROM]->(c:Column {tenant:$tenant})
                RETURN f.id AS id, f.ref AS ref, u.roles AS roles,
                       c.source_id AS source_id, c.catalog AS catalog,
                       c.schema AS schema, c.table AS table, c.name AS column
                ORDER BY ref, source_id, catalog, schema, table, column
            """, **p)
            fields: dict[str, dict] = {}
            for row in field_rows:
                item = fields.setdefault(row["id"], {"id": row["id"], "ref": row["ref"],
                                                     "roles": row["roles"] or [], "value_columns": []})
                if row["column"] is not None:
                    key = column_key(row)
                    if key not in item["value_columns"]:
                        item["value_columns"].append(key)
            row_set = _rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version_id:$version_id,ref:$dataset})
                      -[:HAS]->(:Metric {tenant:$tenant,model:$model,version_id:$version_id,ref:$metric})
                MATCH (d)-[:HAS_SOURCE_QUERY]->(q:SourceQuery {tenant:$tenant,model:$model,version_id:$version_id})
                      -[r:ROW_DEPENDS_ON]->(c:Column {tenant:$tenant})
                RETURN c.source_id AS source_id, c.catalog AS catalog,
                       c.schema AS schema, c.table AS table, c.name AS column,
                       r.roles AS roles ORDER BY source_id,catalog,schema,table,column
            """, **p)
            return {**scope, "metric": m, "fields": list(fields.values()),
                    "row_set_candidates": [{"column": column_key(row), "roles": row["roles"] or []}
                                           for row in row_set]}
        return self._read(work)

    def get_column_impact(self, source_id: str, catalog: str, schema: str,
                          table: str, column: str, model: str | None = None) -> dict:
        def work(tx: Any) -> dict:
            scope = self._scope(tx, model)
            p = {**scope, "source_id": source_id, "catalog": catalog,
                 "schema": schema, "table": table, "column": column}
            _one(_rows(tx, """
                MATCH (c:Column {tenant:$tenant,source_id:$source_id,catalog:$catalog,
                                 schema:$schema,table:$table,name:$column})
                RETURN c.name AS name
            """, **p), "column")
            value = _rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version_id:$version_id})
                      -[:HAS]->(m:Metric {tenant:$tenant,model:$model,version_id:$version_id})
                      -[u:USES_FIELD]->(f:Field {tenant:$tenant,model:$model,version_id:$version_id})
                      -[:VALUE_DERIVED_FROM]->(c:Column {tenant:$tenant,source_id:$source_id,
                          catalog:$catalog,schema:$schema,table:$table,name:$column})
                RETURN d.ref AS dataset, m.ref AS metric, m.status AS status,
                       f.ref AS field, u.roles AS roles ORDER BY dataset,metric,field
            """, **p)
            row_set = _rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version_id:$version_id})
                      -[:HAS]->(m:Metric {tenant:$tenant,model:$model,version_id:$version_id})
                MATCH (d)-[:HAS_SOURCE_QUERY]->(q:SourceQuery {tenant:$tenant,model:$model,version_id:$version_id})
                      -[r:ROW_DEPENDS_ON]->(c:Column {tenant:$tenant,source_id:$source_id,
                          catalog:$catalog,schema:$schema,table:$table,name:$column})
                RETURN d.ref AS dataset, m.ref AS metric, m.status AS status,
                       r.roles AS roles ORDER BY dataset,metric
            """, **p)
            impacts = [{**row, "impact": "value"} for row in value]
            impacts += [{**row, "impact": "row_set_candidate"} for row in row_set]
            return {**scope, "column": {k: p[k] for k in ("source_id", "catalog", "schema", "table", "column")},
                    "impacts": impacts}
        return self._read(work)

    def list_join_rules(self, dataset: str | None = None,
                        model: str | None = None) -> dict:
        def work(tx: Any) -> dict:
            scope = self._scope(tx, model)
            rows = _rows(tx, """
                MATCH (root:Model {tenant:$tenant,model:$model,version_id:$version_id})
                      -[:HAS_JOIN_RULE]->(j:JoinRule {tenant:$tenant,model:$model,version_id:$version_id})
                      -[:FROM_DATASET]->(src:Dataset {tenant:$tenant,model:$model,version_id:$version_id})
                MATCH (j)-[:TO_DATASET]->(dst:Dataset {tenant:$tenant,model:$model,version_id:$version_id})
                WHERE $dataset IS NULL OR src.ref=$dataset OR dst.ref=$dataset
                RETURN j.id AS id, j.ref AS ref, j.name AS name, j.description AS description,
                       j.status AS status, j.from_columns AS from_columns,
                       j.to_columns AS to_columns, j.declared_n_to_1 AS declared_n_to_1,
                       j.data_verified AS data_verified,
                       src.ref AS from_dataset, dst.ref AS to_dataset ORDER BY ref
            """, **scope, dataset=dataset)
            return {**scope, "rules": rows}
        return self._read(work)

    def get_concept_mapping(self, concept: str, model: str | None = None) -> dict:
        def work(tx: Any) -> dict:
            scope = self._scope(tx, model)
            p = {**scope, "concept": concept}
            item = _one(_rows(tx, """
                MATCH (c:Concept {tenant:$tenant,model:$model,version_id:$version_id,ref:$concept})
                RETURN c.id AS id, c.ref AS ref, c.description AS description,
                       c.concept_type AS concept_type
            """, **p), "concept")
            rows = _rows(tx, """
                MATCH (concept:Concept {tenant:$tenant,model:$model,version_id:$version_id,ref:$concept})
                      -[:MAPS_TO]->(f:Field {tenant:$tenant,model:$model,version_id:$version_id})
                OPTIONAL MATCH (f)-[:VALUE_DERIVED_FROM]->(c:Column {tenant:$tenant})
                RETURN f.id AS id, f.ref AS ref, c.source_id AS source_id,
                       c.catalog AS catalog, c.schema AS schema,
                       c.table AS table, c.name AS column
                ORDER BY ref,source_id,catalog,schema,table,column
            """, **p)
            fields: dict[str, dict] = {}
            for row in rows:
                field = fields.setdefault(row["id"], {"id": row["id"], "ref": row["ref"],
                                                     "value_columns": []})
                if row["column"] is not None:
                    key = column_key(row)
                    if key not in field["value_columns"]:
                        field["value_columns"].append(key)
            return {**scope, "concept": item, "fields": list(fields.values())}
        return self._read(work)
