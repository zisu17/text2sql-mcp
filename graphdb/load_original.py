"""Load current Ossie definitions into the first blog post's six-node graph schema."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from neo4j import GraphDatabase
import sqlglot
from sqlglot import exp
import yaml


DEFINITION_LABELS = ("Model", "Dataset", "Field", "Metric")
CATALOG_LABELS = ("Table", "Column")
RELATIONSHIPS = ("HAS", "DERIVED_FROM", "JOINS", "REFERENCES")


def extension(item: dict) -> dict:
    for entry in item.get("nonportable_extensions") or []:
        if entry.get("vendor_name") == "TEXT2SQL":
            return entry.get("data") or {}
    return {}


def text_properties(item: dict) -> dict:
    context = item.get("ai_context") or {}
    props = {
        "name": item.get("name", ""),
        "description": item.get("description") or "",
        "synonyms": context.get("synonyms") or [],
        "instructions": context.get("instructions") or "",
    }
    if context.get("examples"):
        props["examples"] = context["examples"]
    return props


def expression_sql(item: dict) -> str:
    value = item.get("expression") or {}
    return value.get("sql", "") if isinstance(value, dict) else str(value)


def build_plan(definition: Path, sqlite_path: Path, tenant: str, source_id: str) -> dict:
    data = yaml.safe_load(definition.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Ossie definition must be a mapping")
    model = data["name"]
    version = sha256(definition.read_bytes()).hexdigest()
    nodes: dict[str, dict[str, dict]] = defaultdict(dict)
    edges: dict[str, dict[tuple[str, str, str], dict]] = defaultdict(dict)
    diagnostics: list[str] = []

    def definition_node(label: str, object_id: str, props: dict) -> str:
        uid = f"definition|{tenant}|{model}|{version}|{object_id}"
        nodes[label][uid] = {"tenant": tenant, "model": model, "version": version,
                             "id": object_id, **props}
        return uid

    def physical_node(label: str, table: str, column: str | None, props: dict) -> str:
        uid = f"catalog|{tenant}|{source_id}|main|main|{table}"
        if column is not None:
            uid += f"|{column}"
        nodes[label][uid] = {"tenant": tenant, "source_id": source_id,
                             "catalog": "main", "schema": "main", **props}
        return uid

    def edge(kind: str, start: str, end: str, props: dict | None = None) -> None:
        if kind not in RELATIONSHIPS:
            raise ValueError(f"Unexpected relationship: {kind}")
        key = (start, end, json.dumps(props or {}, sort_keys=True, ensure_ascii=False))
        edges[kind][key] = {"start": start, "end": end, "props": props or {}}

    table_nodes: dict[str, str] = {}
    column_nodes: dict[tuple[str, str], str] = {}
    foreign_keys: list[tuple[str, str, int, str, str]] = []
    db = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            columns = db.execute(f'PRAGMA table_info("{table}")').fetchall()
            table_uid = physical_node("Table", table, None,
                                      {"name": table, "kind": "table",
                                       "pk": [c[1] for c in columns if c[5]],
                                       "fqn_lc": f"main.main.{table}".lower()})
            table_nodes[table] = table_uid
            for ordinal, name, raw_type, not_null, _default, _pk in columns:
                column_uid = physical_node("Column", table, name,
                                            {"table": table, "name": name,
                                             "raw_type": raw_type or "UNKNOWN",
                                             "nullable": not bool(not_null),
                                             "ordinal": ordinal})
                column_nodes[(table, name)] = column_uid
                edge("HAS", table_uid, column_uid)
            for fk in db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
                foreign_keys.append((table, fk[2], fk[0], fk[3], fk[4]))
    finally:
        db.close()
    for table, target, fk_id, source_column, target_column in foreign_keys:
        if target in table_nodes:
            edge("REFERENCES", table_nodes[table], table_nodes[target],
                 {"name": f"{table}_fk_{fk_id}", "columns": [source_column],
                  "ref_columns": [target_column]})

    model_uid = definition_node("Model", f"model:{model}",
                                {"ref": model, "revision": extension(data).get("revision", 0),
                                 "ossie_version": str(data.get("version", "")),
                                 **text_properties(data)})
    dataset_uids: dict[str, str] = {}
    dataset_data: dict[str, dict] = {}
    fields: dict[tuple[str, str], str] = {}
    metric_names: set[str] = set()
    for dataset in data.get("datasets") or []:
        name, source = dataset["name"], dataset["source"]
        if name in dataset_uids:
            raise ValueError(f"Duplicate dataset: {name}")
        dataset_data[name] = dataset
        ds_uid = definition_node("Dataset", f"dataset:{name}",
                                 {"ref": name, "source": source,
                                  "primary_key": dataset.get("primary_key") or [],
                                  "unique_keys": [",".join(key) for key in dataset.get("unique_keys") or []],
                                  **text_properties(dataset)})
        dataset_uids[name] = ds_uid
        edge("HAS", model_uid, ds_uid)
        for field in dataset.get("fields") or []:
            field_name = field["name"]
            key = (name, field_name)
            if key in fields:
                raise ValueError(f"Duplicate field: {name}.{field_name}")
            field_uid = definition_node("Field", f"field:{name}.{field_name}",
                                        {"ref": f"{name}.{field_name}", "dataset": name,
                                         "expression": expression_sql(field),
                                         "datatype": field.get("datatype") or "",
                                         "is_key": field_name in (dataset.get("primary_key") or []),
                                         **text_properties(field)})
            fields[key] = field_uid
            edge("HAS", ds_uid, field_uid)
        for metric in dataset.get("metrics") or []:
            metric_name = metric["name"]
            if metric_name in metric_names:
                raise ValueError(f"Duplicate metric ref: {metric_name}")
            metric_names.add(metric_name)
            sql = expression_sql(metric)
            metric_uid = definition_node("Metric", f"metric:{metric_name}",
                                         {"ref": metric_name, "dataset": name,
                                          "expression": sql,
                                          "datatype": metric.get("datatype") or "",
                                          "status": extension(metric).get("status", "draft"),
                                          **text_properties(metric)})
            edge("HAS", ds_uid, metric_uid)
            try:
                parsed = sqlglot.parse_one(sql, read="snowflake")
            except Exception as error:
                diagnostics.append(f"metric {metric_name}: SQL parse failed: {error}")
                continue
            for column in parsed.find_all(exp.Column):
                field_uid = fields.get((name, column.name))
                if field_uid is None:
                    diagnostics.append(f"metric {metric_name}: unknown field {column.name}")
                    continue
                parent = column.parent
                role = "measure"
                while parent is not None:
                    if isinstance(parent, (exp.Where, exp.When)):
                        role = "condition"
                        break
                    parent = parent.parent
                edge("DERIVED_FROM", metric_uid, field_uid, {"role": role})

    for name, dataset in dataset_data.items():
        source = dataset["source"]
        if source.lstrip().upper().startswith("SELECT"):
            diagnostics.append(f"dataset {name}: initial schema does not resolve SQL sources to physical tables")
            continue
        table_uid = table_nodes.get(source)
        if table_uid is None:
            diagnostics.append(f"dataset {name}: physical table absent: {source}")
            continue
        edge("DERIVED_FROM", dataset_uids[name], table_uid)
        for field in dataset.get("fields") or []:
            sql = expression_sql(field).strip()
            column_uid = column_nodes.get((source, sql))
            if column_uid:
                edge("DERIVED_FROM", fields[(name, field["name"])], column_uid)
            else:
                diagnostics.append(f"field {name}.{field['name']}: physical column unresolved: {sql}")

    for relation in data.get("relationships") or []:
        from_name, to_name = relation["from"], relation["to"]
        if from_name not in dataset_uids or to_name not in dataset_uids:
            diagnostics.append(f"join {relation['name']}: endpoint dataset absent")
            continue
        target = dataset_data[to_name]
        target_keys = [target.get("primary_key") or [], *(target.get("unique_keys") or [])]
        to_columns = relation.get("to_columns") or []
        n_to_1 = any(list(key) == list(to_columns) for key in target_keys if key)
        edge("JOINS", dataset_uids[from_name], dataset_uids[to_name],
             {"name": relation["name"],
              "from_columns": relation.get("from_columns") or [],
              "to_columns": to_columns,
              "status": extension(relation).get("status", "draft"),
              "n_to_1": n_to_1})

    return {"tenant": tenant, "source_id": source_id, "model": model, "version": version,
            "nodes": nodes, "edges": edges, "diagnostics": diagnostics}


def summary(plan: dict) -> dict:
    return {"tenant": plan["tenant"], "model": plan["model"],
            "version": plan["version"],
            "nodes": {label: len(items) for label, items in plan["nodes"].items()},
            "relationships": {kind: len(items) for kind, items in plan["edges"].items()},
            "diagnostics": plan["diagnostics"]}


CONSTRAINTS = (
    "CREATE CONSTRAINT definition_key IF NOT EXISTS FOR (n:Definition) REQUIRE (n.tenant, n.model, n.version, n.id) IS UNIQUE",
    "CREATE CONSTRAINT table_key IF NOT EXISTS FOR (n:Table) REQUIRE (n.tenant, n.source_id, n.catalog, n.schema, n.name) IS UNIQUE",
    "CREATE CONSTRAINT column_key IF NOT EXISTS FOR (n:Column) REQUIRE (n.tenant, n.source_id, n.catalog, n.schema, n.table, n.name) IS UNIQUE",
    "CREATE CONSTRAINT active_key IF NOT EXISTS FOR (n:ActiveVersion) REQUIRE (n.tenant, n.model) IS UNIQUE",
    "CREATE CONSTRAINT catalog_lock IF NOT EXISTS FOR (n:CatalogLock) REQUIRE (n.tenant, n.source_id) IS UNIQUE",
)


def publish(plan: dict, password: str) -> None:
    driver = GraphDatabase.driver("bolt://127.0.0.1:7687", auth=("neo4j", password))
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            for statement in CONSTRAINTS:
                session.run(statement).consume()
            session.execute_write(_write, plan)
    finally:
        driver.close()


def _write(tx, plan: dict) -> None:
    for label in DEFINITION_LABELS + CATALOG_LABELS:
        items = plan["nodes"].get(label, {})
        for props in items.values():
            if label in DEFINITION_LABELS:
                tx.run(f"MERGE (n:Definition:{label} {{tenant:$tenant, model:$model, version:$version, id:$id}}) SET n += $props",
                       tenant=props["tenant"], model=props["model"],
                       version=props["version"], id=props["id"], props=props).consume()
            elif label == "Table":
                tx.run("MERGE (n:Table {tenant:$tenant, source_id:$source_id, catalog:$catalog, schema:$schema, name:$name}) SET n += $props",
                       **{key: props[key] for key in ("tenant", "source_id", "catalog", "schema", "name")},
                       props=props).consume()
            else:
                tx.run("MERGE (n:Column {tenant:$tenant, source_id:$source_id, catalog:$catalog, schema:$schema, table:$table, name:$name}) SET n += $props",
                       **{key: props[key] for key in ("tenant", "source_id", "catalog", "schema", "table", "name")},
                       props=props).consume()

    # IDs are unique within this plan. Match by indexed definition/catalog keys via a
    # temporary in-memory mapping to avoid persisting an extra GraphEntity label.
    lookup = {}
    for label, items in plan["nodes"].items():
        for uid, props in items.items():
            lookup[uid] = (label, props)
    for kind in RELATIONSHIPS:
        for item in plan["edges"].get(kind, {}).values():
            source_label, source = lookup[item["start"]]
            target_label, target = lookup[item["end"]]
            where_a, args_a = _match(source_label, source, "a")
            where_b, args_b = _match(target_label, target, "b")
            props = item["props"]
            if kind == "JOINS":
                cypher = f"MATCH (a:{source_label} {where_a}), (b:{target_label} {where_b}) MERGE (a)-[r:JOINS {{name:$rel_name}}]->(b) SET r += $rel_props"
                tx.run(cypher, **args_a, **args_b, rel_name=props["name"], rel_props=props).consume()
            elif kind == "DERIVED_FROM" and props:
                cypher = f"MATCH (a:{source_label} {where_a}), (b:{target_label} {where_b}) MERGE (a)-[r:DERIVED_FROM {{role:$rel_role}}]->(b)"
                tx.run(cypher, **args_a, **args_b, rel_role=props["role"]).consume()
            else:
                cypher = f"MATCH (a:{source_label} {where_a}), (b:{target_label} {where_b}) MERGE (a)-[r:{kind}]->(b)"
                tx.run(cypher, **args_a, **args_b).consume()

    now = datetime.now(timezone.utc).isoformat()
    tx.run("MERGE (a:ActiveVersion {tenant:$tenant, model:$model}) SET a.commit=$version, a.activated_at=datetime($now)",
           tenant=plan["tenant"], model=plan["model"], version=plan["version"], now=now).consume()
    tx.run("MERGE (r:LoadRun {tenant:$tenant, model:$model, commit:$version}) SET r.finished_at=datetime($now), r.result='ok', r.nodes=$nodes, r.relationships=$relationships, r.diagnostics=$diagnostics",
           tenant=plan["tenant"], model=plan["model"], version=plan["version"], now=now,
           nodes=sum(len(x) for x in plan["nodes"].values()),
           relationships=sum(len(x) for x in plan["edges"].values()),
           diagnostics=json.dumps(plan["diagnostics"], ensure_ascii=False)).consume()
    tx.run("MERGE (r:CatalogRun {tenant:$tenant, source_id:$source_id, run_id:$version}) SET r.finished_at=datetime($now), r.status='SUCCESS', r.tables_seen=$tables, r.columns_seen=$columns",
           tenant=plan["tenant"], source_id=plan["source_id"], version=plan["version"], now=now,
           tables=len(plan["nodes"].get("Table", {})),
           columns=len(plan["nodes"].get("Column", {}))).consume()


def _match(label: str, props: dict, prefix: str) -> tuple[str, dict]:
    if label in DEFINITION_LABELS:
        keys = ("tenant", "model", "version", "id")
    elif label == "Table":
        keys = ("tenant", "source_id", "catalog", "schema", "name")
    else:
        keys = ("tenant", "source_id", "catalog", "schema", "table", "name")
    return "{" + ", ".join(f"{key}:${prefix}_{key}" for key in keys) + "}", \
           {f"{prefix}_{key}": props[key] for key in keys}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "load"))
    parser.add_argument("--definition", type=Path, required=True)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--tenant", default="olist")
    parser.add_argument("--source-id", default="olist")
    parser.add_argument("--auth-file", type=Path, default=Path(__file__).with_name(".env"))
    args = parser.parse_args()
    plan = build_plan(args.definition, args.sqlite, args.tenant, args.source_id)
    print(json.dumps(summary(plan), ensure_ascii=False, indent=2))
    if args.command == "load":
        auth = args.auth_file.read_text().strip().split("=", 1)[1]
        user, password = auth.split("/", 1)
        if user != "neo4j":
            raise ValueError("Expected neo4j user in auth file")
        publish(plan, password)


if __name__ == "__main__":
    main()
