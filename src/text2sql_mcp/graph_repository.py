"""Fixed Cypher reads of the active semantic lineage graph."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from neo4j import GraphDatabase, unit_of_work
from neo4j.exceptions import DriverError, Neo4jError

from .config import Config
from .contracts import GraphError, column_key, is_table_source


log = logging.getLogger(__name__)
# A query this server sent that no longer fits the published graph. Every other failure
# (auth, transaction timeout, transient, driver/connection) means Neo4j could not answer.
_QUERY_ERRORS = ("Neo.ClientError.Statement.", "Neo.ClientError.Schema.")

# The full-text index semantic-definitions creates at publish time (Lucene, BM25 relevance).
FULLTEXT_INDEX = "definition_text"
_INDEX_UNAVAILABLE = re.compile(r"\bindex\b.*\b(does not exist|not online|populating)\b", re.I)
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')

RESULT_LIMIT = 128 * 1024  # tool results enter an LLM context
SNAPSHOT_LIMIT = 1024 * 1024  # the snapshot is read by t2s, never by an LLM
SNAPSHOT_VERSIONS = 8  # ponytail: oldest-first eviction, an LRU if many models are served at once


def _rows(tx: Any, cypher: str, **params: Any) -> list[dict]:
    return [record.data() for record in tx.run(cypher, params)]


def _one(rows: list[dict], kind: str) -> dict:
    if not rows:
        raise GraphError("NOT_FOUND", f"{kind} was not found in the active graph")
    if len(rows) != 1:
        raise GraphError("SCHEMA_INCOMPATIBLE", f"Duplicate {kind} in the active graph")
    return rows[0]


def _roles(row: dict) -> list[str]:
    """Every role of a metric->field edge. Graphs published before `roles` only have `role`."""
    roles = [r for r in row.get("roles") or [row.get("role")] if r]
    return roles or ["unknown"]


def lucene_query(text: str) -> str:
    """Words of the user's text as a Lucene OR query. Operators and special characters are escaped
    (lower-casing also keeps AND/OR/NOT from acting as operators), so input is searched as plain words."""
    words = [_LUCENE_SPECIAL.sub(r"\\\1", word) for word in text.lower().split()]
    return " OR ".join(words)


class GraphRepository:
    def __init__(self, config: Config):
        self.config = config
        self.driver = GraphDatabase.driver(config.uri, auth=(config.user, config.password),
                                           connection_timeout=3, max_transaction_retry_time=5)

    def close(self) -> None:
        self.driver.close()

    def _read(self, action: Callable[[Any], dict], limit: int = RESULT_LIMIT) -> dict:
        try:
            with self.driver.session(database=self.config.database) as session:
                result = session.execute_read(unit_of_work(timeout=5)(action))
        except Neo4jError as error:
            # Log the server code and class only: messages can quote query parameter values.
            log.warning("Neo4j read failed: %s (%s)", error.code, type(error).__name__)
            if getattr(error, "gql_status", None) == "22N69" or _INDEX_UNAVAILABLE.search(error.message or ""):
                # 22N69: index does not exist (graph published before the full-text index).
                raise GraphError("INDEX_UNAVAILABLE", "Full-text index is missing or not online") from error
            if (error.code or "").startswith(_QUERY_ERRORS):
                raise GraphError("SCHEMA_INCOMPATIBLE",
                                 "Graph query does not match the published graph schema") from error
            raise GraphError("DB_UNAVAILABLE", "Neo4j query failed or timed out") from error
        except (DriverError, OSError) as error:
            log.warning("Neo4j connection failed (%s)", type(error).__name__)
            raise GraphError("DB_UNAVAILABLE", "Neo4j query failed or timed out") from error
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > limit:
            size = f"{limit // 1024 ** 2} MiB" if limit % 1024 ** 2 == 0 else f"{limit // 1024} KiB"
            raise GraphError("RESULT_TOO_LARGE", f"Graph result exceeds {size}")
        return result

    def _scope(self, tx: Any, model: str | None) -> dict:
        scope = {"tenant": self.config.tenant, "model": model or self.config.default_model}
        rows = _rows(tx, """
            MATCH (a:ActiveVersion {tenant:$tenant, model:$model})
            OPTIONAL MATCH (root:Model {tenant:$tenant, model:$model})
            WHERE root.version = a.commit
            RETURN a.commit AS version, count(root) AS roots
        """, **scope)
        if not rows:
            raise GraphError("NO_ACTIVE_VERSION", "No active graph for this model")
        if not rows[0]["version"] or rows[0]["roots"] != 1:
            raise GraphError("SCHEMA_INCOMPATIBLE", "ActiveVersion commit has no unique Model")
        scope["version"] = rows[0]["version"]
        return scope

    @staticmethod
    def _metadata(scope: dict) -> dict:
        # Preserve the MCP response key while reading the writer's version/commit.
        return {"tenant": scope["tenant"], "model": scope["model"],
                "version_id": scope["version"]}

    def list_models(self) -> dict:
        def work(tx: Any) -> dict:
            rows = _rows(tx, """
                MATCH (a:ActiveVersion {tenant:$tenant})
                OPTIONAL MATCH (m:Model {tenant:$tenant})
                WHERE m.model = a.model AND m.version = a.commit
                RETURN a.model AS model, a.commit AS version_id,
                       head(collect(m.name)) AS name,
                       head(collect(m.description)) AS description,
                       count(m) AS roots
                ORDER BY model LIMIT 51
            """, tenant=self.config.tenant)
            if any(not row["version_id"] or row["roots"] != 1 for row in rows):
                raise GraphError("SCHEMA_INCOMPATIBLE", "ActiveVersion commit has no unique Model")
            models = [{k: v for k, v in row.items() if k != "roots"} for row in rows[:50]]
            return {"tenant": self.config.tenant, "models": models,
                    "has_more": len(rows) > 50}
        return self._read(work)

    def search_definitions(self, query: str, kinds: list[str] | None = None,
                           model: str | None = None, limit: int = 3) -> dict:
        if not query.strip():
            raise GraphError("INVALID_INPUT", "query must not be empty")
        allowed = {"Dataset", "Field", "Metric"}
        selected = kinds or sorted(allowed)
        if set(selected) - allowed or not 1 <= limit <= 50:
            raise GraphError("INVALID_INPUT", "Invalid kinds or limit (1..50)")

        text = query.strip().lower()

        def items(rows: list[dict]) -> list[dict]:
            return [{**{k: v for k, v in row.items() if k != "labels"},
                     "kind": next(kind for kind in selected if kind in row["labels"])}
                    for row in rows[:limit]]

        def bm25(tx: Any) -> dict:
            """Lucene full-text search (BM25 relevance) over ref, name, description and synonyms."""
            scope = self._scope(tx, model)
            # One index covers every tenant/model/version and SEARCH applies its LIMIT before the WHERE
            # filter, so ask for as many hits as there are indexed nodes: nothing of this model is cut off.
            indexed = _rows(tx, "MATCH (n:Definition) RETURN count(n) AS total")[0]["total"]
            rows = _rows(tx, f"""
                CYPHER 25
                MATCH (n:Definition)
                  SEARCH n IN (FULLTEXT INDEX {FULLTEXT_INDEX} FOR $lucene LIMIT $candidates) SCORE AS score
                WHERE n.tenant = $tenant AND n.model = $model AND n.version = $version
                  AND any(kind IN labels(n) WHERE kind IN $kinds)
                WITH n, score, CASE WHEN toLower(coalesce(n.ref,'')) = $query THEN 0
                                    WHEN toLower(coalesce(n.name,'')) = $query THEN 1
                                    WHEN any(s IN coalesce(n.synonyms,[]) WHERE toLower(s) = $query) THEN 2
                                    ELSE 3 END AS rank
                RETURN n.id AS id, n.ref AS ref, n.name AS name,
                       n.description AS description, n.synonyms AS synonyms,
                       n.status AS status, labels(n) AS labels, round(score, 4) AS score
                ORDER BY rank, score DESC, ref, id LIMIT $take
            """, **scope, kinds=selected, query=text, lucene=lucene_query(text), take=limit + 1,
                         candidates=max(indexed, 1))
            return {**self._metadata(scope), "search": "bm25", "items": items(rows), "has_more": len(rows) > limit}

        def substring(tx: Any) -> dict:
            """Case-insensitive substring match, for graphs published before the full-text index."""
            scope = self._scope(tx, model)
            rows = _rows(tx, """
                MATCH (n:Definition {tenant:$tenant,model:$model,version:$version})
                WHERE any(kind IN labels(n) WHERE kind IN $kinds)
                  AND (toLower(coalesce(n.ref,'')) CONTAINS $query
                       OR toLower(coalesce(n.name,'')) CONTAINS $query
                       OR toLower(coalesce(n.description,'')) CONTAINS $query
                       OR any(s IN coalesce(n.synonyms,[]) WHERE toLower(s) CONTAINS $query))
                WITH n, CASE WHEN toLower(coalesce(n.ref,'')) = $query THEN 0
                             WHEN toLower(coalesce(n.name,'')) = $query THEN 1
                             WHEN any(s IN coalesce(n.synonyms,[]) WHERE toLower(s) = $query) THEN 2
                             ELSE 3 END AS rank
                RETURN n.id AS id, n.ref AS ref, n.name AS name,
                       n.description AS description, n.synonyms AS synonyms,
                       n.status AS status, labels(n) AS labels
                ORDER BY rank, ref, id LIMIT $take
            """, **scope, kinds=selected, query=text, take=limit + 1)
            return {**self._metadata(scope), "search": "substring", "items": items(rows), "has_more": len(rows) > limit}

        try:
            return self._read(bm25)
        except GraphError as error:
            if error.code != "INDEX_UNAVAILABLE":
                raise
            return self._read(substring)

    def get_dataset(self, dataset: str, model: str | None = None) -> dict:
        def work(tx: Any) -> dict:
            scope = self._scope(tx, model)
            p = {**scope, "dataset": dataset}
            ds = _one(_rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version:$version,ref:$dataset})
                RETURN d.id AS id, d.ref AS ref, d.name AS name,
                       d.description AS description, d.primary_key AS primary_key,
                       d.source AS source
            """, **p), "dataset")
            fields = _rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version:$version,ref:$dataset})
                      -[:HAS]->(f:Field {tenant:$tenant,model:$model,version:$version})
                RETURN f.id AS id, f.ref AS ref, f.name AS name, f.description AS description,
                       f.datatype AS datatype, f.expression AS expression ORDER BY f.ref
            """, **p)
            metrics = _rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version:$version,ref:$dataset})
                      -[:HAS]->(m:Metric {tenant:$tenant,model:$model,version:$version})
                RETURN m.id AS id, m.ref AS ref, m.name AS name,
                       m.description AS description, m.status AS status ORDER BY m.ref
            """, **p)
            if not isinstance(ds["source"], str) or not ds["source"].strip():
                raise GraphError("SCHEMA_INCOMPATIBLE", "Dataset has no source")
            source_kind = "table" if is_table_source(ds["source"]) else "sql_query"
            if source_kind == "table":
                tables = _rows(tx, """
                    MATCH (d:Dataset {tenant:$tenant,model:$model,version:$version,ref:$dataset})
                          -[:DERIVED_FROM]->(t:Table {tenant:$tenant})
                    RETURN DISTINCT t.source_id AS source_id, t.catalog AS catalog,
                           t.schema AS schema, t.name AS table, t.kind AS kind
                    ORDER BY source_id,catalog,schema,table
                """, **p)
            else:
                # SQL-source datasets have no Dataset->Table edge in the writer.
                tables = _rows(tx, """
                    MATCH (d:Dataset {tenant:$tenant,model:$model,version:$version,ref:$dataset})
                          -[:HAS]->(f:Field {tenant:$tenant,model:$model,version:$version})
                          -[:DERIVED_FROM]->(c:Column {tenant:$tenant})
                    MATCH (t:Table {tenant:$tenant})
                    WHERE t.source_id=c.source_id AND t.catalog=c.catalog
                      AND t.schema=c.schema AND t.name=c.table
                    RETURN DISTINCT t.source_id AS source_id, t.catalog AS catalog,
                           t.schema AS schema, t.name AS table, t.kind AS kind
                    ORDER BY source_id,catalog,schema,table
                """, **p)
            return {**self._metadata(scope), "dataset": {k: v for k, v in ds.items() if k != "source"},
                    "fields": fields, "metrics": metrics, "source_kind": source_kind,
                    "source_sql": ds["source"] if source_kind == "sql_query" else None,
                    "tables": tables,
                    "tables_basis": "field_lineage" if source_kind == "sql_query" else "direct"}
        return self._read(work)

    def get_model_snapshot(self, model: str | None = None) -> dict:
        """Return the active graph's executable semantic definition, in memory.

        A published version never changes, so its built definition is kept per (tenant, model, version):
        a repeat request costs one ActiveVersion read instead of six queries."""
        snapshots = self.__dict__.setdefault("_snapshots", {})

        def work(tx: Any) -> dict:
            scope = self._scope(tx, model)
            cached = snapshots.get((scope["tenant"], scope["model"], scope["version"]))
            if cached is not None:
                return cached
            root = _one(_rows(tx, """
                MATCH (m:Model {tenant:$tenant,model:$model,version:$version})
                RETURN m.ref AS name, m.description AS description,
                       m.ossie_version AS ossie_version, m.revision AS revision,
                       m.instructions AS instructions
            """, **scope), "model")
            datasets = _rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version:$version})
                RETURN d.ref AS name, d.source AS source,
                       d.primary_key AS primary_key, d.unique_keys AS unique_keys,
                       d.description AS description, d.instructions AS instructions
                ORDER BY name
            """, **scope)
            if not datasets:
                raise GraphError("SCHEMA_INCOMPATIBLE", "Active model has no datasets")
            fields = _rows(tx, """
                MATCH (f:Field {tenant:$tenant,model:$model,version:$version})
                RETURN f.dataset AS dataset, f.name AS name, f.expression AS expression,
                       f.datatype AS datatype, f.description AS description,
                       f.instructions AS instructions
                ORDER BY dataset,name
            """, **scope)
            metrics = _rows(tx, """
                MATCH (m:Metric {tenant:$tenant,model:$model,version:$version})
                RETURN m.dataset AS dataset, m.name AS name, m.expression AS expression,
                       m.datatype AS datatype, m.description AS description,
                       m.instructions AS instructions, m.status AS status
                ORDER BY dataset,name
            """, **scope)
            joins = _rows(tx, """
                MATCH (src:Dataset {tenant:$tenant,model:$model,version:$version})
                      -[j:JOINS]->(dst:Dataset {tenant:$tenant,model:$model,version:$version})
                RETURN j.name AS name, src.ref AS from_dataset, dst.ref AS to_dataset,
                       j.from_columns AS from_columns, j.to_columns AS to_columns,
                       j.status AS status
                ORDER BY name,from_dataset,to_dataset
            """, **scope)
            by_name = {}
            for item in datasets:
                ds = {"name": item["name"], "source": item["source"],
                      "primary_key": item["primary_key"] or [],
                      "unique_keys": [key.split(",") for key in item["unique_keys"] or []],
                      "description": item["description"] or "", "fields": [], "metrics": []}
                if item["instructions"]:
                    ds["ai_context"] = {"instructions": item["instructions"]}
                by_name[ds["name"]] = ds
            for item in fields:
                if item["dataset"] not in by_name:
                    raise GraphError("SCHEMA_INCOMPATIBLE", "Field has no dataset")
                field = {"name": item["name"], "expression": {"sql": item["expression"]},
                         "datatype": item["datatype"], "description": item["description"] or ""}
                if item["instructions"]:
                    field["ai_context"] = {"instructions": item["instructions"]}
                by_name[item["dataset"]]["fields"].append(field)
            top_metrics = []
            for item in metrics:
                metric = {"name": item["name"], "expression": {"sql": item["expression"]},
                          "datatype": item["datatype"], "description": item["description"] or "",
                          "nonportable_extensions": [{"vendor_name": "TEXT2SQL",
                                                      "data": {"status": item["status"]}}]}
                if item["instructions"]:
                    metric["ai_context"] = {"instructions": item["instructions"]}
                if item["dataset"]:
                    if item["dataset"] not in by_name:
                        raise GraphError("SCHEMA_INCOMPATIBLE", "Metric has no dataset")
                    by_name[item["dataset"]]["metrics"].append(metric)
                else:
                    top_metrics.append(metric)
            relationships = [{"name": j["name"], "from": j["from_dataset"], "to": j["to_dataset"],
                              "from_columns": j["from_columns"], "to_columns": j["to_columns"],
                              "nonportable_extensions": [{"vendor_name": "TEXT2SQL",
                                                          "data": {"status": j["status"]}}]}
                             for j in joins]
            definition = {"version": root["ossie_version"], "name": root["name"],
                          "description": root["description"] or "", "datasets": list(by_name.values()),
                          "metrics": top_metrics, "relationships": relationships,
                          "nonportable_extensions": [{"vendor_name": "TEXT2SQL",
                                                      "data": {"revision": root["revision"] or 0}}]}
            if root["instructions"]:
                definition["ai_context"] = {"instructions": root["instructions"]}
            return {**self._metadata(scope), "definition": definition}
        result = self._read(work, limit=SNAPSHOT_LIMIT)
        key = (result["tenant"], result["model"], result["version_id"])
        if key not in snapshots:  # only after the size check passed
            if len(snapshots) >= SNAPSHOT_VERSIONS:
                snapshots.pop(next(iter(snapshots)), None)
            snapshots[key] = result
        return result

    def get_metric_lineage(self, dataset: str, metric: str,
                           model: str | None = None) -> dict:
        def work(tx: Any) -> dict:
            scope = self._scope(tx, model)
            p = {**scope, "dataset": dataset, "metric": metric}
            m = _one(_rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version:$version,ref:$dataset})
                      -[:HAS]->(m:Metric {tenant:$tenant,model:$model,version:$version,ref:$metric})
                RETURN m.id AS id, m.ref AS ref, m.dataset AS dataset,
                       m.name AS name, m.description AS description,
                       m.expression AS expression, m.status AS status
            """, **p), "metric")
            rows = _rows(tx, """
                MATCH (m:Metric {tenant:$tenant,model:$model,version:$version,id:$metric_id})
                      -[r:DERIVED_FROM]->(f:Field {tenant:$tenant,model:$model,version:$version})
                OPTIONAL MATCH (f)-[:DERIVED_FROM]->(c:Column {tenant:$tenant})
                RETURN f.id AS id, f.ref AS ref, r.roles AS roles, r.role AS role,
                       c.source_id AS source_id, c.catalog AS catalog,
                       c.schema AS schema, c.table AS table, c.name AS column
                ORDER BY ref,source_id,catalog,schema,table,column
            """, **scope, metric_id=m["id"])
            fields: dict[str, dict] = {}
            row_set: list[dict] = []
            for row in rows:
                roles = _roles(row)
                item = fields.setdefault(row["id"], {"id": row["id"], "ref": row["ref"],
                                                     "roles": roles, "value_columns": []})
                if row["column"] is None:
                    continue
                key = column_key(row)
                if "join" in roles:
                    candidate = {"column": key, "roles": ["join"]}
                    if candidate not in row_set:
                        row_set.append(candidate)
                if any(role != "join" for role in roles) and key not in item["value_columns"]:
                    item["value_columns"].append(key)
            return {**self._metadata(scope), "metric": m, "fields": list(fields.values()),
                    "row_set_candidates": row_set}
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
            rows = _rows(tx, """
                MATCH (m:Metric {tenant:$tenant,model:$model,version:$version})
                      -[r:DERIVED_FROM]->(f:Field {tenant:$tenant,model:$model,version:$version})
                      -[:DERIVED_FROM]->(c:Column {tenant:$tenant,source_id:$source_id,
                          catalog:$catalog,schema:$schema,table:$table,name:$column})
                RETURN m.dataset AS dataset, m.ref AS metric, m.status AS status,
                       f.ref AS field, r.roles AS roles, r.role AS role
                ORDER BY dataset,metric,field
            """, **p)
            impacts = []
            for row in rows:
                roles = _roles(row)
                # A field that is both a value and a join key changes the value and the row set.
                impacts.append({"dataset": row["dataset"], "metric": row["metric"],
                                "status": row["status"], "field": row["field"], "roles": roles,
                                "impact": "value" if any(r != "join" for r in roles) else "row_set_candidate",
                                "row_set_candidate": "join" in roles})
            return {**self._metadata(scope),
                    "column": {k: p[k] for k in ("source_id", "catalog", "schema", "table", "column")},
                    "impacts": impacts}
        return self._read(work)

    def list_join_rules(self, dataset: str | None = None,
                        model: str | None = None) -> dict:
        def work(tx: Any) -> dict:
            scope = self._scope(tx, model)
            rows = _rows(tx, """
                MATCH (src:Dataset {tenant:$tenant,model:$model,version:$version})
                      -[j:JOINS]->(dst:Dataset {tenant:$tenant,model:$model,version:$version})
                WHERE $dataset IS NULL OR src.ref=$dataset OR dst.ref=$dataset
                RETURN j.name AS name, j.status AS status,
                       j.from_columns AS from_columns, j.to_columns AS to_columns,
                       j.n_to_1 AS n_to_1,
                       src.ref AS from_dataset, dst.ref AS to_dataset
                ORDER BY name,from_dataset,to_dataset
            """, **scope, dataset=dataset)
            return {**self._metadata(scope), "rules": rows}
        return self._read(work)

    @staticmethod
    def _paths(tx: Any, scope: dict, start: str, end: str, max_hops: int,
               approved_only: bool) -> tuple[list[list[dict]], bool]:
        """Shortest paths between two datasets (at most 5), and whether more exist."""
        # A variable-length bound cannot be a Cypher parameter; max_hops is a validated int.
        where = ("WHERE all(r IN relationships(p) WHERE r.status = 'approved' AND r.n_to_1 = true)"
                 if approved_only else "")
        rows = _rows(tx, f"""
            MATCH (s:Dataset {{tenant:$tenant,model:$model,version:$version,ref:$start}}),
                  (t:Dataset {{tenant:$tenant,model:$model,version:$version,ref:$end}}),
                  p = allShortestPaths((s)-[:JOINS*..{max_hops}]-(t))
            {where}
            WITH [r IN relationships(p) | r.name] AS names, p
            ORDER BY names LIMIT 6
            RETURN [r IN relationships(p) | {{name: r.name, from: startNode(r).ref,
                    to: endNode(r).ref, from_columns: r.from_columns,
                    to_columns: r.to_columns, n_to_1: r.n_to_1, status: r.status}}] AS hops
        """, **scope, start=start, end=end)
        paths = []
        for row in rows[:5]:
            at, hops = start, []
            for hop in row["hops"]:
                # Walking a join from its one-row side to its many-row side multiplies rows.
                reverse = hop["from"] != at
                at = hop["from"] if reverse else hop["to"]
                hops.append({**hop, "reverse": reverse})
            paths.append(hops)
        return paths, len(rows) > 5

    def find_join_path(self, datasets: list[str], model: str | None = None,
                       max_hops: int = 4) -> dict:
        """Shortest approved N:1 join paths between each adjacent pair of datasets.

        Prefers the joins t2s treats as approved (approved, target columns are a key). A pair with no
        such path lands in `unreachable` with the shortest paths over any declared join, so a caller
        can still join and disclose it. Advisory: `t2s validate` reports how a query's joins compare.
        """
        if (not isinstance(datasets, list) or not 2 <= len(datasets) <= 6
                or any(not isinstance(name, str) or not name.strip() for name in datasets)):
            raise GraphError("INVALID_INPUT", "datasets must list 2..6 dataset names")
        if any(a == b for a, b in zip(datasets, datasets[1:])):
            raise GraphError("INVALID_INPUT", "Adjacent datasets must differ")
        if type(max_hops) is not int or not 1 <= max_hops <= 6:
            raise GraphError("INVALID_INPUT", "max_hops must be an integer in 1..6")

        def work(tx: Any) -> dict:
            scope = self._scope(tx, model)
            known = {row["ref"] for row in _rows(tx, """
                MATCH (d:Dataset {tenant:$tenant,model:$model,version:$version})
                WHERE d.ref IN $refs RETURN d.ref AS ref
            """, **scope, refs=sorted(set(datasets)))}
            missing = sorted(set(datasets) - known)
            if missing:
                raise GraphError("NOT_FOUND", "Unknown datasets: " + ", ".join(missing))
            segments, unreachable = [], []
            for start, end in zip(datasets, datasets[1:]):
                paths, more = self._paths(tx, scope, start, end, max_hops, approved_only=True)
                if paths:
                    segments.append({"from": start, "to": end, "paths": paths,
                                     "ambiguous": len(paths) > 1, "has_more": more})
                    continue
                # No approved path: still show the shortest path over any declared join (draft or
                # not N:1) so the caller can join and say so, instead of stopping.
                other, more = self._paths(tx, scope, start, end, max_hops, approved_only=False)
                unreachable.append({"from": start, "to": end, "unapproved_paths": other,
                                    "has_more": more})
            return {**self._metadata(scope), "max_hops": max_hops,
                    "segments": segments, "unreachable": unreachable}
        return self._read(work)
