"""Weft substrate: record store, journal, and projection writer.

Authoritative store is SQLite. Projection files are derived, never authoritative.
The journal is an audit trail of committed operations and denials. It carries
no record payloads, so deletion cannot leave content behind in it.

See docs/adr/0001-phase1-decisions.md for the design rationale.
"""

from __future__ import annotations

from contextlib import contextmanager

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from weft.knowledge import KnowledgeRefusal, check_record
from weft.scope import ScopeContext, ScopeEngine, _flatten_payload

SCHEMA_VERSION = 1

# Record ids are used as file names by projections. Restrict them so they can
# never contain separators, dots, or a leading dash (review fix F9).
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class SubstrateError(Exception):
    """Base error for substrate failures."""


class ScopeViolation(SubstrateError):
    """A scope refusal. audit_recorded is true only for a confirmed durable audit.

    False means no audit was confirmed; a deferred audit may still be recorded later.
    """

    def __init__(self, reason: str, *, audit_recorded: bool = False):
        super().__init__(reason)
        self.audit_recorded = audit_recorded
        self._audit_index: int | None = None


class SchemaViolation(SubstrateError):
    """A record does not match its schema, or its id is unsafe."""


class RecordConflict(SubstrateError):
    """A record with this id already exists and overwrite was not requested."""


@dataclass
class Record:
    """A single typed record in the substrate."""

    record_id: str
    record_type: str
    scope: str
    payload: dict[str, Any]
    epistemic_status: str = "stated"
    created_at: float = field(default_factory=time.time)
    effective_at: float | None = None
    supersedes: str | None = None
    source_ids: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    # Phase 2 (D2-2): required on every new write; enforced by the knowledge rules.
    attribution: str | None = None
    # Phase 12 (P12): bi-temporal validity window.
    valid_from: float | None = None
    valid_to: float | None = None

    def to_row(self) -> tuple:
        return (
            self.record_id,
            self.record_type,
            self.scope,
            self.epistemic_status,
            json.dumps(self.payload, sort_keys=True),
            self.created_at,
            self.effective_at,
            self.supersedes,
            json.dumps(self.source_ids, sort_keys=True),
            json.dumps(self.provenance, sort_keys=True),
            self.attribution,
            self.valid_from,
            self.valid_to,
        )

    @classmethod
    def from_row(cls, row: tuple) -> "Record":
        return cls(
            record_id=row[0],
            record_type=row[1],
            scope=row[2],
            epistemic_status=row[3],
            payload=json.loads(row[4]),
            created_at=row[5],
            effective_at=row[6],
            supersedes=row[7],
            source_ids=json.loads(row[8]),
            provenance=json.loads(row[9]),
            attribution=row[10] if len(row) > 10 else None,
            valid_from=row[11] if len(row) > 11 else None,
            valid_to=row[12] if len(row) > 12 else None,
        )


