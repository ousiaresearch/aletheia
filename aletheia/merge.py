"""Carry records between scopes and stores — explicit, copy-only, and index-complete.

Two jobs:

* :func:`upgrade_scope` renames a scope in place: records, journal, entity links, jobs, the FTS index
  and the scope-keyed vector tables. The vector tables are named from a hash of the scope, and a vec0
  table cannot simply be renamed (its shadow tables keep the old name and the virtual table then
  fails with "no such table: ..._rowids" — verified), so they are copied into the new prefix and the
  old ones dropped.
* :func:`merge_store` copies a scope from one store into another. The source is opened read-only,
  identical records are counted rather than rewritten, divergent ids are refused by name, and the
  result states whether exported = imported + present + skipped.

The provider never runs either of these on startup. Deciding to move another profile's records from
a path basename is how one profile silently absorbs another's memory; the operator runs the command
and sees the counts.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import SCHEMA_VERSION, Record, Substrate

logger = logging.getLogger(__name__)

# Tables with a literal ``scope`` column that a plain UPDATE can move. ``jobs`` has a
# UNIQUE(scope, source_id, stage, content_hash) and is moved with INSERT OR IGNORE + DELETE.
# ``receipts`` is never rewritten: a receipt records what was consulted at the time, and editing it
# would bend the audit trail to match the present.
SCOPE_TABLES = ("records", "journal", "entity_links", "quarantined_records")

_VEC_NAME_RE = re.compile(r"^vec_([0-9a-f]{16})_(.+)$")
_VEC_SHADOW = ("_chunks", "_info", "_rowids", "_vector_chunks00")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def _load_vec(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return True
    except Exception:  # no extension: vectors are reported as deferred, never half-moved
        return False


def scope_counts(db_path: Path) -> dict[str, int]:
    """Records per scope, for before/after evidence."""
    db_path = Path(db_path).expanduser()
    if not db_path.is_file():
        return {}
    conn = _connect_readonly(db_path)
    try:
        if not _has_table(conn, "records"):
            return {}
        return {scope: count for scope, count in
                conn.execute("SELECT scope, COUNT(*) FROM records GROUP BY scope").fetchall()}
    finally:
        conn.close()


def _vec_parents(conn: sqlite3.Connection, scope: str) -> list[str]:
    import hashlib

    prefix = "vec_" + hashlib.sha256(scope.encode()).hexdigest()[:16]
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?", (prefix + "%",))]
    return [name for name in names
            if _VEC_NAME_RE.match(name) and not any(name.endswith(s) for s in _VEC_SHADOW)]


def _move_vectors(conn: sqlite3.Connection, old_scope: str, new_scope: str) -> dict[str, int]:
    import hashlib

    new_prefix = "vec_" + hashlib.sha256(new_scope.encode()).hexdigest()[:16]
    moved = {"tables": 0, "rows": 0}
    for old in _vec_parents(conn, old_scope):
        match = _VEC_NAME_RE.match(old)
        if not match:
            continue
        new = f"{new_prefix}_{match.group(2)}"
        if _has_table(conn, new):
            continue
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (old,)).fetchone()
        dim = re.search(r"float\[(\d+)\]", ddl[0]) if ddl and ddl[0] else None
        if not dim:
            continue
        conn.execute(f"CREATE VIRTUAL TABLE {new} USING vec0(record_id TEXT PRIMARY KEY, "
                     f"embedding float[{int(dim.group(1))}])")
        conn.execute(f"INSERT INTO {new}(record_id, embedding) SELECT record_id, embedding FROM {old}")
        rows = conn.execute(f"SELECT COUNT(*) FROM {new}").fetchone()[0]
        if rows:
            # Prove the copy answers a KNN query before the only other copy is dropped.
            sample = conn.execute(f"SELECT embedding FROM {new} LIMIT 1").fetchone()
            conn.execute(f"SELECT record_id FROM {new} WHERE embedding MATCH ? LIMIT 1",
                         (sample[0],)).fetchall()
        conn.execute(f"DROP TABLE {old}")
        moved["tables"] += 1
        moved["rows"] += rows
    return moved


def upgrade_scope(db_path: Path, old_scope: str, new_scope: str) -> dict[str, Any]:
    """Rename ``old_scope`` to ``new_scope`` in place. Idempotent; returns what moved.

    Refuses instead of silently unioning when the target scope already holds records: that is a
    scope-boundary change, not a rename, and it needs an explicit decision.
    """
    db_path = Path(db_path).expanduser()
    result: dict[str, Any] = {"from": old_scope, "to": new_scope, "changed": {}, "refused": ""}
    if not db_path.is_file() or old_scope == new_scope:
        return result
    conn = _connect(db_path)
    has_vec = _load_vec(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        counts = dict(conn.execute("SELECT scope, COUNT(*) FROM records GROUP BY scope").fetchall())
        result["before"] = counts
        if counts.get(old_scope, 0) == 0:
            conn.rollback()
            result["refused"] = f"scope {old_scope!r} holds no records"
            return result
        if counts.get(new_scope, 0) > 0:
            conn.rollback()
            result["refused"] = (f"scope {new_scope!r} already holds {counts[new_scope]} records; "
                                 "rename to a fresh scope, or decide the union explicitly")
            return result

        changed: dict[str, int] = {}
        for table in SCOPE_TABLES:
            if not _has_table(conn, table):
                continue
            cur = conn.execute(f"UPDATE {table} SET scope=? WHERE scope=?", (new_scope, old_scope))
            if cur.rowcount:
                changed[table] = cur.rowcount
        if _has_table(conn, "jobs"):
            moved = conn.execute(
                "INSERT OR IGNORE INTO jobs(scope, job_id, source_id, stage, content_hash, state, reason) "
                "SELECT ?, job_id, source_id, stage, content_hash, state, reason FROM jobs WHERE scope=?",
                (new_scope, old_scope)).rowcount
            dropped = conn.execute("DELETE FROM jobs WHERE scope=?", (old_scope,)).rowcount
            if moved or dropped:
                changed["jobs"] = moved
        if _has_table(conn, "fts"):
            cur = conn.execute("UPDATE fts SET scope=? WHERE scope=?", (new_scope, old_scope))
            if cur.rowcount:
                changed["fts"] = cur.rowcount
        if _has_table(conn, "journal"):
            conn.execute(
                "INSERT INTO journal(op, record_id, scope, detail, recorded_at) "
                "VALUES ('rename_scope', NULL, ?, ?, strftime('%s','now'))",
                (new_scope, f"renamed from {old_scope}"))
        conn.commit()
        result["changed"] = changed
        if has_vec:
            conn.execute("BEGIN IMMEDIATE")
            result["vectors"] = _move_vectors(conn, old_scope, new_scope)
            conn.commit()
        else:
            result["vectors"] = {"deferred": "sqlite-vec unavailable; run `aletheia reembed`"}
    except Exception as exc:
        conn.rollback()
        result["refused"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        conn.close()
    if result["changed"]:
        logger.info("aletheia: renamed scope %s -> %s in %s (%s)", old_scope, new_scope, db_path,
                    ", ".join(f"{k}={v}" for k, v in result["changed"].items()))
    return result


def _ctx(scope: str, *, write: bool) -> ScopeContext:
    return ScopeContext(granted_scopes=frozenset({scope}), user_authored=True,
                        actor="aletheia-merge:operator",
                        authorization_source="config:merge-store" if write else None)


def _read_export(path: Path, scope: str) -> tuple[list[dict], int]:
    """Export a scope without opening the store for writing. Returns (records, user_version).

    ``Substrate.__init__`` migrates, sets PRAGMAs and repairs rows; running that against a live
    owner's store (or a read-only archive) is not a read. This touches records and tombstones only.
    """
    conn = _connect_readonly(path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        if not _has_table(conn, "records"):
            return [], version
        rows = conn.execute("SELECT * FROM records WHERE scope = ?", (scope,)).fetchall()
        erased = ({row[0] for row in conn.execute("SELECT record_id FROM lineage_erased")}
                  if _has_table(conn, "lineage_erased") else set())
        out: list[dict] = []
        for row in rows:
            record = Record.from_row(row)
            item = dict(record.__dict__)
            if record.record_type == "Correction" and record.supersedes is None \
                    and record.record_id in erased:
                item["lineage_erased"] = True
            out.append(item)
        return out, version
    finally:
        conn.close()


def _same(record: dict, incumbent: Record) -> bool:
    for key in ("payload", "source_ids", "provenance"):
        if json.dumps(record.get(key), sort_keys=True) != \
                json.dumps(getattr(incumbent, key), sort_keys=True):
            return False
    for key in ("record_id", "record_type", "epistemic_status", "attribution", "supersedes",
                "created_at", "effective_at", "valid_from", "valid_to"):
        if record.get(key) != getattr(incumbent, key):
            return False
    return True


def merge_store(source_path: Path, target_path: Path, old_scope: str, new_scope: str, *,
                embedder: Any = None, engine_builder=ScopeEngine) -> dict[str, Any]:
    """Copy a scope from ``source_path`` into ``target_path`` under ``new_scope``.

    Additive: an existing id with different content is refused and named, never overwritten. Records
    the target's rules reject are skipped by id rather than zeroing the bundle. Index repair runs
    whenever the target scope is incomplete, so an interrupted merge stays repairable.
    """
    source_path = Path(source_path).expanduser()
    target_path = Path(target_path).expanduser()
    result: dict[str, Any] = {
        "source": str(source_path), "target": str(target_path),
        "old_scope": old_scope, "new_scope": new_scope,
        "exported": 0, "imported": 0, "already_present": 0,
        "skipped_divergent": [], "skipped_refused": [], "refusal": "",
    }
    if not source_path.is_file():
        result["error"] = "source store does not exist"
        return result

    records, source_version = _read_export(source_path, old_scope)
    result["source_schema_version"] = source_version
    result["exported"] = len(records)
    if not records:
        result["error"] = f"source scope {old_scope!r} holds no records"
        return result
    if source_version > SCHEMA_VERSION:
        result["error"] = (f"source store is schema {source_version}; this build understands "
                           f"{SCHEMA_VERSION} — upgrade the source with its own Aletheia first")
        return result

    target_engine = engine_builder(known_scopes={new_scope})
    target = Substrate(target_path, target_engine)
    try:
        incumbents = {rec.record_id: rec for rec in target.all_records(_ctx(new_scope, write=False))}
        to_import: list[dict] = []
        for record in records:
            incumbent = incumbents.get(str(record.get("record_id") or ""))
            if incumbent is not None:
                if _same(record, incumbent):
                    result["already_present"] += 1
                else:
                    result["skipped_divergent"].append(record["record_id"])
                continue
            record["scope"] = new_scope
            to_import.append(record)

        if to_import:
            bundle = {"schema_version": SCHEMA_VERSION, "scope": new_scope, "records": to_import}
            imported = target.import_scope(bundle, _ctx(new_scope, write=True))
            result["imported"] = int(imported)
            result["refusal"] = getattr(target, "last_import_refusal", None) or ""
            if not imported and result["refusal"]:
                # A policy refusal drops the whole bundle. Import one at a time instead and name
                # what the target refuses, so "zero imported" is never mistaken for "empty source".
                refused: list[dict] = []
                kept = 0
                for record in to_import:
                    probe = {"schema_version": SCHEMA_VERSION, "scope": new_scope,
                             "records": [record]}
                    if target.import_scope(probe, _ctx(new_scope, write=True)):
                        kept += 1
                    else:
                        refused.append({"record_id": record["record_id"],
                                        "reason": getattr(target, "last_import_refusal", "")})
                result["imported"] = kept
                result["skipped_refused"] = refused

        ctxw = _ctx(new_scope, write=True)
        if hasattr(target, "fts_index_complete") and not target.fts_index_complete(new_scope, ctxw):
            try:
                target.rebuild_fts(new_scope, ctxw)
            except Exception as exc:
                result["fts_error"] = str(exc)
        if embedder is not None and hasattr(target, "vector_index_complete") \
                and not target.vector_index_complete(new_scope, ctxw, model_id=embedder.model_id):
            try:
                result["reembedded"] = target.reembed_missing(new_scope, ctxw, embedder)
            except Exception as exc:
                result["reembed_error"] = str(exc)
        result["counts_after"] = scope_counts(target_path)
    finally:
        target.close()

    result["accounted"] = (result["imported"] + result["already_present"]
                           + len(result["skipped_divergent"]) + len(result["skipped_refused"]))
    result["balanced"] = result["accounted"] == result["exported"]
    return result