"""Authoritative FK checks with optional bounded, semantic row witnesses."""

from __future__ import annotations

import base64
import math
import sqlite3
import time
from pathlib import Path


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def name_key(name):
    # SQLite folds ASCII identifier case, not arbitrary Unicode.
    return name.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def encode_value(value):
    if isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float) and not math.isfinite(value):
        return {"type": "real", "value": str(value)}
    kind = {str: "text", int: "integer", float: "real", type(None): "null"}[type(value)]
    return {"type": kind, "value": value}


def witnesses(db, group, child_metadata, parent_metadata, without_rowid, cap):
    """Use the parent column's affinity/collation; remove child affinity with unary +."""
    primary = [c[1] for c in sorted(child_metadata, key=lambda c: c[5]) if c[5]]
    used = {name_key(c[1]) for c in child_metadata}
    rowid = next((n for n in ("_rowid_", "rowid", "oid") if n not in used), None)
    if without_rowid:
        rowid = None
    locators = [rowid] if rowid else primary
    if not locators:
        return [], "unavailable: all rowid aliases are shadowed and no primary key exists"
    columns = group["child_columns"]
    select = [f"c.{quote(c)}" for c in locators + columns]
    where = " AND ".join(f"c.{quote(c)} IS NOT NULL" for c in columns)
    if parent_metadata is not None:
        parents = group["parent_columns"]
        if len(parents) != len(columns) or any(p is None for p in parents):
            return [], "unavailable: unresolved parent key"
        comparisons = " AND ".join(
            f"p.{quote(p)} = +c.{quote(c)}" for p, c in zip(parents, columns, strict=True)
        )
        where += (
            f" AND NOT EXISTS (SELECT 1 FROM main.{quote(group['parent'])} AS p"
            f" WHERE {comparisons})"
        )
    sql = (
        f"SELECT {', '.join(select)} FROM main.{quote(group['table'])} AS c WHERE {where}"
        f" ORDER BY {', '.join(f'c.{quote(c)}' for c in locators)} LIMIT ?"
    )
    values = db.execute(sql, (cap,)).fetchall()
    samples = []
    for row in values:
        split = len(locators)
        samples.append(
            {
                "locator_kind": "rowid" if rowid else "primary-key",
                "locator_unique": rowid is not None or all(v is not None for v in row[:split]),
                "locator": {c: encode_value(v) for c, v in zip(locators, row[:split], strict=True)},
                "child_values": {
                    c: encode_value(v) for c, v in zip(columns, row[split:], strict=True)
                },
            }
        )
    if len(samples) != min(cap, group["violations"]):
        return [], "unavailable: witness query disagreed with engine count"
    return samples, "bounded"


def audit(
    path: str | Path,
    *,
    include_values=False,
    max_samples=20,
    max_seconds=30,
    max_tables=1000,
    max_constraints=10000,
    max_value_bytes=1_048_576,
):
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise ValueError("SQLite 3.37 or newer is required")
    if max_samples < 0 or min(max_tables, max_constraints) < 1 or max_value_bytes < 1024:
        raise ValueError("invalid sample, table, constraint or length limit")
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be positive and finite")
    source = Path(path).resolve(strict=True)
    if not source.is_file():
        raise ValueError("input must be a regular database file")
    result = {
        "schema_version": 1,
        "sqlite_version": sqlite3.sqlite_version,
        "tables_checked": 0,
        "constraints": [],
        "violations": 0,
        "skipped": [],
        "errors": [],
        "complete": False,
        "ok": False,
    }
    started = time.perf_counter()
    db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA foreign_keys=ON")
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, max_value_bytes)
        db.set_progress_handler(lambda: time.perf_counter() - started > max_seconds, 1000)
        db.execute("BEGIN")
        tables = db.execute(
            "SELECT name,type,wr FROM pragma_table_list WHERE schema='main' "
            "AND type != 'view' AND name NOT GLOB 'sqlite_*' ORDER BY name"
        ).fetchmany(max_tables + 1)
        if len(tables) > max_tables:
            raise ValueError("table limit exceeded")
        ordinary = {name_key(t[0]): t for t in tables if t[1] == "table"}
        metadata = {}
        for table, kind, _ in tables:
            if kind != "table":
                result["skipped"].append({"table": table, "reason": "virtual/shadow table"})
                continue
            metadata[table] = db.execute("SELECT * FROM pragma_table_xinfo(?)", (table,)).fetchall()
        remaining = max_samples
        for table, kind, without_rowid in tables:
            if kind != "table":
                continue
            if time.perf_counter() - started > max_seconds:
                result["errors"].append({"table": table, "code": "time-budget"})
                break
            grouped = {}
            for fk in db.execute(
                "SELECT * FROM pragma_foreign_key_list(?) ORDER BY id,seq", (table,)
            ):
                grouped.setdefault(fk[0], []).append(fk)
            if len(result["constraints"]) + len(grouped) > max_constraints:
                raise ValueError("constraint limit exceeded")
            groups = {}
            for key, rows in grouped.items():
                parent = ordinary.get(name_key(rows[0][2]))
                parent_name = parent[0] if parent else rows[0][2]
                parent_meta = metadata.get(parent_name)
                parent_columns = [r[4] for r in rows]
                if all(c is None for c in parent_columns) and parent_meta is not None:
                    parent_columns = [c[1] for c in sorted(parent_meta, key=lambda c: c[5]) if c[5]]
                group = {
                    "table": table,
                    "fk_id": key,
                    "parent": parent_name,
                    "child_columns": [r[3] for r in rows],
                    "parent_columns": parent_columns,
                    "violations": 0,
                    "checked": False,
                    "samples": [],
                    "witness_status": "disabled" if not include_values else "not-needed",
                }
                groups[key] = group
                result["constraints"].append(group)
            try:
                for _, _, _, key in db.execute(f"PRAGMA main.foreign_key_check({quote(table)})"):
                    groups[key]["violations"] += 1
                    result["violations"] += 1
                for group in groups.values():
                    group["checked"] = True
                result["tables_checked"] += 1
            except sqlite3.Error:
                result["errors"].append({"table": table, "code": "foreign-key-check-failed"})
                continue
            if include_values:
                for group in groups.values():
                    if group["violations"] == 0:
                        continue
                    if remaining == 0:
                        group["witness_status"] = "sample-cap"
                        continue
                    try:
                        samples, status = witnesses(
                            db,
                            group,
                            metadata[table],
                            metadata.get(group["parent"]),
                            without_rowid,
                            remaining,
                        )
                        group["samples"], group["witness_status"] = samples, status
                        remaining -= len(samples)
                    except sqlite3.Error:
                        group["witness_status"] = "unavailable: SQLite witness query failed"
        result["complete"] = not result["errors"] and not result["skipped"]
    except sqlite3.Error:
        result["errors"].append({"code": "database-read-failed"})
    finally:
        db.close()
    result["ok"] = result["complete"] and result["violations"] == 0
    return result