SCHEMA_VERSION = 6
VEC_DIM = 1024  # embedding dimension of the alpha embedding adapter

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    record_id        TEXT PRIMARY KEY,
    record_type      TEXT NOT NULL,
    scope            TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    payload          TEXT NOT NULL,
    created_at       REAL NOT NULL,
    effective_at     REAL,
    supersedes       TEXT,
    source_ids       TEXT NOT NULL DEFAULT '[]',
    provenance       TEXT NOT NULL DEFAULT '{}',
    attribution      TEXT,
    valid_from       REAL,
    valid_to         REAL
);
CREATE TABLE IF NOT EXISTS verifiers (
    actor_id      TEXT PRIMARY KEY,
    registered_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(record_id UNINDEXED, scope UNINDEXED, text, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS jobs (
    job_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    stage        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    state        TEXT NOT NULL,
    reason       TEXT,
    UNIQUE (scope, source_id, stage, content_hash)
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,
    scope_consulted  TEXT NOT NULL,
    payload_counts   TEXT NOT NULL,
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_scope ON records(scope);
CREATE INDEX IF NOT EXISTS idx_records_type  ON records(record_type);

CREATE TABLE IF NOT EXISTS journal (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    op              TEXT NOT NULL,
    record_id       TEXT,
    scope           TEXT,
    detail          TEXT NOT NULL,
    recorded_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_record ON journal(record_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _check_id(record_id: Any) -> None:
    if not isinstance(record_id, str) or not _ID_RE.match(record_id):
        raise SchemaViolation(f"invalid record_id: {record_id!r}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_body(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


_MANIFEST = "_manifest.json"


class Substrate:
    """The authoritative record store.

    Every write is one SQLite transaction covering the record, its journal
    entry, and the store version. Projection files are written after commit
    and are rebuildable. A lock serialises use within one process.
    """

    def __init__(
        self,
        db_path: str | os.PathLike,
        scope_engine: ScopeEngine | None = None,
        *,
        busy_timeout: float = 5.0,
    ):
        self.db_path = Path(db_path)
        # Fail closed: never operate without a gate.
        self.scope_engine = scope_engine if scope_engine is not None else ScopeEngine()
        self._lock = threading.RLock()
        self._ingestion_audits = None
        self._conn: sqlite3.Connection
        # The store holds plaintext memory. Create new path components owner-only, and
        # keep the restrictive umask active while SQLite creates its sidecar files.
        old_umask = os.umask(0o077)
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(str(self.db_path), os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            self._conn = sqlite3.connect(
                str(self.db_path),
                isolation_level=None,
                timeout=busy_timeout,
                check_same_thread=False,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            # Explicit, not the library default (ADR 0001 durability claim).
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('store_version', '0')")
            for suffix in ("", "-wal", "-shm"):
                path = str(self.db_path) + suffix
                if os.path.exists(path):
                    os.chmod(path, 0o600)
        except Exception:
            if hasattr(self, "_conn"):
                self._conn.close()
            raise
        finally:
            os.umask(old_umask)

    # ---- transaction helpers ------------------------------------------

    def _rollback_if_open(self) -> None:
        # Review fix F14: only roll back a transaction that actually exists.
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")

    def _rollback_savepoint(self, name: str) -> None:
        # A SQLite RAISE(ROLLBACK) may already have ended the whole transaction.
        if self._conn.in_transaction:
            try:
                self._conn.execute(f"ROLLBACK TO {name}")
                self._conn.execute(f"RELEASE {name}")
            except sqlite3.OperationalError as exc:
                if "no such savepoint" not in str(exc):
                    raise

    def _begin_write(self, name: str) -> bool:
        own = not self._conn.in_transaction
        if own and self._ingestion_audits is not None:
            raise sqlite3.OperationalError("ingestion transaction was rolled back")
        if own:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(f"SAVEPOINT {name}")
        except Exception:
            if own:
                self._rollback_if_open()
            raise
        return own

    def _finish_write(self, name: str, own: bool) -> None:
        self._conn.execute(f"RELEASE {name}")
        if own:
            self._conn.execute("COMMIT")

    def _abort_write(self, name: str, own: bool) -> None:
        if own:
            self._rollback_if_open()
        else:
            self._rollback_savepoint(name)

    @contextmanager
    def ingestion_transaction(self):
        """Atomically publish ingestion records, indexes, and completion marker."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._ingestion_audits = []
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException as exc:
                self._rollback_if_open()
                audits = self._ingestion_audits
                self._ingestion_audits = None
                recorded = [self._journal_standalone(*audit) for audit in audits]
                index = getattr(exc, '_audit_index', None)
                if isinstance(exc, ScopeViolation) and index is not None:
                    exc.audit_recorded = recorded[index]
                raise
            else:
                audits = self._ingestion_audits
                self._ingestion_audits = None
                for audit in audits:
                    self._journal_standalone(*audit)
            finally:
                self._ingestion_audits = None

    def _audited_violation(self, reason: str, recorded: bool) -> ScopeViolation:
        exc = ScopeViolation(reason, audit_recorded=recorded)
        if self._ingestion_audits is not None:
            exc._audit_index = len(self._ingestion_audits) - 1
        return exc

    def _journal(self, op: str, record_id: str | None, scope: str | None, detail: str) -> None:
        """Append a journal entry. Must run inside an open transaction."""
        if self._ingestion_audits is not None and op.endswith("_denied"):
            self._ingestion_audits.append((op, record_id, scope, detail))
            return
        self._conn.execute(
            "INSERT INTO journal(op, record_id, scope, detail, recorded_at) VALUES (?,?,?,?,?)",
            (op, record_id, scope, detail, time.time()),
        )

    def _journal_standalone(self, op: str, record_id: str | None, scope: str | None, detail: str) -> bool:
        """Journal a denial in its own transaction. Returns False if it could not be recorded.

        Review fix F13: a lock error here must not replace the denial. The caller
        still refuses the operation and reports whether the audit entry landed.
        """
        if self._ingestion_audits is not None:
            self._ingestion_audits.append((op, record_id, scope, detail))
            return False  # The audit is not durable yet.
        own = False
        try:
            own = self._begin_write("weft_journal")
            self._journal(op, record_id, scope, detail)
            self._finish_write("weft_journal", own)
            return own  # A savepoint release does not commit the outer transaction.
        except sqlite3.Error:
            self._abort_write("weft_journal", own)
            return False

    def _store_version(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='store_version'").fetchone()
        return int(row[0]) if row else 0

    # ---- migrations (spec §4) -----------------------------------------

    def _migrate(self) -> None:
        self._load_vec()
        """Forward-only, transactional migrations, tracked in PRAGMA user_version.

        Version 2 (Phase 2): records.attribution, backfilled to 'system' for rows
        written before attribution existed. A failure rolls the whole step back.
        Version 5 (Phase 12): entity_links table, valid_from/valid_to columns.
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(records)")}
        if version >= 2 and "attribution" not in cols:
            # R-5: the recorded version is not trusted on its own. Fail closed.
            raise SubstrateError("schema version >= 2 without records.attribution; refusing to open")
        if version >= SCHEMA_VERSION:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if version < 2:
                cols = {r[1] for r in self._conn.execute("PRAGMA table_info(records)")}
                if "attribution" not in cols:
                    self._conn.execute("ALTER TABLE records ADD COLUMN attribution TEXT")
                self._conn.execute(
                    "UPDATE records SET attribution = 'system' WHERE attribution IS NULL"
                )
            if version < 4:
                # Version 4 (review G-P3-3): jobs are keyed by scope. The jobs table is
                # Phase 3 idempotency state, unreleased; it is rebuilt, not migrated.
                self._conn.execute("DROP TABLE IF EXISTS jobs")
                self._conn.execute(
                    "CREATE TABLE jobs (job_id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, "
                    "source_id TEXT NOT NULL, stage TEXT NOT NULL, content_hash TEXT NOT NULL, "
                    "state TEXT NOT NULL, reason TEXT, UNIQUE (scope, source_id, stage, content_hash))"
                )
            if version < 5:
                # Version 5 (Phase 12): bi-temporal columns and entity_links table.
                if "valid_from" not in cols:
                    self._conn.execute("ALTER TABLE records ADD COLUMN valid_from REAL")
                if "valid_to" not in cols:
                    self._conn.execute("ALTER TABLE records ADD COLUMN valid_to REAL")
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS entity_links (
                        source_id   TEXT NOT NULL,
                        target_id   TEXT NOT NULL,
                        relation    TEXT NOT NULL,
                        valid_from  REAL,
                        valid_to    REAL,
                        scope       TEXT NOT NULL,
                        PRIMARY KEY (source_id, target_id, relation),
                        FOREIGN KEY (source_id) REFERENCES records(record_id) ON DELETE CASCADE
                    )
                """)
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_links_source ON entity_links(source_id)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_links_target ON entity_links(target_id)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_links_scope ON entity_links(scope)")
            if version < 3:
                self._conn.execute("DELETE FROM fts")
                for row in self._conn.execute("SELECT * FROM records").fetchall():
                    self._fts_index(Record.from_row(row))
            if version < 6:
                # Version 6: switch FTS5 to porter stemming tokenizer.
                self._conn.execute("DROP TABLE IF EXISTS fts")
                self._conn.execute(
                    "CREATE VIRTUAL TABLE fts USING fts5(record_id UNINDEXED, scope UNINDEXED, text, tokenize='porter unicode61')"
                )
                for row in self._conn.execute("SELECT * FROM records").fetchall():
                    self._fts_index(Record.from_row(row))
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.execute("COMMIT")
        except Exception:
            self._rollback_if_open()
            raise

    # ---- verifiers (D2-9) ---------------------------------------------

    def register_verifier(self, actor_id: str, ctx: ScopeContext | None) -> None:
        """Register a verifier. Only a user-authored turn may do this (D2-9).

        The agent cannot create, modify, or grant a verifier.
        """
        with self._lock:
            if ctx is None or not ctx.user_authored:
                raise ScopeViolation("verifier registration not permitted")
            _check_id(actor_id)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO verifiers(actor_id, registered_at) VALUES (?,?)",
                    (actor_id, time.time()),
                )
                self._journal("verifier_registered", None, None, "verifier registered")
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_if_open()
                raise

    @staticmethod
    def _cites(source_ids_text: str, record_id: str) -> bool:
        """Does a stored source_ids list cite record_id? Corrupt data fails closed."""
        try:
            ids = json.loads(source_ids_text)
        except (TypeError, ValueError):
            raise SubstrateError("corrupt source_ids in store; refusing to continue")
        return isinstance(ids, list) and record_id in ids

    def _relied_on(self, record_id: str) -> bool:
        """R-2: is record_id the Verification that a verified Claim relies on?"""
        for _cid, csrc in self._conn.execute(
            "SELECT record_id, source_ids FROM records "
            "WHERE record_type = 'Claim' AND epistemic_status = 'verified'"
        ).fetchall():
            if self._cites(csrc, record_id):
                return True
        return False

    # ---- full-text index (Phase 3) -----------------------------------

    @staticmethod
    def _payload_only_text(record) -> str:
        parts: list[str] = []
        _flatten_payload(getattr(record, "payload", None), parts)
        return " ".join(parts)

    def _fts_index(self, record) -> None:
        self._conn.execute("DELETE FROM fts WHERE record_id = ?", (record.record_id,))
        self._conn.execute(
            "INSERT INTO fts(record_id, scope, text) VALUES (?,?,?)",
            (record.record_id, record.scope, self._payload_only_text(record)),
        )

    def rebuild_fts(self, scope: str, ctx) -> None:
        """Rebuild one granted scope's derived index, atomically from records."""
        with self._lock:
            own = self._begin_write("weft_fts_rebuild")
            try:
                if ctx is None or scope not in ctx.granted_scopes or scope not in self.scope_engine.known_scopes:
                    raise ScopeViolation("write not permitted")
                records = [Record.from_row(row) for row in self._conn.execute(
                    "SELECT * FROM records WHERE scope = ?", (scope,)
                ).fetchall()]
                if not records:
                    raise SubstrateError("no records in requested scope; refusing rebuild")
                for rec in records:
                    if (not isinstance(rec.payload, dict) or not isinstance(rec.provenance, dict)
                            or not isinstance(rec.source_ids, list)
                            or not all(isinstance(item, str) for item in rec.source_ids)):
                        raise SchemaViolation("invalid stored record shape; refusing rebuild")
                    if (not self.scope_engine.authorize_write(rec, ctx).allowed
                            or not self.scope_engine.authorize_read(
                                scope, ctx, self.scope_engine._payload_text(rec)).allowed):
                        raise ScopeViolation("write not permitted")
                    if self._conn.execute(
                            "SELECT 1 FROM fts WHERE record_id=? AND scope IS NOT ? LIMIT 1",
                            (rec.record_id, scope)).fetchone():
                        raise SubstrateError("conflicting index scope; refusing rebuild")
                self._conn.execute("DELETE FROM fts WHERE scope=?", (scope,))
                for rec in records:
                    self._fts_index(rec)
                self._journal("fts_rebuilt", None, scope, "scope index rebuilt")
                self._finish_write("weft_fts_rebuild", own)
            except BaseException:
                self._abort_write("weft_fts_rebuild", own)
                raise

    def _fts_remove(self, record_id: str) -> None:
        self._conn.execute("DELETE FROM fts WHERE record_id = ?", (record_id,))

    # ---- jobs and receipts (Phase 3) ---------------------------------

    def job_state(self, source_id: str, stage: str, content_hash: str, scope: str, ctx=None) -> str | None:
        """Job state for a Source, visible only to a caller who can read the Source's scope.

        Unknown, unreadable and missing-context all return None (review G-P3-2).
        """
        with self._lock:
            if ctx is None or not self.scope_engine.authorize_read(scope, ctx, "").allowed:
                return None
            row = self._conn.execute(
                "SELECT state FROM jobs WHERE scope=? AND source_id=? AND stage=? AND content_hash=?",
                (scope, source_id, stage, content_hash),
            ).fetchone()
            return row[0] if row else None

    def set_job(self, source_id: str, stage: str, content_hash: str, state: str,
                reason: str | None = None, *, scope: str, ctx=None) -> None:
        """Job writes need write authority over the named scope (review G-P3-2).

        The scope is caller-named but gated against ctx: naming a scope grants nothing.
        The Source may not exist yet (jobs start before the Source is written).
        """
        stub = Record(record_id=source_id, record_type="Source", scope=scope,
                      payload={}, attribution="document", source_ids=[])
        with self._lock:
            if ctx is None or not self.scope_engine.authorize_write(stub, ctx).allowed:
                raise ScopeViolation("write not permitted")
            own = self._begin_write("weft_job")
            try:
                self._conn.execute(
                    "INSERT INTO jobs(scope, source_id, stage, content_hash, state, reason) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(scope, source_id, stage, content_hash) DO UPDATE SET state=excluded.state, reason=excluded.reason "
                    "WHERE jobs.state != 'done' OR excluded.state = 'done'",
                    (scope, source_id, stage, content_hash, state, reason),
                )
                self._finish_write("weft_job", own)
            except Exception:
                self._abort_write("weft_job", own)
                raise

    def _load_vec(self) -> None:
        import sqlite_vec
        self._conn.enable_load_extension(True)
        try:
            sqlite_vec.load(self._conn)
        finally:
            self._conn.enable_load_extension(False)

    @staticmethod
    def _vec_prefix(scope: str) -> str:
        import hashlib
        return "vec_" + hashlib.sha256(scope.encode()).hexdigest()[:16] + "_"

    @classmethod
    def _vec_table(cls, scope: str, model_id: str) -> str:
        """One vector table per (scope, model). Vectors from different models never share a table (review F3)."""
        import hashlib
        return cls._vec_prefix(scope) + hashlib.sha256(model_id.encode()).hexdigest()[:12]

    def _vec_tables_for(self, scope: str) -> list:
        prefix = self._vec_prefix(scope)
        # Only the vec0 virtual tables: sqlite-vec also creates shadow tables with the same prefix.
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND substr(name, 1, ?) = ? "
            "AND sql LIKE 'CREATE VIRTUAL TABLE%'",
            (len(prefix), prefix),
        ).fetchall()
        return [r[0] for r in rows]

    def _vec_exists(self, scope: str, model_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (self._vec_table(scope, model_id),),
        ).fetchone()
        return row is not None

    def vector_has_record(self, record_id: str, scope: str, ctx, *, model_id: str) -> bool:
        """Return whether a caller-readable record has a vector in its scope/model index."""
        if ctx is None:
            return False
        with self._lock:
            rec = self._lookup_record(record_id)
            if (rec is None or rec.scope != scope
                    or not self.scope_engine.authorize_read(
                        scope, ctx, self.scope_engine._payload_text(rec)
                    ).allowed):
                return False
            if not self._vec_exists(scope, model_id):
                return False
            return self._conn.execute(
                f"SELECT 1 FROM {self._vec_table(scope, model_id)} WHERE record_id = ?",
                (record_id,),
            ).fetchone() is not None

    def fts_index_complete(self, scope: str, ctx) -> bool:
        """Check readable record projections and stray entries in one scope."""
        if ctx is None or not self.scope_engine.authorize_read(scope, ctx, "").allowed:
            raise ScopeViolation("read not permitted")
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM fts LEFT JOIN records ON records.record_id=fts.record_id "
                "WHERE records.record_id IS NULL OR records.scope IS NOT fts.scope LIMIT 1"
            ).fetchone():
                return False
            rows = self._conn.execute("SELECT * FROM records WHERE scope=?", (scope,)).fetchall()
            for row in rows:
                rec = Record.from_row(row)
                if (not isinstance(rec.payload, dict) or not isinstance(rec.provenance, dict)
                        or not isinstance(rec.source_ids, list)
                        or not all(isinstance(item, str) for item in rec.source_ids)):
                    return False
                if not self.scope_engine.authorize_read(
                        scope, ctx, self.scope_engine._payload_text(rec)).allowed:
                    continue
                indexed = self._conn.execute(
                    "SELECT scope, text FROM fts WHERE record_id=?", (rec.record_id,)
                ).fetchall()
                if [tuple(item) for item in indexed] != [(scope, self._payload_only_text(rec))]:
                    return False
            return True

    def vector_index_complete(self, scope: str, ctx, *, model_id: str) -> bool:
        """Whether every readable text record in one authorized scope has this model's vector.

        Used by hybrid recall to fail visibly rather than silently become lexical-only.
        The method returns no identifiers or counts.
        """
        if ctx is None or not self.scope_engine.authorize_read(scope, ctx, "").allowed:
            raise ScopeViolation("read not permitted")
        with self._lock:
            # Stray entries affect the search corpus even when later filtering
            # hides their IDs. Reject orphan or cross-scope index members.
            if self._conn.execute(
                "SELECT 1 FROM fts LEFT JOIN records ON records.record_id=fts.record_id "
                "WHERE records.record_id IS NULL OR records.scope IS NOT fts.scope LIMIT 1"
            ).fetchone():
                return False
            # Records, not the rebuildable FTS projection, define inventory.
            # A missing projection cannot attest complete hybrid recall.
            record_ids = [row[0] for row in self._conn.execute(
                "SELECT record_id FROM records WHERE scope = ?", (scope,)
            ).fetchall()]
            table = self._vec_table(scope, model_id)
            exists = self._vec_exists(scope, model_id)
            for rid in record_ids:
                rec = self._source_or_record(rid)
                if rec is not None and (not isinstance(rec.payload, dict) or not isinstance(rec.provenance, dict)
                                        or not isinstance(rec.source_ids, list)
                                        or not all(isinstance(item, str) for item in rec.source_ids)):
                    return False
                if rec is None or not self.scope_engine.authorize_read(
                    rec.scope, ctx, self.scope_engine._payload_text(rec)
                ).allowed:
                    continue
                indexed = self._conn.execute(
                    "SELECT scope, text FROM fts WHERE record_id = ?", (rid,)
                ).fetchall()
                if [tuple(row) for row in indexed] != [(scope, self._payload_only_text(rec))]:
                    return False
                if not exists:
                    return False
                found = self._conn.execute(
                    f"SELECT embedding FROM {table} WHERE record_id = ?", (rid,)
                ).fetchone()
                if found is None or len(found[0]) != VEC_DIM * 4:
                    return False
                # Legacy stores may contain NaN/inf from before write validation.
                if not all(math.isfinite(value[0]) for value in struct.iter_unpack('=f', found[0])):
                    return False
            return True

    def vector_put(self, record_id: str, scope: str, vector, ctx=None, *, model_id: str) -> None:
        """Store an embedding for a record in its scope's vector table.

        Needs write authority over the scope (review-style gate: same rule as writes).
        """
        import sqlite_vec
        vector = list(vector)
        if len(vector) != VEC_DIM:
            raise ValueError(f"embedding must have {VEC_DIM} dimensions")
        if not all(math.isfinite(value) and abs(value) <= 3.4028234663852886e38 for value in vector):
            raise ValueError("embedding values must be finite and within float32 range")
        table = self._vec_table(scope, model_id)
        with self._lock:
            own = self._begin_write("weft_vector")
            try:
                # The writer reservation covers lookup, authorization and mutation,
                # including against writers using independent SQLite connections.
                stored = self._lookup_record(record_id)
                if (ctx is None or stored is None or stored.scope != scope
                        or not self.scope_engine.authorize_write(stored, ctx).allowed):
                    raise ScopeViolation("write not permitted")
                self._conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING "
                    f"vec0(record_id TEXT PRIMARY KEY, embedding float[{VEC_DIM}])"
                )
                self._conn.execute(f"DELETE FROM {table} WHERE record_id = ?", (record_id,))
                self._conn.execute(
                    f"INSERT INTO {table}(record_id, embedding) VALUES (?, ?)",
                    (record_id, sqlite_vec.serialize_float32(list(vector))),
                )
                self._finish_write("weft_vector", own)
            except Exception:
                self._abort_write("weft_vector", own)
                raise

    def _vec_remove(self, record_id: str, scope: str) -> None:
        """Remove this record's vector rows in every model table of its scope (review F3/F9)."""
        for table in self._vec_tables_for(scope):
            self._conn.execute(f"DELETE FROM {table} WHERE record_id = ?", (record_id,))

    def vector_candidates(self, vector, ctx, limit: int, scopes=None, *, model_id: str) -> list:
        """Nearest record ids for a query vector, limited to what ctx may read.

        Scope is decided first (only readable tables are queried), then each candidate is
        checked with the read gate (review-style defence in depth, as for FTS).
        """
        import sqlite_vec
        if ctx is None:
            return []
        vector = list(vector)
        if len(vector) != VEC_DIM:
            raise ValueError(f"query embedding must have {VEC_DIM} dimensions")
        if not all(math.isfinite(value) and abs(value) <= 3.4028234663852886e38 for value in vector):
            raise ValueError("query embedding values must be finite and within float32 range")
        engine = self.scope_engine
        readable = [s for s in sorted(engine.known_scopes) if engine.authorize_read(s, ctx, "").allowed]
        if scopes is not None:
            readable = [s for s in readable if s in scopes]
        blob = sqlite_vec.serialize_float32(list(vector))
        hits = []
        with self._lock:
            for scope in readable:
                if not self._vec_exists(scope, model_id):
                    continue
                table = self._vec_table(scope, model_id)
                # A direct search may intentionally use a partial index, but any
                # readable vector that does exist must be safe to rank.
                for rid, stored_vector in self._conn.execute(f"SELECT record_id, embedding FROM {table}"):
                    rec = self._source_or_record(rid)
                    if rec is None or not engine.authorize_read(
                            rec.scope, ctx, engine._payload_text(rec)).allowed:
                        continue
                    if (len(stored_vector) != VEC_DIM * 4 or not all(
                            math.isfinite(value[0]) for value in struct.iter_unpack('=f', stored_vector))):
                        raise ValueError('vector index contains invalid values')
                count = self._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if not count:
                    continue
                if count > 4096:
                    # vec0 caps KNN k at 4096. Exhaustive scalar distance preserves
                    # readable recall even if more than 4096 nearer rows are hidden.
                    rows = self._conn.execute(
                        f"SELECT record_id, vec_distance_L2(embedding, ?) FROM {table}",
                        (blob,),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        f"SELECT record_id, distance FROM {table} "
                        "WHERE embedding MATCH ? AND k = ?",
                        (blob, count),
                    ).fetchall()
                hits.extend((dist, rid) for rid, dist in rows)
            hits.sort()
            out = []
            for _, rid in hits:
                rec = self._source_or_record(rid)
                if rec is None:
                    continue
                if engine.authorize_read(rec.scope, ctx, engine._payload_text(rec)).allowed:
                    out.append(rid)
                    if len(out) >= limit:
                        break
        return out

    def fts_candidates(self, match: str, ctx, limit: int, scopes=None) -> list:
        """Candidate record ids for a full-text match, limited to what ctx may read.

        Each candidate is checked against the Phase 1 read gate with its own text
        (review G-P3-1). No context, or nothing readable, returns [].
        """
        if ctx is None:
            return []
        out = []
        with self._lock:
            if scopes is not None:
                placeholders = ",".join("?" * len(scopes))
                rows = self._conn.execute(
                    f"SELECT fts.record_id FROM fts "
                    f"JOIN records ON records.record_id = fts.record_id "
                    f"WHERE fts MATCH ? AND records.scope IN ({placeholders}) "
                    f"ORDER BY bm25(fts)",
                    (match, *scopes),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT record_id FROM fts WHERE fts MATCH ? ORDER BY bm25(fts)",
                    (match,),
                ).fetchall()
            for (rid,) in rows:
                rec = self._source_or_record(rid)
                if rec is None:
                    continue
                if self.scope_engine.authorize_read(rec.scope, ctx, self.scope_engine._payload_text(rec)).allowed:
                    out.append(rid)
                    if len(out) >= limit:
                        break
        return out

    def _source_or_record(self, record_id: str):
        row = self._conn.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
        return Record.from_row(row) if row else None

    def record_receipt(self, kind: str, scope_consulted: list, payload_counts: dict, ctx=None) -> None:
        """Receipts hold scope names and counts only, never record text (spec D3-12).

        Requires a context, and every named scope must be readable by it (review G-P3-2):
        a caller cannot plant scope names it has no authority over.
        """
        if ctx is None or not all(
            self.scope_engine.authorize_read(sc, ctx, "").allowed for sc in scope_consulted
        ):
            raise ScopeViolation("write not permitted")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO receipts(kind, scope_consulted, payload_counts, created_at) VALUES (?,?,?,?)",
                    (kind, json.dumps(sorted(scope_consulted)), json.dumps(payload_counts, sort_keys=True), time.time()),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_if_open()
                raise

    def _read_transaction(self):
        """Open a single read transaction snapshot across recall's checks and reads.

        The connection is in autocommit mode, so each statement otherwise sees a
        different WAL snapshot. Recall's completeness check, candidate queries, and
        per-record reads must observe one consistent view. Callers must not hold an
        open caller-owned transaction: this starts its own deferred BEGIN and rolls
        back on exit. record_receipt writes its own BEGIN IMMEDIATE separately.
        """
        import contextlib

        @contextlib.contextmanager
        def _cm():
            if self._conn.in_transaction:
                raise sqlite3.OperationalError(
                    "read_transaction refused: caller holds an open transaction"
                )
            with self._lock:
                self._conn.execute("BEGIN")
                try:
                    yield
                except BaseException:
                    self._rollback_if_open()
                    raise
                else:
                    self._rollback_if_open()

        return _cm()

    def _lookup_record(self, record_id: str):
        """Internal lookup for rule checks. Never returned to a caller."""
        row = self._conn.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
        return Record.from_row(row) if row is not None else None

    def _is_verifier(self, actor: str | None) -> bool:
        if not actor:
            return False
        return self._conn.execute(
            "SELECT 1 FROM verifiers WHERE actor_id=?", (actor,)
        ).fetchone() is not None

    def _bump_version(self) -> None:
        self._conn.execute(
            "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='store_version'"
        )

    # ---- writes -------------------------------------------------------

    def put(
        self,
        record: Record,
        ctx: ScopeContext | None,
        *,
        validate_schema: bool = True,
        overwrite: bool = False,
    ) -> None:
        """Write a record. The caller needs a granted write scope for the record's scope.

        Refused outright (ScopeViolation) if the scope engine denies it. An existing
        record with the same id raises RecordConflict unless overwrite is requested,
        and an overwrite also needs a granted scope for the incumbent record.
        """
        with self._lock:
            self._write(record, ctx, validate_schema=validate_schema, overwrite=overwrite)

    def _write(
        self,
        record: Record,
        ctx: ScopeContext | None,
        *,
        validate_schema: bool,
        overwrite: bool,
        original_id: str | None = None,
    ) -> None:
        decision = self.scope_engine.authorize_write(record, ctx)
        if not decision.allowed:
            # G10: a caller without the scope must not write into its journal.
            audited = self._journal_standalone("write_denied", None, None, "write not permitted")
            # Review G4/G5: the caller learns only that the write was refused.
            # The reason goes to the journal, where only holders of the scope read it.
            raise self._audited_violation("write not permitted", audited)

        if original_id is None and record.supersedes is not None:
            # G13: a supersedes link into a scope the caller did not authorize
            # would make another scope's record undeletable. Only correct() links.
            audited = self._journal_standalone("write_denied", None, None, "write not permitted")
            raise self._audited_violation("write not permitted", audited)

        _check_id(record.record_id)
        if validate_schema:
            self._validate(record)

        own = False
        try:
            own = self._begin_write("weft_write")

            if original_id is not None:
                orig_row = self._conn.execute(
                    "SELECT * FROM records WHERE record_id=?", (original_id,)
                ).fetchone()
                if orig_row is None:
                    # G9: same exception as an unreadable original.
                    raise ScopeViolation("correction not permitted")
                orig = Record.from_row(orig_row)
                read = self.scope_engine.authorize_read(
                    orig.scope, ctx, self.scope_engine._payload_text(orig)
                )
                if not read.allowed:
                    # Review fix F7: a correction must not supersede what the caller cannot read.
                    self._journal("correct_denied", None, None, "correction not permitted")
                    self._finish_write("weft_write", own)
                    raise self._audited_violation("correction not permitted", own)
                if record.scope != orig.scope:
                    # Review N10: a correction stays in its original's scope.
                    self._journal("correct_denied", None, None, "correction not permitted")
                    self._finish_write("weft_write", own)
                    raise self._audited_violation("correction not permitted", own)
                record.supersedes = original_id

            # Phase 2: knowledge rules (spec §3) decide inside the transaction.
            if original_id is None and record.record_type == "Correction":
                raise KnowledgeRefusal("correction must use correct()")
            if original_id is not None and record.record_type != "Correction":
                raise KnowledgeRefusal("correct() replacement must be a Correction")
            reason = check_record(
                record, ctx, is_verifier=self._is_verifier, lookup=self._lookup_record
            )
            if reason is not None:
                raise KnowledgeRefusal(reason)

            existing = self._conn.execute(
                "SELECT scope, record_type, epistemic_status FROM records WHERE record_id=?",
                (record.record_id,),
            ).fetchone()
            incumbent = None
            if existing is not None:
                # Review G5: check authority over the incumbent BEFORE reporting a
                # conflict, so a caller without the scope cannot learn the id exists.
                incumbent = self._lookup_record(record.record_id)
                if (incumbent is None or ctx is None or existing[0] not in ctx.granted_scopes
                        or not self.scope_engine.authorize_write(incumbent, ctx).allowed
                        or not self.scope_engine.authorize_read(
                            incumbent.scope, ctx, self.scope_engine._payload_text(incumbent)).allowed):
                    # Review fix F2 / G4: overwrite needs the incumbent's scope. The
                    # caller gets the same generic refusal as any denied write.
                    self._journal("write_denied", None, None, "write not permitted")
                    self._finish_write("weft_write", own)
                    raise self._audited_violation("write not permitted", own)
                if not overwrite:
                    raise RecordConflict(f"record {record.record_id!r} already exists")
                if existing[2] == "verified":
                    # Round 3 BLOCK: a verified record can only be removed, never rewritten.
                    # Otherwise a Claim could be demoted to derived and its Verification deleted.
                    raise KnowledgeRefusal("verified record cannot be overwritten")
                if existing[1] == "Verification" and self._relied_on(record.record_id):
                    # Round 2 BLOCK: overwrite must not re-point a relied-on Verification.
                    raise KnowledgeRefusal("verification is relied on by a verified claim")

            # Phase 13 gate: ProposedRule status transitions must go through
            # promote_rule/reject_rule/deprecate_rule, not raw put().
            if record.record_type == "ProposedRule":
                if incumbent is None:
                    # New insert: must start as proposed.
                    new_status = (record.payload or {}).get("status", "proposed")
                    if new_status != "proposed":
                        raise KnowledgeRefusal("proposed rule must start as proposed")
                elif incumbent.record_type != "ProposedRule":
                    # Cannot convert a non-ProposedRule into a ProposedRule.
                    raise KnowledgeRefusal("cannot change record type to ProposedRule")
                else:
                    # Overwrite: only valid lifecycle transitions allowed.
                    old_status = (incumbent.payload or {}).get("status", "proposed")
                    new_status = (record.payload or {}).get("status", "proposed")
                    # proposed -> active requires user attribution (enforced by
                    # promote_rule's _check_user_only); direct writes without
                    # user attribution must not bypass the promotion gate.
                    if (old_status, new_status) == ("proposed", "active") and not (ctx and ctx.user_authored):
                        raise KnowledgeRefusal("promotion requires user attribution")
                    allowed_transitions = {
                        ("proposed", "active"),
                        ("proposed", "rejected"),
                        ("proposed", "deprecated"),
                        ("active", "deprecated"),
                    }
                    if (old_status, new_status) not in allowed_transitions:
                        raise KnowledgeRefusal("invalid proposed rule transition")

            # Review F9: a record's vector describes its text. An overwrite replaces the text,
            # and a correction supersedes the original, so both drop the old vectors.
            if existing is not None:
                self._vec_remove(record.record_id, existing[0])
            if original_id is not None:
                self._vec_remove(original_id, record.scope)
            self._conn.execute(
                """INSERT OR REPLACE INTO records
                   (record_id, record_type, scope, epistemic_status, payload,
                    created_at, effective_at, supersedes, source_ids, provenance, attribution,
                    valid_from, valid_to)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                record.to_row(),
            )
            # Index the serialized record representation, so rebuilds and integrity
            # checks use the same key order as the durable authority.
            self._fts_index(self._lookup_record(record.record_id))
            detail = f"type={record.record_type} status={record.epistemic_status}"
            if existing is not None:
                detail += " overwrite"
            self._journal("put", record.record_id, record.scope, detail)
            self._bump_version()
            self._finish_write("weft_write", own)
        except KnowledgeRefusal as exc:
            self._abort_write("weft_write", own)
            audited = self._journal_standalone("write_denied", None, None, "write not permitted")
            raise self._audited_violation("write not permitted", audited) from None
        except Exception:
            self._abort_write("weft_write", own)
            raise

    def delete(self, record_id: str, ctx: ScopeContext | None = None, *, reason: str = "") -> bool:
        """Hard delete: remove the record and journal the removal.

        Deletion is a write. Authorization is decided INSIDE the write transaction
        (review fix F8). If any record that would lose its supersedes link is in a
        scope the caller cannot write, the whole delete is refused (review fix F15).
        """
        with self._lock:
            if ctx is None:
                self._journal_standalone("delete_denied", record_id, None, "no scope context")
                return False
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT record_id, scope FROM records WHERE record_id=?", (record_id,)
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return False

                decision = self.scope_engine.authorize_delete(row[1], ctx)
                if not decision.allowed:
                    self._journal("delete_denied", None, None, "delete not permitted")
                    self._conn.execute("COMMIT")
                    return False
                # D2-4: the classifier must be able to run. Its verdict does not block
                # deletion (removing contaminated content is the point), only its failure does.
                try:
                    full = self._lookup_record(record_id)
                    self.scope_engine.content_conflict(row[1], self.scope_engine._payload_text(full))
                except Exception:
                    self._journal("delete_denied", None, None, "delete not permitted")
                    self._conn.execute("COMMIT")
                    return False

                dependents = self._conn.execute(
                    "SELECT record_id, scope FROM records WHERE supersedes = ?", (record_id,)
                ).fetchall()
                for dep_id, dep_scope in dependents:
                    if not self.scope_engine.authorize_delete(dep_scope, ctx).allowed:
                        # Review fix F-B: name neither the dependent nor its scope in the
                        # audit detail, so the caller learns nothing about it.
                        self._journal(
                            "delete_denied", record_id, row[1],
                            "delete refused: a dependent record is outside your granted scopes",
                        )
                        self._conn.execute("COMMIT")
                        return False

                # R-2 (review): a verified Claim depends on the Verification that names it.
                # Removing the Verification would leave the Claim verified with no support.
                if self._relied_on(record_id):
                    self._journal("delete_denied", None, None, "delete not permitted")
                    self._conn.execute("COMMIT")
                    return False

                summaries = [
                    (sid, sscope)
                    for sid, sscope, src in self._conn.execute(
                        "SELECT record_id, scope, source_ids FROM records "
                        "WHERE record_type IN ('Summary', 'Observation')"
                    ).fetchall()
                    if self._cites(src, record_id)
                ]
                for sid, sscope in summaries:
                    if not self.scope_engine.authorize_delete(sscope, ctx).allowed:
                        self._journal("delete_denied", None, None, "delete not permitted")
                        self._conn.execute("COMMIT")
                        return False
                for sid, sscope in summaries:
                    self._conn.execute("DELETE FROM records WHERE record_id = ?", (sid,))
                    self._fts_remove(sid)
                    self._vec_remove(sid, sscope)
                    self._journal("unlink_summary", None, sscope, "removed derived artifact (cascade)")

                for dep_id, dep_scope in dependents:
                    self._conn.execute("UPDATE records SET supersedes = NULL WHERE record_id = ?", (dep_id,))
                    # Review N7: the detail names no deleted id.
                    self._journal(
                        "unlink_supersedes", dep_id, dep_scope,
                        "cleared dangling supersedes link (target deleted)",
                    )

                self._conn.execute("DELETE FROM records WHERE record_id=?", (record_id,))
                self._fts_remove(record_id)
                self._vec_remove(record_id, row[1])
                # R-P3-10: an import's job row must not outlive its Source.
                self._conn.execute("DELETE FROM jobs WHERE scope = ? AND source_id = ?", (row[1], record_id))
                self._journal("delete", record_id, row[1], f"reason={reason}")
                self._bump_version()
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._rollback_if_open()
                raise

    def correct(
        self,
        original_id: str,
        replacement: Record,
        ctx: ScopeContext | None,
        *,
        validate_schema: bool = True,
    ) -> None:
        """Supersede a record, retaining lineage.

        The caller must be able to read the original (review fix F7) and hold a
        granted write scope for the replacement. The original is not erased.
        """
        with self._lock:
            self._write(
                replacement, ctx, validate_schema=validate_schema, overwrite=False, original_id=original_id
            )

    # ---- reads --------------------------------------------------------

    def get(self, record_id: str, ctx: ScopeContext | None = None) -> Record | None:
        """Fetch one record, gated by scope. No context -> None."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
            if row is None or ctx is None:
                return None
            rec = Record.from_row(row)
            decision = self.scope_engine.authorize_read(rec.scope, ctx, self.scope_engine._payload_text(rec))
            if not decision.allowed:
                self._journal_standalone("read_denied", None, None, "read not permitted")
                return None
            return rec

    def query(
        self,
        ctx: ScopeContext | None,
        *,
        record_type: str | None = None,
        scope: str | None = None,
    ) -> tuple[list[Record], list]:
        """The scoped read path. Returns (readable, exclusions). No context -> nothing."""
        if ctx is None:
            return [], []
        with self._lock:
            sql = "SELECT * FROM records"
            clauses, params = [], []
            if record_type is not None:
                clauses.append("record_type = ?")
                params.append(record_type)
            if scope is not None:
                clauses.append("scope = ?")
                params.append(scope)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at"
            records = [Record.from_row(r) for r in self._conn.execute(sql, params).fetchall()]
            return self.scope_engine.filter_readable(records, ctx), []  # no exclusions (G6, G7)

    def all_records(self, ctx: ScopeContext | None = None) -> list[Record]:
        """Every record the caller may read. No context -> nothing."""
        readable, _ = self.query(ctx)
        return readable

    def get_at(self, record_id: str, timestamp: float, ctx: ScopeContext | None = None) -> Record | None:
        """Fetch the effective version of a record at ``timestamp``.

        Walks the ``supersedes`` chain to find the latest supersession whose
        ``effective_at`` is at or before ``timestamp``. Returns ``None`` if the
        record does not exist or the caller lacks read access.
        """
        if ctx is None:
            return None
        with self._lock:
            # Start with the requested record.
            row = self._conn.execute(
                "SELECT * FROM records WHERE record_id = ?", (record_id,)
            ).fetchone()
            if row is None:
                return None
            rec = Record.from_row(row)
            if rec.effective_at is not None and rec.effective_at > timestamp:
                return None
            decision = self.scope_engine.authorize_read(rec.scope, ctx, self.scope_engine._payload_text(rec))
            if not decision.allowed:
                self._journal_standalone("read_denied", None, None, "read not permitted")
                return None
            # Walk supersedes chain to find latest supersession at or before timestamp.
            current = rec
            while True:
                child = self._conn.execute(
                    "SELECT * FROM records WHERE supersedes = ? AND effective_at <= ? ORDER BY effective_at DESC LIMIT 1",
                    (current.record_id, timestamp),
                ).fetchone()
                if child is None:
                    break
                child_rec = Record.from_row(child)
                child_decision = self.scope_engine.authorize_read(child_rec.scope, ctx, self.scope_engine._payload_text(child_rec))
                if not child_decision.allowed:
                    self._journal_standalone("read_denied", None, None, "read not permitted for supersession")
                    break
                current = child_rec
            return current

    def query_as_of(
        self,
        timestamp: float,
        ctx: ScopeContext | None = None,
        *,
        record_type: str | None = None,
        scope: str | None = None,
    ) -> list[Record]:
        """Return all records visible at ``timestamp`` in the authorized scopes.

        A record is visible if its ``effective_at`` is at or before ``timestamp``
        and no superseding record with a later ``effective_at`` (also <= timestamp)
        exists. No context -> empty list.
        """
        if ctx is None:
            return []
        with self._lock:
            sql = (
                "SELECT * FROM records r "
                "WHERE r.effective_at <= ? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM records s "
                "  WHERE s.scope = r.scope "
                "  AND s.supersedes = r.record_id "
                "  AND s.effective_at <= ? "
                "  AND s.effective_at > r.effective_at"
                ")"
            )
            params: list[Any] = [timestamp, timestamp]
            if record_type is not None:
                sql += " AND r.record_type = ?"
                params.append(record_type)
            if scope is not None:
                sql += " AND r.scope = ?"
                params.append(scope)
            sql += " ORDER BY r.created_at"
            rows = self._conn.execute(sql, params).fetchall()
            records = [Record.from_row(r) for r in rows]
            return self.scope_engine.filter_readable(records, ctx)

    def export_scope(self, scope: str, ctx: ScopeContext | None = None) -> dict[str, Any]:
        """Export all records in ``scope`` readable by ``ctx`` as a portable dict.

        The payload includes a schema version and a list of record dicts with
        all fields preserved. Returns an empty dict if ``ctx`` is ``None`` or
        the scope is not readable.
        """
        if ctx is None:
            return {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM records WHERE scope = ?", (scope,)
            ).fetchall()
            records = [Record.from_row(r) for r in rows]
            readable = self.scope_engine.filter_readable(records, ctx)
            return {
                "schema_version": SCHEMA_VERSION,
                "scope": scope,
                "exported_at": time.time(),
                "records": [self._record_to_export(r) for r in readable],
            }

    def import_scope(self, data: dict[str, Any], ctx: ScopeContext | None = None) -> int:
        """Import records from an export dict into the substrate.

        The caller must have a granted write scope for the exported scope.
        Returns the number of records imported. Raises ``ValueError`` if the
        schema version does not match or the scope is missing.
        """
        if ctx is None:
            raise ValueError("context required for import")
        scope = data.get("scope")
        if not scope:
            raise ValueError("export payload missing scope")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"schema version mismatch: {data.get('schema_version')} != {SCHEMA_VERSION}")
        records = []
        for rec_dict in data.get("records", []):
            records.append(self._record_from_export(rec_dict))
        imported = 0
        with self._lock:
            for rec in records:
                decision = self.scope_engine.authorize_write(rec, ctx)
                if not decision.allowed:
                    continue
                self._validate(rec)
                reason = check_record(
                    rec, ctx, is_verifier=self._is_verifier, lookup=self._lookup_record
                )
                if reason is not None:
                    continue
                # ProposedRule gate: imports must respect the same lifecycle
                # transitions as direct writes (no bypass via import path).
                if rec.record_type == "ProposedRule":
                    old = self._lookup_record(rec.record_id)
                    new_status = (rec.payload or {}).get("status", "proposed")
                    if old is None:
                        # New insert via import: must start as proposed.
                        if new_status != "proposed":
                            continue
                    elif old.record_type != "ProposedRule":
                        # Cannot convert a non-ProposedRule into a ProposedRule.
                        continue
                    else:
                        old_status = (old.payload or {}).get("status", "proposed")
                        # proposed -> active requires user attribution.
                        if (old_status, new_status) == ("proposed", "active") and not (ctx and ctx.user_authored):
                            continue
                        allowed = {
                            ("proposed", "active"),
                            ("proposed", "rejected"),
                            ("proposed", "deprecated"),
                            ("active", "deprecated"),
                        }
                        if (old_status, new_status) not in allowed:
                            continue
                own = False
                try:
                    own = self._begin_write("weft_write")
                    self._conn.execute(
                        "INSERT OR REPLACE INTO records "
                        "(record_id, record_type, scope, epistemic_status, payload, "
                        "created_at, effective_at, supersedes, source_ids, provenance, attribution, "
                        "valid_from, valid_to) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        rec.to_row(),
                    )
                    self._fts_index(rec)
                    self._journal("import", rec.record_id, rec.scope, f"type={rec.record_type}")
                    self._bump_version()
                    self._finish_write("weft_write", own)
                    imported += 1
                except Exception:
                    self._abort_write("weft_write", own)
                    raise
        return imported

    @staticmethod
    def _record_to_export(rec: Record) -> dict[str, Any]:
        return {
            "record_id": rec.record_id,
            "record_type": rec.record_type,
            "scope": rec.scope,
            "epistemic_status": rec.epistemic_status,
            "payload": rec.payload,
            "created_at": rec.created_at,
            "effective_at": rec.effective_at,
            "supersedes": rec.supersedes,
            "source_ids": rec.source_ids,
            "provenance": rec.provenance,
            "attribution": rec.attribution,
        }

    @staticmethod
    def _record_from_export(d: dict[str, Any]) -> Record:
        return Record(
            record_id=d["record_id"],
            record_type=d["record_type"],
            scope=d["scope"],
            epistemic_status=d["epistemic_status"],
            payload=d.get("payload", {}),
            created_at=d.get("created_at", 0.0),
            effective_at=d.get("effective_at"),
            supersedes=d.get("supersedes"),
            source_ids=d.get("source_ids", []),
            provenance=d.get("provenance", {}),
            attribution=d.get("attribution"),
        )

    def journal_entries(self, ctx: ScopeContext | None = None) -> list[dict]:
        """Journal entries the caller may see (review fix F12). No context -> nothing.

        An entry is visible only if its scope is readable by the caller. Entries
        without a scope are never visible.
        """
        if ctx is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, op, record_id, scope, detail, recorded_at FROM journal ORDER BY seq"
            ).fetchall()
        out = []
        for seq, op, rid, scope, detail, ts in rows:
            if scope is None:
                continue
            if not self.scope_engine.authorize_read(scope, ctx, "").allowed:
                continue
            # G12: renumber within the caller's view so global gaps are invisible.
            out.append({"seq": len(out) + 1, "op": op, "record_id": rid, "scope": scope,
                        "detail": detail, "recorded_at": ts})
        return out

    def prune_derived(self, scope: str, older_than: float, ctx: ScopeContext | None = None) -> int:
        """Delete derived records (Claim, Hypothesis, Summary, Reasoning) in ``scope``
        created before ``older_than`` whose source records still exist.

        Returns the number of records pruned. No context -> 0.
        """
        if ctx is None:
            return 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_id, scope, source_ids FROM records "
                "WHERE scope = ? AND record_type IN ('Claim', 'Hypothesis', 'Summary', 'Reasoning') "
                "AND created_at < ?",
                (scope, older_than),
            ).fetchall()
            prunable = []
            for rid, rscope, src_json in rows:
                if not self.scope_engine.authorize_delete(rscope, ctx).allowed:
                    continue
                src_ids = json.loads(src_json)
                if not src_ids:
                    continue
                if all(self._lookup_record(sid) is not None for sid in src_ids):
                    prunable.append(rid)
            for rid in prunable:
                self._conn.execute("DELETE FROM records WHERE record_id = ?", (rid,))
                self._fts_remove(rid)
                self._vec_remove(rid, scope)
                self._journal("prune_derived", rid, scope, "retention TTL exceeded")
            if prunable:
                self._bump_version()
            return len(prunable)

    # ---- projections --------------------------------------------------

    @staticmethod
    def _view_token(ctx: ScopeContext) -> str:
        """Identify a caller's view by its granted scopes. One projection
        directory belongs to exactly one view (review G2)."""
        return _sha256(json.dumps(
            [sorted(ctx.granted_scopes), ctx.user_authorized_cross_scope,
             sorted(ctx.cross_scope_targets), ctx.authorization_source, ctx.user_authored],
            sort_keys=True,
        ))

    def _expected_files(self, ctx: ScopeContext | None) -> dict[str, tuple[str, str]]:
        """name -> (body, sha256) for every record the caller may read.

        Records whose id is unsafe as a file name are skipped (review G3).
        """
        expected = {}
        for rec in self.all_records(ctx):
            if not _ID_RE.match(rec.record_id):
                continue
            body = _file_body(rec.payload)
            expected[f"{rec.record_id}.json"] = (body, _sha256(body))
        return expected

    @staticmethod
    def _read_manifest(out: Path) -> dict | None:
        path = out / _MANIFEST
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write_projection(self, out_dir: str | os.PathLike, ctx: ScopeContext | None = None) -> Path:
        """Write the projection for exactly the caller's view.

        Derived, never authoritative. Rules:
          - No context: nothing is touched, not even the directory.
          - A directory belongs to one view. Writing with a different view, or
            over unmanaged files with projected names, is refused.
          - Only files named in this view's own previous manifest are removed.
        """
        with self._lock:
            out = Path(out_dir)
            if ctx is None:
                return out
            view = self._view_token(ctx)
            manifest = self._read_manifest(out)
            if manifest is not None and manifest.get("view") != view:
                raise ScopeViolation("projection directory belongs to another view")

            expected = self._expected_files(ctx)
            if manifest is None:
                for name in expected:
                    if (out / name).exists():
                        raise ScopeViolation("projection directory holds unmanaged files")

            # Create the projection directory owner-only from the start (no umask window).
            out.parent.mkdir(parents=True, exist_ok=True)
            if not out.exists():
                os.mkdir(out, 0o700)
            os.chmod(out, 0o700)
            prev_names = {
                n for n in ((manifest or {}).get("files") or {}) if _safe_projection_name(n)
            }
            for name, (body, _digest) in expected.items():
                _atomic_write(out / name, body)
            for name in prev_names - set(expected):
                (out / name).unlink(missing_ok=True)

            new_manifest = {
                "view": view,
                "files": {name: digest for name, (_body, digest) in expected.items()},
            }
            _atomic_write(out / _MANIFEST, json.dumps(new_manifest, indent=2, sort_keys=True))
            return out

    def projection_status(self, out_dir: str | os.PathLike, ctx: ScopeContext | None = None) -> dict:
        """Compare a projection with the store, for the caller's view only.

        Reports nothing about records outside the caller's view (review G1).
        state is one of:
          missing   no projection for this view
          tampered  a file's bytes differ from what this projection wrote
          stale     the store changed in this view since the projection was written
          current   matches the store for this view
        """
        with self._lock:
            out = Path(out_dir)
            manifest = self._read_manifest(out)
            if ctx is None or manifest is None or manifest.get("view") != self._view_token(ctx):
                return {"state": "missing", "missing": [], "modified": []}

            files = manifest.get("files") or {}
            expected = self._expected_files(ctx)
            missing, modified, changed = [], [], []
            for name, (_body, expected_digest) in expected.items():
                path = out / name
                if not path.exists():
                    missing.append(name)
                    continue
                on_disk = _sha256(path.read_text())
                written = files.get(name)
                if written is not None and on_disk != written:
                    modified.append(name)
                elif written != expected_digest:
                    changed.append(name)

            if modified:
                state = "tampered"
            elif missing or changed or len(files) != len(expected):
                state = "stale"
            else:
                state = "current"
            return {"state": state, "missing": missing, "modified": modified}

    # ---- internals ----------------------------------------------------

    def _validate(self, record: Record) -> None:
        """Schema validation. Scope and id safety are enforced separately and always."""
        if not record.record_type:
            raise SchemaViolation("record_type is required")
        if not record.scope:
            raise SchemaViolation("scope is required")
        if record.epistemic_status not in ("stated", "derived", "verified"):
            raise SchemaViolation(f"invalid epistemic_status: {record.epistemic_status!r}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Substrate":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _safe_projection_name(name: str) -> bool:
    return name.endswith(".json") and _ID_RE.match(name[: -len(".json")]) is not None


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
