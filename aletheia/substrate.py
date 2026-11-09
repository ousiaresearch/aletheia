"""Aletheia substrate: record store, journal, and projection writer.

Authoritative store is SQLite. Projection files are derived, never authoritative.
The journal is an audit trail of committed operations and denials. It carries
no record payloads, so deletion cannot leave content behind in it.

See docs/adr/0001-phase1-decisions.md for the design rationale.
"""

from __future__ import annotations

from contextlib import contextmanager

import hashlib
import json
import logging
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

from aletheia.knowledge import KnowledgeRefusal, check_record
from aletheia.scope import ScopeContext, ScopeEngine, _flatten_payload

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


SCHEMA_VERSION = 8
# Export bundles from these schema versions can be restored. Version 6 bundles carry the same
# record shape; they never carry approval bindings (approvals are never restored, see import_scope).
# Version 8 adds the relations table, which is derived: a 6 or 7 bundle restores and the graph is
# rebuilt from records rather than trusted from the bundle.
_IMPORTABLE_SCHEMA_VERSIONS = (6, 7, 8)
# Payload keys a lifecycle transition (propose -> active/rejected/deprecated) may change.
# Everything else in the rule, including all record-level fields, must stay identical.
_RULE_LIFECYCLE_KEYS = ('status', 'promoted_by', 'promoted_at', 'rejection_reason', 'deprecated_at')
# What a journal row's record_id / reason become when the record is erased: the audit row stays
# (type, time, scope), the identifier and free text do not.
_ERASED = "[erased]"
_GENERIC_DELETE_REFUSAL = "delete not permitted"
_DEFAULT_SCRUB_RETRY_SECONDS = 2.0
VEC_DIM = 1024  # embedding dimension of the alpha embedding adapter
# Record types the ingest pipelines embed. A scope's vector index must cover these; procedural and
# decision-type records (ProposedRule, Decision, Commitment, Correction, ...) are lexical-only, so a
# missing vector for them never makes a scope's vector index incomplete.
EMBEDDABLE_TYPES = frozenset({"Source", "Observation", "Claim"})

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
-- Legacy rows that fail the record integrity rules (non-string payload text, non-finite
-- timestamps, ...), moved out of `records` by the schema-7 migration so no read path can see them.
-- Columns are untyped on purpose: the original values (e.g. an infinite timestamp) are preserved.
CREATE TABLE IF NOT EXISTS quarantined_records (
    record_id TEXT PRIMARY KEY, reason TEXT NOT NULL, record_type, scope, epistemic_status, payload,
    created_at, effective_at, supersedes, source_ids, provenance, attribution, valid_from, valid_to
);
-- A Correction whose original was erased is marked here (the Correction's own id only; NO id of
-- the erased original is stored anywhere), so an export of the scope can still be restored.
-- See delete() and import_scope().
CREATE TABLE IF NOT EXISTS lineage_erased (record_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS verifiers (
    actor_id      TEXT PRIMARY KEY,
    registered_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(record_id UNINDEXED, scope UNINDEXED, text, tokenize='porter unicode61');
-- record_id is UNINDEXED in fts, so a lookup by it scans the whole index. This maps a record to the
-- rowid of its fts row so a write replaces its row with a point lookup. Derived data: a row here is
-- only ever a hint, deletes re-check record_id, and integrity checks never trust it.
CREATE TABLE IF NOT EXISTS fts_rowmap (record_id TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL);
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
CREATE TABLE IF NOT EXISTS rule_incarnations (
    rule_id TEXT PRIMARY KEY, token TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_approvals (
    promoter_id TEXT NOT NULL, rule_id TEXT NOT NULL, token TEXT NOT NULL,
    proposal_hash TEXT NOT NULL, PRIMARY KEY (promoter_id, rule_id)
);

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
    if not isinstance(record_id, str) or not _ID_RE.fullmatch(record_id):
        raise SchemaViolation(f"invalid record_id: {record_id!r}")


class _StaleVector(Exception):
    """The record text changed after it was embedded (internal to reembed_missing)."""


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
        scrub_retry_seconds: float = _DEFAULT_SCRUB_RETRY_SECONDS,
    ):
        self.db_path = Path(db_path)
        self._busy_ms = int(busy_timeout * 1000)
        self.scrub_retry_seconds = max(0.0, float(scrub_retry_seconds))
        self._scrub_deferred = 0
        self._scrub_dirty = False
        self._optimize_pending = False
        self._fts_rows_erased = False
        self._fts_residual_ids: set[str] = set()
        self._fts_erased_ids: set[str] = set()
        self._scrub_flag = False
        self.last_refusal: str | None = None
        # Fail closed: never operate without a gate.
        self.scope_engine = scope_engine if scope_engine is not None else ScopeEngine()
        self._lock = threading.RLock()
        self._ingestion_audits = None
        self._fts_epoch = 0
        self._fts_corpus = None  # (key, in-memory connection) for the last lexical query
        self._fts_corpus_builds = 0
        self.last_import_refusal: str | None = None
        self.last_scrub_complete: bool | None = None
        self.migration_reapproval: list[str] = []
        self.migration_quarantined: list[str] = []
        self.migration_cascaded: list[str] = []
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
            # A store written by a newer release may carry invariants this code does not know.
            # Refuse before anything is created or written (the read below changes nothing).
            found = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if found > SCHEMA_VERSION:
                raise SubstrateError(
                    f"database schema version {found} is newer than the supported version "
                    f"{SCHEMA_VERSION}; refusing to open (upgrade Aletheia)")
            # Switching a brand-new file to WAL needs an exclusive lock that busy_timeout does not
            # cover, so concurrent first opens can fail at once with 'database is locked'.
            self._retry_locked(lambda: self._conn.execute("PRAGMA journal_mode=WAL"), busy_timeout)
            self._conn.execute("PRAGMA secure_delete=ON")  # erased plaintext must not linger in free pages
            # Explicit, not the library default (ADR 0001 durability claim).
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._retry_locked(lambda: self._conn.executescript(_SCHEMA), busy_timeout)
            self._drop_legacy_tombstones()
            if self._conn.execute("PRAGMA user_version").fetchone()[0] >= 6:
                # The schema-7 migration reindexes rules; it needs the rowid hints first or it
                # cannot find (and so duplicates) the rows an older store already holds.
                self._backfill_fts_rowmap()
            self._migrate()
            self._backfill_fts_rowmap()
            self._repair_targetless_corrections()
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version' AND CAST(value AS INTEGER) < ?",
                (str(SCHEMA_VERSION), SCHEMA_VERSION),
            )
            self._conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('store_version', '0')")
            for suffix in ("", "-wal", "-shm"):
                path = str(self.db_path) + suffix
                if os.path.exists(path):
                    os.chmod(path, 0o600)
            if self.scrub_pending():
                # An earlier erase could not finish its WAL scrub: finish it now.
                self._optimize_pending = self._scrub_needs_optimize()
                self._fts_rows_erased = False
                self._finish_scrub(self.scrub_retry_seconds)
        except Exception:
            if hasattr(self, "_conn"):
                self._conn.close()
            raise
        finally:
            os.umask(old_umask)

    def _drop_legacy_tombstones(self) -> None:
        """Dev-schema cleanup: the round-7 ``erased_supersedes`` table kept the erased original's id.

        Keep only the boolean fact (the Correction's own id goes into ``lineage_erased``), drop the
        table and scrub it. No erased id is retained.
        """
        if self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='erased_supersedes'"
                              ).fetchone() is None:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO lineage_erased(record_id) SELECT e.record_id FROM erased_supersedes e "
                "JOIN records r ON r.record_id=e.record_id "
                "WHERE r.record_type='Correction' AND r.supersedes IS NULL")
            self._conn.execute("DROP TABLE erased_supersedes")
            self._set_scrub_flag(True)
            self._conn.execute("COMMIT")
        except Exception:
            self._rollback_if_open()
            raise
        self._scrub()

    def _scrub(self) -> None:
        """Push deleted plaintext out of the live DB files, once per outermost erase.

        FTS5 only marks removed rows; 'optimize' merges the segments so the deleted terms leave
        fts_data (skipped when the erase touched no FTS row), and a TRUNCATE checkpoint moves the
        WAL into the main file and empties it. With secure_delete=ON freed pages are zeroed.

        This never fails open silently: the logical erase is already committed, and if the
        checkpoint cannot finish inside ``scrub_retry_seconds`` (a reader holds a snapshot) a
        ``scrub_pending`` flag is persisted in ``meta``, a warning is logged,
        ``last_scrub_complete`` is False, and the scrub is retried at the start of the next
        write/open or by ``complete_scrub()``. Inside ``deferred_scrub()`` it is postponed to exit.
        Backups, snapshots and storage-media remanence are out of scope.
        """
        self._drop_fts_corpus()
        if self._conn.in_transaction:
            return
        if self._fts_rows_erased:
            self._optimize_pending = True
            self._fts_rows_erased = False
        if self._scrub_deferred:
            self._scrub_dirty = True
            self.last_scrub_complete = False
            if self._scrub_token() is None:
                self._set_scrub_flag(True)  # durable even if the process dies before the deferred scrub
            else:
                self._scrub_flag = True
            return
        self._finish_scrub(self.scrub_retry_seconds)

    def _set_scrub_flag(self, pending: bool, expected_value: str | None = None, *, fts: bool | None = None) -> bool:
        try:
            if pending:
                need_fts = fts if fts is not None else (
                    self._fts_rows_erased or self._optimize_pending or self._scrub_needs_optimize()
                )
                prefix = "fts:" if need_fts else "wal:"
                token = prefix + os.urandom(16).hex()
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('scrub_pending', ?)", (token,)
                )
                self._scrub_flag = True
                return True
            if expected_value is not None:
                cur = self._conn.execute(
                    "DELETE FROM meta WHERE key='scrub_pending' AND value=?", (expected_value,)
                )
                cleared = (cur.rowcount if cur is not None else 0) > 0
                self._scrub_flag = not cleared and self.scrub_pending()
                return cleared
            self._conn.execute("DELETE FROM meta WHERE key='scrub_pending'")
            self._scrub_flag = False
            return True
        except sqlite3.Error:
            self._scrub_flag = pending or self._scrub_flag
            return not pending

    def _scrub_token(self) -> str | None:
        try:
            cur = self._conn.execute("SELECT value FROM meta WHERE key='scrub_pending'")
            row = cur.fetchone() if cur is not None else None
            return str(row[0]) if row is not None else None
        except sqlite3.Error:
            return None

    def _scrub_needs_optimize(self) -> bool:
        tok = self._scrub_token()
        return tok is not None and not tok.startswith("wal:")

    def _fts_ids_remaining(self, ids) -> list[str]:
        """Which of ``ids`` still have an orphaned FTS row (no live record in ``records``)."""
        ids = list(ids)
        left: list[str] = []
        for i in range(0, len(ids), 400):  # one index scan per chunk, not per id
            chunk = ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            left.extend(r[0] for r in self._conn.execute(
                f"SELECT DISTINCT f.record_id FROM fts f "
                f"WHERE f.record_id IN ({marks}) "
                f"AND NOT EXISTS (SELECT 1 FROM records r WHERE r.record_id = f.record_id)", chunk))
        return left

    def _scrub_attempts(self, window: float) -> bool:
        """optimize (if owed) + TRUNCATE checkpoint, retried with bounded backoff. True if complete."""
        deadline = time.monotonic() + max(window, 0.0)
        delay = 0.01
        self._conn.execute("PRAGMA busy_timeout = 0")  # the loop below owns the waiting
        try:
            while True:
                try:
                    if self._optimize_pending:
                        self._conn.execute("INSERT INTO fts(fts) VALUES('optimize')")
                        self._optimize_pending = False
                    row = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if row and row[0] == 0:
                        return True
                except sqlite3.OperationalError:
                    pass
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.25)
        finally:
            self._conn.execute(f"PRAGMA busy_timeout = {self._busy_ms}")

    def _finish_scrub(self, window: float) -> bool:
        token_before = self._scrub_token()
        if token_before is not None and not token_before.startswith("wal:"):
            self._optimize_pending = True
        to_check = self._fts_residual_ids | self._fts_erased_ids
        self._fts_erased_ids = set()
        self._fts_residual_ids = to_check  # unverified ids keep the scrub incomplete
        try:
            if to_check:
                # Ids a previous scrub still found orphaned are erased again, then all are verified.
                self._fts_residual_ids = set(self._fts_ids_remaining(to_check))
                for rid in self._fts_residual_ids:
                    if self._conn.execute("SELECT 1 FROM records WHERE record_id=?", (rid,)).fetchone() is not None:
                        continue
                    self._conn.execute("DELETE FROM fts WHERE record_id=?", (rid,))
                    self._conn.execute("DELETE FROM fts_rowmap WHERE record_id=?", (rid,))
                    self._optimize_pending = True
                if self._fts_residual_ids:
                    self._fts_residual_ids = set(self._fts_ids_remaining(self._fts_residual_ids))
        except sqlite3.Error:
            pass  # the checkpoint below still runs; the residual set keeps the scrub incomplete
        try:
            done = self._scrub_attempts(window) and not self._fts_residual_ids
        except sqlite3.Error:
            done = False
        if done and token_before is not None:
            if not self._set_scrub_flag(False, expected_value=token_before):
                # A newer erase committed on another connection while we were scrubbing;
                # keep scrub_pending set and report that the store still has a pending scrub.
                done = False
        elif done:
            if self.scrub_pending():
                # An erase occurred after token_before was read (when it was None)
                done = False
            else:
                self._scrub_flag = False
        self.last_scrub_complete = done
        if not done:
            was = self._scrub_flag
            if self._scrub_token() is None:
                self._set_scrub_flag(True)
            else:
                self._scrub_flag = True
            logging.getLogger("aletheia.substrate").warning(
                "erase committed but the WAL/FTS scrub is incomplete (a concurrent reader or an "
                "index row remains); deleted text may still be in %s-wal. scrub_pending is set; it "
                "is retried at the next write/open or by Substrate.complete_scrub()%s",
                self.db_path, " (already pending)" if was else "")
        return done

    def scrub_pending(self) -> bool:
        """True while an erase has committed but its WAL/FTS scrub has not completed."""
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM meta WHERE key='scrub_pending'")
            row = cur.fetchone() if cur is not None else None
            self._scrub_flag = row is not None
            return self._scrub_flag

    def complete_scrub(self) -> bool:
        """Retry a pending scrub now (bounded by ``scrub_retry_seconds``). True once nothing is pending."""
        with self._lock:
            if self._conn.in_transaction or self._scrub_deferred:
                return False
            if not self.scrub_pending():
                return True
            self._optimize_pending = True
            return self._finish_scrub(self.scrub_retry_seconds)

    def _auto_retry_scrub(self) -> None:
        """Start of a write: one non-blocking attempt at a pending scrub (never waits on a reader)."""
        if not self._scrub_deferred and not self._conn.in_transaction and (self._scrub_flag or self.scrub_pending()):
            self._finish_scrub(0.0)

    @contextmanager
    def deferred_scrub(self):
        """Bulk erase: postpone the optimize + checkpoint to one run when the outermost block exits."""
        with self._lock:
            self._scrub_deferred += 1
        try:
            yield
        finally:
            with self._lock:
                self._scrub_deferred -= 1
                if self._scrub_deferred == 0 and self._scrub_dirty:
                    self._scrub_dirty = False
                    self._scrub()

    def _redact_journal(self, record_id: str) -> None:
        """An erased record keeps its audit rows (op, scope, time) but not its id or free text."""
        self._conn.execute(
            "UPDATE journal SET detail=? WHERE record_id=? AND op IN ('quarantine', 'delete', "
            "'delete_quarantined', 'prune_derived')", (f"reason={_ERASED}", record_id))
        self._conn.execute("UPDATE journal SET record_id=? WHERE record_id=?", (_ERASED, record_id))

    def _repair_targetless_corrections(self) -> None:
        """A Correction with no supersedes can only come from an erased original (v6 delete() made them).

        Mark such rows lineage_erased so the scope export restores. Idempotent; writes only if needed.
        """
        sql = ("SELECT record_id FROM records WHERE record_type='Correction' AND supersedes IS NULL "
               "AND record_id NOT IN (SELECT record_id FROM lineage_erased)")
        if self._conn.execute(sql).fetchone() is None:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(f"INSERT OR IGNORE INTO lineage_erased(record_id) {sql}")
            self._conn.execute("COMMIT")
        except Exception:
            self._rollback_if_open()
            raise

    # ---- transaction helpers ------------------------------------------

    @staticmethod
    def _retry_locked(operation, timeout: float):
        """Run a connection-setup step, retrying 'database is locked' for up to ``timeout`` seconds."""
        deadline = time.monotonic() + max(timeout, 1.0)
        delay = 0.005
        while True:
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)

    def _drop_fts_corpus(self) -> None:
        cached, self._fts_corpus = self._fts_corpus, None
        if cached is not None:
            cached[1].close()

    def _backfill_fts_rowmap(self) -> None:
        """One-time (per store) fill of fts_rowmap for stores written before it existed."""
        if self._conn.execute("SELECT 1 FROM meta WHERE key='fts_rowmap_ready'").fetchone():
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("DELETE FROM fts_rowmap")
            self._conn.execute(
                "INSERT INTO fts_rowmap(record_id, fts_rowid) "
                "SELECT record_id, max(rowid) FROM fts GROUP BY record_id")
            self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('fts_rowmap_ready','1')")
            self._conn.execute("COMMIT")
        except Exception:
            self._rollback_if_open()
            raise

    def _rollback_if_open(self) -> None:
        # Review fix F14: only roll back a transaction that actually exists.
        self._drop_fts_corpus()
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")

    def _rollback_savepoint(self, name: str) -> None:
        # A SQLite RAISE(ROLLBACK) may already have ended the whole transaction.
        self._drop_fts_corpus()
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
            self._auto_retry_scrub()
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
            self._auto_retry_scrub()
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
            own = self._begin_write("aletheia_journal")
            self._journal(op, record_id, scope, detail)
            self._finish_write("aletheia_journal", own)
            return own  # A savepoint release does not commit the outer transaction.
        except sqlite3.Error:
            self._abort_write("aletheia_journal", own)
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
            # Another opener may have migrated between the read above and this write lock;
            # rerunning (notably the rule re-approval step) would undo work done since.
            if self._conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
                self._conn.execute("ROLLBACK")
                return
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
            if version < 7:
                self._quarantine_invalid_records()
            if version < 3:
                self._conn.execute("DELETE FROM fts")
                self._conn.execute("DELETE FROM fts_rowmap")
                for row in self._conn.execute("SELECT * FROM records").fetchall():
                    self._fts_index(Record.from_row(row))
            if version < 8:
                # Version 8 (Phase 12): typed relations between records, scoped and time-bounded.
                # `entity_links` answers "which record mentions this string"; this answers "which
                # records are related, and how" — subject and object are record ids, so an edge can
                # never point outside the substrate or outside a scope.
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS relations (
                        subject     TEXT NOT NULL,
                        predicate   TEXT NOT NULL,
                        object      TEXT NOT NULL,
                        scope       TEXT NOT NULL,
                        confidence  REAL NOT NULL DEFAULT 1.0,
                        valid_from  REAL,
                        valid_to    REAL,
                        source_ids  TEXT NOT NULL DEFAULT '[]',
                        created_at  REAL NOT NULL,
                        PRIMARY KEY (subject, predicate, object, scope),
                        FOREIGN KEY (subject) REFERENCES records(record_id) ON DELETE CASCADE,
                        FOREIGN KEY (object) REFERENCES records(record_id) ON DELETE CASCADE
                    )
                """)
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_object ON relations(object)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_scope ON relations(scope)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_predicate ON relations(predicate)")
            if version < 6:
                # Version 6: switch FTS5 to porter stemming tokenizer.
                self._conn.execute("DROP TABLE IF EXISTS fts")
                self._conn.execute("DELETE FROM fts_rowmap")
                self._conn.execute(
                    "CREATE VIRTUAL TABLE fts USING fts5(record_id UNINDEXED, scope UNINDEXED, text, tokenize='porter unicode61')"
                )
                for row in self._conn.execute("SELECT * FROM records").fetchall():
                    self._fts_index(Record.from_row(row))
            if version < 7:
                self._migrate_rule_approvals()
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.execute("COMMIT")
        except Exception:
            self._rollback_if_open()
            raise

    def _quarantine_invalid_records(self) -> None:
        """Version 7: move legacy rows that violate the integrity rules into ``quarantined_records``.

        Earlier versions accepted records whose payload text is not a string or whose timestamps
        are not finite. Reads (HTTP /query, /shadow/*, recall) assume the rules new writes obey,
        so one such row would make every read of its scope fail. The row is preserved verbatim,
        its index rows are dropped, and each is journalled and listed in ``migration_quarantined``
        (and logged). Nothing reads it meanwhile. It is erasable: ``delete(id)`` (and
        ``prune_derived``) remove the quarantined copy under the usual delete authority, and
        ``list_quarantined`` / ``purge_quarantined`` enumerate and erase quarantined rows.
        """
        moved = []
        for row in self._conn.execute("SELECT * FROM records").fetchall():
            try:
                self._validate_integrity(Record.from_row(row))
                continue
            except (SchemaViolation, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
                reason = f"invalid legacy record: {exc}"
            moved.append((row, reason))
        cascaded = 0
        for row, reason in moved:
            rid = row[0]
            if self._conn.execute("SELECT 1 FROM records WHERE record_id=?", (rid,)).fetchone() is None:
                continue  # already erased by an earlier row's lineage cascade
            # A Correction of the quarantined original must not dangle (same repair as delete()).
            for dep_id, dep_scope in self._conn.execute(
                    "SELECT record_id, scope FROM records WHERE supersedes=?", (rid,)).fetchall():
                self._unlink_supersedes(dep_id)
                self._journal("unlink_supersedes", dep_id, dep_scope,
                              "cleared dangling supersedes link (target quarantined)")
            # Derived records citing the quarantined original go with it (delete()'s lineage
            # cascade), so no citation dangles. A retained non-derived citer cannot be changed here.
            try:
                members, _blocked = self._lineage(rid, row[2]) if isinstance(row[2], str) else ([], False)
            except SubstrateError:
                members = []  # corrupt source_ids somewhere: leave lineage alone rather than refuse to open
            for sid, sscope in members:
                for dep_id, dep_scope in self._conn.execute(
                        "SELECT record_id, scope FROM records WHERE supersedes=?", (sid,)).fetchall():
                    self._unlink_supersedes(dep_id)
                    self._journal("unlink_supersedes", dep_id, dep_scope,
                                  "cleared dangling supersedes link (target deleted)")
                self._remove_lineage_member(sid, sscope)
                self._journal("unlink_summary", None, sscope, "removed derived artifact (cascade; source quarantined)")
                self.migration_cascaded.append(sid)
                cascaded += 1
            self._conn.execute(
                "INSERT OR REPLACE INTO quarantined_records(record_id, reason, record_type, scope, "
                "epistemic_status, payload, created_at, effective_at, supersedes, source_ids, provenance, "
                "attribution, valid_from, valid_to) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, reason) + tuple(row[1:14]),
            )
            self._conn.execute("DELETE FROM records WHERE record_id=?", (rid,))
            self._fts_drop_row(rid)
            if self._conn.execute("SELECT 1 FROM fts WHERE record_id=? LIMIT 1", (rid,)).fetchone() is not None:
                self._conn.execute("DELETE FROM fts WHERE record_id=?", (rid,))
            self._fts_rows_erased = True
            self._conn.execute("DELETE FROM entity_links WHERE source_id=? OR target_id=?", (rid, rid))
            for table in self._vec_tables_for(row[2]) if isinstance(row[2], str) else []:
                self._conn.execute(f"DELETE FROM {table} WHERE record_id = ?", (rid,))
            self._journal("quarantine", rid, row[2] if isinstance(row[2], str) else None, reason)
            self.migration_quarantined.append(rid)
        if self.migration_quarantined:
            qset = set(self.migration_quarantined) | set(self.migration_cascaded)
            while True:
                changed = False
                for r_id, r_type, r_status, r_scope, r_src in self._conn.execute(
                        "SELECT record_id, record_type, epistemic_status, scope, source_ids FROM records").fetchall():
                    try:
                        src_list = json.loads(r_src)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(src_list, list) or not any(s in qset for s in src_list):
                        continue
                    kept = [s for s in src_list if s not in qset]
                    if not kept and (r_status in ("derived", "verified") or r_type == "Observation"):
                        for dep_id, dep_scope in self._conn.execute(
                                "SELECT record_id, scope FROM records WHERE supersedes=?", (r_id,)).fetchall():
                            self._unlink_supersedes(dep_id)
                            self._journal("unlink_supersedes", dep_id, dep_scope,
                                          "cleared dangling supersedes link (target deleted)")
                        self._remove_lineage_member(r_id, r_scope if isinstance(r_scope, str) else "")
                        self._journal("unlink_summary", None, r_scope if isinstance(r_scope, str) else None,
                                      "removed artifact whose only cited source was quarantined")
                        self.migration_cascaded.append(r_id)
                        qset.add(r_id)
                        cascaded += 1
                        changed = True
                    else:
                        self._conn.execute("UPDATE records SET source_ids=? WHERE record_id=?",
                                           (json.dumps(kept), r_id))
                        self._journal("unlink_citation", r_id, r_scope if isinstance(r_scope, str) else None,
                                      "removed dangling citation to quarantined legacy record")
                        changed = True
                if not changed:
                    break
        if moved or cascaded:
            self._set_scrub_flag(True)  # erased or quarantined text: the open-time scrub completes it
        if cascaded:
            logging.getLogger("aletheia.substrate").warning(
                "schema-7 migration also removed %d derived record(s) citing a quarantined source "
                "(listed in migration_cascaded)", cascaded)
        if moved:
            logging.getLogger("aletheia.substrate").warning(
                "schema-7 migration quarantined %d invalid legacy record(s) in %s "
                "(table quarantined_records); they are hidden from every read", len(moved), self.db_path)

    def _migrate_rule_approvals(self) -> None:
        """Version 7: approval bindings (rule_incarnations / rule_approvals).

        Stores written before this version have no incarnation or binding for any rule, so no
        stored approval can be verified. Fail closed, but not silently: every ProposedRule
        without an incarnation gets one, and a formerly active rule goes back to 'proposed'
        (promotion fields removed) so it is visible and re-approvable instead of stranded as
        'active' yet inert. Old Decisions are never trusted; a fresh structured approval
        ({"rule_id": ..., "approved": true}) is required. Each demotion is journalled and listed
        in ``migration_reapproval``. This holds even when the store already has incarnation and
        approval rows: those came from the unversioned implementation and are discarded.
        """
        # Bindings written before this version (by the unversioned implementation, or none at all)
        # are untrusted: drop them all and re-token every rule, so each rule needs a fresh approval.
        self._conn.execute("DELETE FROM rule_approvals")
        rows = self._conn.execute("SELECT * FROM records WHERE record_type='ProposedRule'").fetchall()
        for row in rows:
            rule = Record.from_row(row)
            payload = rule.payload if isinstance(rule.payload, dict) else {}
            superseded = self._conn.execute(
                "SELECT 1 FROM records WHERE supersedes=? AND record_type='Correction'",
                (rule.record_id,)).fetchone() is not None
            if payload.get("status") == "active" and superseded:
                # An active rule a Correction supersedes is inert; it cannot go back to 'proposed'
                # (its export would be refused: a Correction of a live rule).
                payload = {k: v for k, v in payload.items() if k not in ("promoted_by", "promoted_at")}
                payload["status"] = "deprecated"
                self._conn.execute("UPDATE records SET payload=? WHERE record_id=?",
                                   (json.dumps(payload, sort_keys=True), rule.record_id))
                self._fts_index(self._lookup_record(rule.record_id))
                self._journal("migrate_rule_deprecate", rule.record_id, rule.scope,
                              "active rule superseded by a Correction deprecated in schema 7")
            elif payload.get("status") == "active":
                payload = {k: v for k, v in payload.items() if k not in ("status", "promoted_by", "promoted_at")}
                payload["status"] = "proposed"
                self._conn.execute("UPDATE records SET payload=? WHERE record_id=?",
                                   (json.dumps(payload, sort_keys=True), rule.record_id))
                self._fts_index(self._lookup_record(rule.record_id))
                self._journal("migrate_rule_reapproval", rule.record_id, rule.scope,
                              "active rule returned to proposed: approvals are re-recorded in schema 7")
                self.migration_reapproval.append(rule.record_id)
            elif payload.get("status", "proposed") == "proposed" and superseded:
                # Old correct() left a corrected rule 'proposed'. Current correct() deprecates it, and
                # an export of the stale state would not restore (a Correction of a live rule).
                payload = {**payload, "status": "deprecated"}
                self._conn.execute("UPDATE records SET payload=? WHERE record_id=?",
                                   (json.dumps(payload, sort_keys=True), rule.record_id))
                self._fts_index(self._lookup_record(rule.record_id))
                self._journal("migrate_rule_deprecate", rule.record_id, rule.scope,
                              "proposed rule superseded by a Correction deprecated in schema 7")
            self._conn.execute("INSERT OR REPLACE INTO rule_incarnations VALUES (?, ?)",
                               (rule.record_id, os.urandom(32).hex()))

    # ---- verifiers (D2-9) ---------------------------------------------

    def register_verifier(self, actor_id: str, ctx: ScopeContext | None) -> None:
        """Register a verifier. Only a user-authored turn may do this (D2-9).

        The agent cannot create, modify, or grant a verifier.
        """
        with self._lock:
            if ctx is None or not ctx.user_authored:
                raise ScopeViolation("verifier registration not permitted")
            _check_id(actor_id)
            self._auto_retry_scrub()
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

    def _fts_drop_row(self, record_id: str) -> None:
        """Delete a record's fts row by rowid (point lookup); the record_id re-check guards a stale hint."""
        self._fts_epoch += 1
        hint = self._conn.execute(
            "SELECT fts_rowid FROM fts_rowmap WHERE record_id=?", (record_id,)).fetchone()
        if hint is not None:
            self._conn.execute("DELETE FROM fts WHERE rowid=? AND record_id=?", (hint[0], record_id))
            self._conn.execute("DELETE FROM fts_rowmap WHERE record_id=?", (record_id,))

    def _fts_index(self, record) -> None:
        self._fts_erased_ids.discard(record.record_id)
        self._fts_residual_ids.discard(record.record_id)
        self._fts_drop_row(record.record_id)
        cursor = self._conn.execute(
            "INSERT INTO fts(record_id, scope, text) VALUES (?,?,?)",
            (record.record_id, record.scope, self._payload_only_text(record)),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO fts_rowmap(record_id, fts_rowid) VALUES (?,?)",
            (record.record_id, cursor.lastrowid))

    def rebuild_fts(self, scope: str, ctx) -> None:
        """Rebuild one granted scope's derived index, atomically from records."""
        with self._lock:
            own = self._begin_write("aletheia_fts_rebuild")
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
                self._fts_epoch += 1
                self._conn.execute("DELETE FROM fts WHERE scope=?", (scope,))
                self._conn.execute(
                    "DELETE FROM fts_rowmap WHERE record_id IN (SELECT record_id FROM records WHERE scope=?)",
                    (scope,))
                for rec in records:
                    self._fts_index(rec)
                self._journal("fts_rebuilt", None, scope, "scope index rebuilt")
                self._finish_write("aletheia_fts_rebuild", own)
            except BaseException:
                self._abort_write("aletheia_fts_rebuild", own)
                raise

    def _fts_remove(self, record_id: str) -> None:
        """Erase a record's FTS row. A missing/stale rowid hint falls back to a scan by record_id."""
        self._fts_epoch += 1
        hint = self._conn.execute(
            "SELECT fts_rowid FROM fts_rowmap WHERE record_id=?", (record_id,)).fetchone()
        removed = 0
        if hint is not None:
            removed = self._conn.execute(
                "DELETE FROM fts WHERE rowid=? AND record_id=?", (hint[0], record_id)).rowcount
            self._conn.execute("DELETE FROM fts_rowmap WHERE record_id=?", (record_id,))
        if hint is None or removed < 1:
            # Slow path (the hint is derived data and may be absent, e.g. rows written by an older
            # build): locate every row by record_id, erase, then verify.
            removed += self._conn.execute("DELETE FROM fts WHERE record_id=?", (record_id,)).rowcount
            self._conn.execute("DELETE FROM fts_rowmap WHERE record_id=?", (record_id,))
        self._fts_erased_ids.add(record_id)  # verified once, at scrub time
        if removed > 0:
            self._fts_rows_erased = True

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
            own = self._begin_write("aletheia_job")
            try:
                self._conn.execute(
                    "INSERT INTO jobs(scope, source_id, stage, content_hash, state, reason) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(scope, source_id, stage, content_hash) DO UPDATE SET state=excluded.state, reason=excluded.reason "
                    "WHERE jobs.state != 'done' OR excluded.state = 'done'",
                    (scope, source_id, stage, content_hash, state, reason),
                )
                self._finish_write("aletheia_job", own)
            except Exception:
                self._abort_write("aletheia_job", own)
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

    def _readable_fts_records(self, scope: str, ctx):
        """Validate one scope's projection with linear scans in the current snapshot.

        FTS5's UNINDEXED record_id cannot support a point lookup: issuing one per
        record is quadratic. Keep all index rows by id so duplicates and misplaced
        entries for readable records remain detectable, including foreign-scope ones.
        """
        records = [Record.from_row(row) for row in self._conn.execute(
            "SELECT * FROM records WHERE scope=?", (scope,))]
        all_ids = {row[0] for row in self._conn.execute("SELECT record_id FROM records")}
        indexed = {}
        scoped_ids = {rec.record_id for rec in records}
        for rid, indexed_scope, text in self._conn.execute("SELECT record_id, scope, text FROM fts"):
            # An orphan belongs to the scope its own row names, whatever scopes this engine knows
            # (a per-request engine knows only the request scope). Another scope's orphan is found
            # by that scope's own check. Only a row naming no scope at all is unattributable.
            if rid not in all_ids and (indexed_scope == scope or indexed_scope is None):
                return None
            if rid in scoped_ids:
                indexed.setdefault(rid, []).append((indexed_scope, text))
        readable = []
        for rec in records:
            if (not isinstance(rec.payload, dict) or not isinstance(rec.provenance, dict)
                    or not isinstance(rec.source_ids, list)
                    or not all(isinstance(item, str) for item in rec.source_ids)):
                return None
            if not self.scope_engine.authorize_read(
                    scope, ctx, self.scope_engine._payload_text(rec)).allowed:
                continue
            if indexed.get(rec.record_id, []) != [(scope, self._payload_only_text(rec))]:
                return None
            readable.append(rec)
        return readable

    def fts_index_complete(self, scope: str, ctx) -> bool:
        """Check readable record projections and stray entries in one scope."""
        if ctx is None or not self.scope_engine.authorize_read(scope, ctx, "").allowed:
            raise ScopeViolation("read not permitted")
        import contextlib
        with self._lock:
            transaction = contextlib.nullcontext() if self._conn.in_transaction else self._read_transaction()
            with transaction:
                return self._readable_fts_records(scope, ctx) is not None

    def vector_index_complete(self, scope: str, ctx, *, model_id: str) -> bool:
        """Whether every readable record has a valid projection and model vector."""
        if ctx is None or not self.scope_engine.authorize_read(scope, ctx, "").allowed:
            raise ScopeViolation("read not permitted")
        import contextlib
        with self._lock:
            transaction = contextlib.nullcontext() if self._conn.in_transaction else self._read_transaction()
            with transaction:
                records = self._readable_fts_records(scope, ctx)
                if records is None:
                    return False
                table = self._vec_table(scope, model_id)
                # A record deliberately superseded by a Correction loses its vectors (its text
                # no longer describes the current state), so it is exempt, not missing.
                superseded = {row[0] for row in self._conn.execute(
                    "SELECT supersedes FROM records WHERE scope=? AND supersedes IS NOT NULL", (scope,))}
                required = {rec.record_id for rec in records
                            if rec.record_type in EMBEDDABLE_TYPES and rec.record_id not in superseded
                            and self._embed_text(rec) is not None}
                if not required:
                    return True
                if not self._vec_exists(scope, model_id):
                    return False
                for rid, blob in self._conn.execute(f"SELECT record_id, embedding FROM {table}"):
                    if rid not in required:
                        continue
                    if len(blob) != VEC_DIM * 4 or not all(
                            math.isfinite(value[0]) for value in struct.iter_unpack('=f', blob)):
                        return False
                    required.remove(rid)
                return not required

    @staticmethod
    def _embed_text(rec: Record) -> str | None:
        """The stripped payload text a record is embedded from, or None when it has none.

        A record with no embeddable text can never get a vector, so it is exempt from vector
        completeness (otherwise one text-less HTTP-ingested record would disable hybrid recall).
        """
        text = rec.payload.get("text") if isinstance(rec.payload, dict) else None
        return text.strip() if isinstance(text, str) and text.strip() else None

    REEMBED_TEXT_CHARS = 2000  # same opening-text bound the document ingest embeds

    def reembed_missing(self, scope: str, ctx, embedder, *, limit: int | None = None) -> dict[str, int]:
        """Embed the readable embeddable records of ``scope`` that have no vector for the model.

        The supported repair for a scope whose vector index is incomplete (records ingested over
        HTTP, written without an embedder, or left bare by an older correction). Needs write
        authority over the scope, like ``vector_put``. Idempotent: records that already have a
        vector are untouched. Text is the record's payload text, bounded by REEMBED_TEXT_CHARS.
        Returns counts: embedded, already_present, skipped_no_text, remaining (when ``limit`` cut it short).
        """
        stub = Record(record_id="reembed", record_type="Source", scope=scope, payload={},
                      attribution="document", source_ids=[])
        if ctx is None or not self.scope_engine.authorize_write(stub, ctx).allowed:
            raise ScopeViolation("write not permitted")
        model_id = embedder.model_id
        with self._lock:
            have = set()
            if self._vec_exists(scope, model_id):
                have = {row[0] for row in self._conn.execute(
                    f"SELECT record_id FROM {self._vec_table(scope, model_id)}")}
            todo, present, no_text = [], 0, 0
            for row in self._conn.execute(
                    "SELECT * FROM records WHERE scope=? ORDER BY created_at, record_id", (scope,)).fetchall():
                rec = Record.from_row(row)
                if (rec.record_type not in EMBEDDABLE_TYPES or not isinstance(rec.payload, dict)
                        or not self.scope_engine.authorize_read(
                            scope, ctx, self.scope_engine._payload_text(rec)).allowed):
                    continue
                if rec.record_id in have:
                    present += 1
                    continue
                text = self._embed_text(rec)
                if text is None:
                    no_text += 1
                    continue
                todo.append((rec.record_id, text[: self.REEMBED_TEXT_CHARS], _sha256(text)))
        remaining = 0
        if limit is not None and len(todo) > limit:
            remaining, todo = len(todo) - limit, todo[:limit]
        embedded = changed = 0
        for rid, text, digest in todo:  # embed outside the lock; the text is re-verified under it
            vector = embedder.embed_document(text)
            try:
                self.vector_put(rid, scope, vector, ctx, model_id=model_id, _expect_text_hash=digest)
            except _StaleVector:
                changed += 1  # overwritten/deleted while embedding: the vector would be stale
                continue
            embedded += 1
        return {"embedded": embedded, "already_present": present, "skipped_no_text": no_text,
                "skipped_changed": changed, "remaining": remaining}

    def vector_put(self, record_id: str, scope: str, vector, ctx=None, *, model_id: str,
                   _expect_text_hash: str | None = None) -> None:
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
            own = self._begin_write("aletheia_vector")
            try:
                # The writer reservation covers lookup, authorization and mutation,
                # including against writers using independent SQLite connections.
                stored = self._lookup_record(record_id)
                if (ctx is None or stored is None or stored.scope != scope
                        or not self.scope_engine.authorize_write(stored, ctx).allowed):
                    raise ScopeViolation("write not permitted")
                if _expect_text_hash is not None:
                    now = self._embed_text(stored)
                    if now is None or _sha256(now) != _expect_text_hash:
                        raise _StaleVector(record_id)
                self._conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING "
                    f"vec0(record_id TEXT PRIMARY KEY, embedding float[{VEC_DIM}])"
                )
                self._conn.execute(f"DELETE FROM {table} WHERE record_id = ?", (record_id,))
                self._conn.execute(
                    f"INSERT INTO {table}(record_id, embedding) VALUES (?, ?)",
                    (record_id, sqlite_vec.serialize_float32(list(vector))),
                )
                self._finish_write("aletheia_vector", own)
            except Exception:
                self._abort_write("aletheia_vector", own)
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
        with self._lock:
            params = ()
            where = ""
            if scopes is not None:
                if not scopes:
                    return []
                placeholders = ",".join("?" * len(scopes))
                where = f" WHERE records.scope IN ({placeholders})"
                params = tuple(scopes)
            local = self._fts_visible_corpus(ctx, scopes, where, params)
            return [row[0] for row in local.execute(
                "SELECT record_id FROM corpus WHERE corpus MATCH ? ORDER BY bm25(corpus), rowid LIMIT ?",
                (match, limit)).fetchall()]

    def _fts_visible_corpus(self, ctx, scopes, where: str, params: tuple):
        """In-memory FTS over exactly the rows ctx may read in ``scopes``.

        BM25 statistics must come only from that readable corpus, never the shared table, so ranking
        in one scope is not influenced by another. Building it is O(corpus), so the last corpus is
        kept and reused while every input that decides its content is unchanged: the store's
        write/index epochs, the caller's grants, scope names and content markers. Any rollback drops
        it. Raw edits to the fts table that bypass Substrate are not tracked here; the completeness
        check reads the table directly and still fails closed on them.
        """
        engine = self.scope_engine
        version = self._conn.execute("SELECT value FROM meta WHERE key='store_version'").fetchone()
        key = (
            version[0] if version else None,
            self._conn.execute("PRAGMA data_version").fetchone()[0],
            self._fts_epoch,
            None if scopes is None else tuple(sorted(scopes)),
            (frozenset(ctx.granted_scopes), bool(ctx.user_authorized_cross_scope),
             bool(ctx.user_authored), frozenset(ctx.cross_scope_targets),
             bool(ctx.authorization_source)),
            frozenset(engine.known_scopes),
            frozenset(engine.global_allowlist) if hasattr(engine, "global_allowlist") else None,
            tuple(sorted((name, tuple(sorted(ms))) for name, ms in engine.markers.items())),
        )
        if self._fts_corpus is not None and self._fts_corpus[0] == key:
            return self._fts_corpus[1]
        # One joined read; authorization still looks at every record's own text.
        rows = self._conn.execute(
            "SELECT records.*, fts.text FROM fts JOIN records ON records.record_id=fts.record_id" + where,
            params).fetchall()
        visible = []
        for row in rows:
            rec = Record.from_row(row[:-1])
            if engine.authorize_read(rec.scope, ctx, engine._payload_text(rec)).allowed:
                visible.append((rec.record_id, row[-1]))
        self._drop_fts_corpus()
        local = sqlite3.connect(':memory:', check_same_thread=False)
        local.execute("CREATE VIRTUAL TABLE corpus USING fts5(record_id UNINDEXED, text, tokenize='porter unicode61')")
        local.executemany("INSERT INTO corpus(record_id,text) VALUES (?,?)", visible)
        self._fts_corpus_builds += 1
        self._fts_corpus = (key, local)
        return local

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
            self._auto_retry_scrub()
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

    def _summary_sources_available(self, record: Record, ctx: ScopeContext | None) -> bool:
        """Every citation must resolve inside the record's own scope.

        A missing id and a foreign-scope id are one refusal, so a writer learns nothing about
        other scopes. Derived records and Observations must also cite readable sources.
        """
        derived = record.epistemic_status == 'derived' or record.record_type == 'Observation'
        for source_id in record.source_ids:
            source = self._lookup_record(source_id)
            if source is None or source.scope != record.scope:
                return False
            if derived and not self.scope_engine.authorize_read(
                    source.scope, ctx, self.scope_engine._payload_text(source)).allowed:
                return False
        return True

    @staticmethod
    def _same_rule_proposal(old: Record, new: Record) -> bool:
        before = Substrate._record_to_export(old)
        after = Substrate._record_to_export(new)
        before['payload'] = {k: v for k, v in old.payload.items() if k not in _RULE_LIFECYCLE_KEYS}
        after['payload'] = {k: v for k, v in new.payload.items() if k not in _RULE_LIFECYCLE_KEYS}
        return before == after

    @staticmethod
    def _proposal_hash(record: Record) -> str:
        data = Substrate._record_to_export(record)
        data['payload'] = {k: v for k, v in record.payload.items()
                           if k not in _RULE_LIFECYCLE_KEYS}
        return _sha256(json.dumps(data, sort_keys=True, ensure_ascii=True, allow_nan=False))

    @staticmethod
    def _approval_target(record: Record) -> str | None:
        """The rule a user Decision/Commitment affirmatively approves, else None.

        Approval is structured, never inferred from naming or prose: the payload must be
        exactly {"rule_id": <id>, "approved": True}. Rejections, free text, applies_to,
        source_ids and any extra field never approve. Legacy records fail closed.
        """
        payload = record.payload
        if (record.record_type not in ('Decision', 'Commitment') or record.attribution != 'user'
                or not isinstance(payload, dict) or set(payload) != {'approved', 'rule_id'}
                or payload['approved'] is not True or not isinstance(payload['rule_id'], str)):
            return None
        return payload['rule_id']

    def _valid_rule_promoter(self, record: Record, ctx: ScopeContext) -> bool:
        if self._conn.execute("SELECT 1 FROM records WHERE supersedes=? LIMIT 1", (record.record_id,)).fetchone():
            return False
        promoter_id = (record.payload or {}).get("promoted_by")
        if not isinstance(promoter_id, str) or not promoter_id:
            return False
        promoter = self._lookup_record(promoter_id)
        # Time-bounded approval cannot support an indefinitely active rule:
        # reject even a future valid_to rather than expose an expired approval.
        if (promoter is None or promoter.record_type not in ("Decision", "Commitment")
                or promoter.attribution != "user" or promoter.scope != record.scope
                or promoter.valid_to is not None
                or (promoter.valid_from is not None and promoter.valid_from > time.time())
                or (promoter.effective_at is not None and promoter.effective_at > time.time())
                or self._conn.execute("SELECT 1 FROM records WHERE supersedes=? LIMIT 1", (promoter_id,)).fetchone()
                or not self.scope_engine.authorize_read(
                    promoter.scope, ctx, self.scope_engine._payload_text(promoter)).allowed):
            return False
        # Legacy records without an explicit affirmative and target fail closed.
        if self._approval_target(promoter) != record.record_id:
            return False
        binding = self._conn.execute(
            "SELECT a.proposal_hash FROM rule_approvals a JOIN rule_incarnations i "
            "ON i.rule_id=a.rule_id AND i.token=a.token WHERE a.promoter_id=? AND a.rule_id=?",
            (promoter_id, record.record_id)).fetchone()
        return binding is not None and binding[0] == self._proposal_hash(record)

    def _active_rule_relies_on(self, record_id: str) -> bool:
        # Deactivate the rule before changing or removing its promoter.
        return self._conn.execute(
            "SELECT 1 FROM records WHERE record_type='ProposedRule' "
            "AND json_extract(payload, '$.status')='active' "
            "AND json_extract(payload, '$.promoted_by')=? LIMIT 1", (record_id,)
        ).fetchone() is not None

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
        self._validate_integrity(record)
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
            own = self._begin_write("aletheia_write")

            if original_id is not None:
                # Scope before relevance: whether the original is missing, unreadable or in a
                # foreign scope must be decided (identically) before anything about the original's
                # role, such as being an active rule's promoter, can influence the answer.
                orig_row = self._conn.execute(
                    "SELECT * FROM records WHERE record_id=?", (original_id,)
                ).fetchone()
                if orig_row is None:
                    # G9: identical to an unreadable original, audit included.
                    self._journal("correct_denied", None, None, "correction not permitted")
                    self._finish_write("aletheia_write", own)
                    raise self._audited_violation("correction not permitted", own)
                orig = Record.from_row(orig_row)
                read = self.scope_engine.authorize_read(
                    orig.scope, ctx, self.scope_engine._payload_text(orig)
                )
                if not read.allowed:
                    # Review fix F7: a correction must not supersede what the caller cannot read.
                    self._journal("correct_denied", None, None, "correction not permitted")
                    self._finish_write("aletheia_write", own)
                    raise self._audited_violation("correction not permitted", own)
                if record.scope != orig.scope:
                    # Review N10: a correction stays in its original's scope.
                    self._journal("correct_denied", None, None, "correction not permitted")
                    self._finish_write("aletheia_write", own)
                    raise self._audited_violation("correction not permitted", own)
                if self._active_rule_relies_on(original_id):
                    raise KnowledgeRefusal("active rule relies on promoter")
                # The replacement id must be free or accessible BEFORE anything is written: a
                # denial here commits the denial journal entry, so an earlier rule deprecation
                # would be committed with it.
                clash = self._lookup_record(record.record_id)
                if clash is not None and (
                        ctx is None or clash.scope not in ctx.granted_scopes
                        or not self.scope_engine.authorize_write(clash, ctx).allowed
                        or not self.scope_engine.authorize_read(
                            clash.scope, ctx, self.scope_engine._payload_text(clash)).allowed):
                    self._journal("write_denied", None, None, "write not permitted")
                    self._finish_write("aletheia_write", own)
                    raise self._audited_violation("write not permitted", own)
                if orig.record_type == 'ProposedRule' and orig.payload.get('status', 'proposed') in ('proposed', 'active'):
                    orig.payload = {**orig.payload, 'status': 'deprecated'}
                    self._conn.execute("UPDATE records SET payload=? WHERE record_id=?",
                                       (json.dumps(orig.payload, sort_keys=True), original_id))
                    self._fts_index(self._lookup_record(original_id))
                    self._journal("deprecate_rule", original_id, orig.scope, "rule corrected")
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
            if not self._summary_sources_available(record, ctx):
                raise KnowledgeRefusal("cited source not available")

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
                    self._finish_write("aletheia_write", own)
                    raise self._audited_violation("write not permitted", own)
                if not overwrite:
                    raise RecordConflict(f"record {record.record_id!r} already exists")
                if self._active_rule_relies_on(record.record_id):
                    raise KnowledgeRefusal("active rule relies on promoter")
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
                    # Overwrite: only valid lifecycle transitions allowed,
                    # and all require user attribution (enforced by the
                    # procedural module's _check_user_only).
                    if not (ctx and ctx.user_authored):
                        raise KnowledgeRefusal("proposed rule transition requires user attribution")
                    old_status = (incumbent.payload or {}).get("status", "proposed")
                    new_status = (record.payload or {}).get("status", "proposed")
                    allowed_transitions = {
                        ("proposed", "active"),
                        ("proposed", "rejected"),
                        ("proposed", "deprecated"),
                        ("active", "deprecated"),
                    }
                    if (old_status, new_status) not in allowed_transitions:
                        raise KnowledgeRefusal("invalid proposed rule transition")
                    if not self._same_rule_proposal(incumbent, record):
                        raise KnowledgeRefusal("rule transition may change status fields only")
                    if new_status == "active" and not self._valid_rule_promoter(record, ctx):
                        raise KnowledgeRefusal("promotion requires valid user decision")
            elif incumbent is not None and incumbent.record_type == "ProposedRule":
                # Cannot change a ProposedRule into another record type.
                raise KnowledgeRefusal("cannot change ProposedRule record type")

            # Review F9: a record's vector describes its text. An overwrite replaces the text,
            # and a correction supersedes the original, so both drop the old vectors.
            if existing is not None:
                self._vec_remove(record.record_id, existing[0])
            if original_id is not None:
                # The superseded record's vectors are kept: its own text is unchanged, and
                # deleting the Correction later makes it current again (it must stay embedded).
                self._conn.execute("DELETE FROM rule_approvals WHERE promoter_id=?", (original_id,))
            self._conn.execute(
                """INSERT OR REPLACE INTO records
                   (record_id, record_type, scope, epistemic_status, payload,
                    created_at, effective_at, supersedes, source_ids, provenance, attribution,
                    valid_from, valid_to)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                record.to_row(),
            )
            self._conn.execute("DELETE FROM rule_approvals WHERE promoter_id=?", (record.record_id,))
            self._conn.execute("DELETE FROM lineage_erased WHERE record_id=?", (record.record_id,))
            if record.record_type == 'ProposedRule' and incumbent is None:
                self._conn.execute("INSERT OR REPLACE INTO rule_incarnations VALUES (?, ?)",
                                   (record.record_id, os.urandom(32).hex()))
            name = self._approval_target(record) if ctx.user_authored else None
            if name is not None:
                proposal = self._lookup_record(name)
                incarnation = self._conn.execute("SELECT token FROM rule_incarnations WHERE rule_id=?", (name,)).fetchone()
                if (proposal is not None and proposal.record_type == 'ProposedRule'
                        and proposal.scope == record.scope and proposal.payload.get('status', 'proposed') == 'proposed'
                        and incarnation and self.scope_engine.authorize_read(proposal.scope, ctx, self.scope_engine._payload_text(proposal)).allowed):
                    self._conn.execute("INSERT INTO rule_approvals VALUES (?, ?, ?, ?)",
                                       (record.record_id, name, incarnation[0], self._proposal_hash(proposal)))
            # Index the serialized record representation, so rebuilds and integrity
            # checks use the same key order as the durable authority.
            self._fts_index(self._lookup_record(record.record_id))
            detail = f"type={record.record_type} status={record.epistemic_status}"
            if existing is not None:
                detail += " overwrite"
            self._journal("put", record.record_id, record.scope, detail)
            self._bump_version()
            self._finish_write("aletheia_write", own)
        except KnowledgeRefusal as exc:
            self._abort_write("aletheia_write", own)
            audited = self._journal_standalone("write_denied", None, None, "write not permitted")
            raise self._audited_violation("write not permitted", audited) from None
        except Exception:
            self._abort_write("aletheia_write", own)
            raise

    def delete(self, record_id: str, ctx: ScopeContext | None = None, *, reason: str = "") -> bool:
        """Hard delete: remove the record, repair its dependents, journal the removal.

        After a successful erase the index and WAL are scrubbed once (see ``_scrub``), however many
        records the cascade removed. Authorization is decided inside ``_delete``'s write transaction.
        """
        self.last_refusal = None
        erased = self._delete(record_id, ctx, reason=reason)
        if not erased and self.last_refusal is None:
            self.last_refusal = _GENERIC_DELETE_REFUSAL
        if erased:
            with self._lock:
                self._scrub()
        return erased

    def _delete(self, record_id: str, ctx: ScopeContext | None = None, *, reason: str = "") -> bool:
        """Hard delete: remove the record and journal the removal.

        Deletion is a write. Authorization is decided INSIDE the write transaction
        (review fix F8). If any record that would lose its supersedes link is in a
        scope the caller cannot write, the whole delete is refused (review fix F15).
        """
        with self._lock:
            if ctx is None:
                self._journal_standalone("delete_denied", record_id, None, "no scope context")
                return False
            self._auto_retry_scrub()
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT record_id, scope FROM records WHERE record_id=?", (record_id,)
                ).fetchone()
                if row is None:
                    erased = self._erase_quarantined(record_id, ctx, reason)
                    if erased:
                        self._set_scrub_flag(True)
                    self._conn.execute("COMMIT")
                    return erased

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

                if (full.record_type == "ProposedRule" and full.payload.get("status") == "active"
                        and not ctx.user_authored):
                    self._veto(record_id, row[1], "an active rule can only be deleted by a user-authored caller")
                    self._conn.execute("COMMIT")
                    return False

                dependents = self._conn.execute(
                    "SELECT record_id, scope FROM records WHERE supersedes = ?", (record_id,)
                ).fetchall()
                for dep_id, dep_scope in dependents:
                    if not self.scope_engine.authorize_delete(dep_scope, ctx).allowed:
                        # Review fix F-B: name neither the dependent nor its scope in the
                        # audit detail, so the caller learns nothing about it.
                        self._veto(record_id, row[1], "a dependent record is outside your granted scopes")
                        self._conn.execute("COMMIT")
                        return False

                # R-2 (review): a verified Claim depends on the Verification that names it.
                # Removing the Verification would leave the Claim verified with no support.
                if self._relied_on(record_id) or self._active_rule_relies_on(record_id):
                    self._veto(record_id, row[1],
                               "a verified claim or an active rule relies on this record")
                    self._conn.execute("COMMIT")
                    return False

                # Compute the full lineage before making any changes. Historical
                # cross-scope references must not give another scope a veto.
                plan = self._plan_cascade(record_id, row[1], ctx)
                if isinstance(plan, str):
                    self._veto(record_id, row[1], plan)
                    self._conn.execute("COMMIT")
                    return False
                summaries, extra_deps = plan
                dependents.extend(extra_deps)
                for sid, sscope in summaries:
                    self._remove_lineage_member(sid, sscope)
                    self._journal("unlink_summary", None, sscope, "removed derived artifact (cascade)")

                for dep_id, dep_scope in dependents:
                    self._unlink_supersedes(dep_id)
                    # Review N7: the detail names no deleted id.
                    self._journal(
                        "unlink_supersedes", dep_id, dep_scope,
                        "cleared dangling supersedes link (target deleted)",
                    )

                self._conn.execute("DELETE FROM records WHERE record_id=?", (record_id,))
                self._conn.execute("DELETE FROM rule_incarnations WHERE rule_id=?", (record_id,))
                self._conn.execute("DELETE FROM rule_approvals WHERE promoter_id=? OR rule_id=?", (record_id, record_id))
                self._fts_remove(record_id)
                self._vec_remove(record_id, row[1])
                self._conn.execute("DELETE FROM entity_links WHERE source_id=? OR target_id=?", (record_id, record_id))
                # R-P3-10: an import's job row must not outlive its Source.
                self._conn.execute("DELETE FROM jobs WHERE scope = ? AND source_id = ?", (row[1], record_id))
                self._conn.execute("DELETE FROM lineage_erased WHERE record_id=?", (record_id,))
                self._erase_quarantined(record_id, ctx, reason)  # a legacy copy of the same id
                # The audit row stays (type/time/scope); the id and free-text reason do not.
                self._redact_journal(record_id)
                self._journal("delete", _ERASED, row[1], f"reason={_ERASED}")
                self._bump_version()
                self._set_scrub_flag(True)
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._rollback_if_open()
                raise

    def _unlink_supersedes(self, dep_id: str) -> None:
        """Clear a dependent's supersedes link because its target is being erased.

        A Correction gets the boolean ``lineage_erased`` marker (its own id only; no id of the erased
        original is kept anywhere) so an export of its scope still restores. Must run inside a write
        transaction.
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO lineage_erased(record_id) "
            "SELECT record_id FROM records "
            "WHERE record_id=? AND record_type='Correction' AND supersedes IS NOT NULL", (dep_id,))
        self._conn.execute("UPDATE records SET supersedes = NULL WHERE record_id = ?", (dep_id,))

    def _veto(self, record_id: str, scope: str, why: str) -> None:
        """Record a delete refusal the caller is entitled to see (it already holds delete authority
        over ``scope``): the journal row carries the scope, and ``last_refusal`` says why."""
        self.last_refusal = f"delete refused: {why}"
        self._journal("delete_denied", record_id, scope, self.last_refusal)

    def _plan_cascade(self, record_id: str, scope: str, ctx):
        """Lineage cascade of erasing ``record_id``: (members, extra_dependents), or a refusal reason.

        Members are the transitive same-scope derived records that cite it; extra_dependents are
        records whose supersedes link points at a member. Nothing is changed here.
        """
        members, blocked = self._lineage(record_id, scope)
        if blocked:
            return "a retained record in this scope cites this record"
        for sid, sscope in members:
            dependent = self._lookup_record(sid)
            if (not self.scope_engine.authorize_delete(sscope, ctx).allowed or
                    not self.scope_engine.authorize_read(
                        sscope, ctx, self.scope_engine._payload_text(dependent)).allowed):
                return "a derived record is outside your granted scopes"
        extra_deps: list[tuple[str, str]] = []
        for sid, _ in members:
            extra = self._conn.execute(
                "SELECT record_id, scope FROM records WHERE supersedes=?", (sid,)).fetchall()
            if any(not self.scope_engine.authorize_delete(s, ctx).allowed for _, s in extra):
                return "a dependent record is outside your granted scopes"
            extra_deps.extend(extra)
        return members, extra_deps

    def _erase_quarantined(self, record_id: str, ctx, reason: str = "") -> bool:
        """Erase one quarantined row if ``ctx`` may delete in its scope. In a write transaction.

        Applies delete()'s lineage cascade: derived records citing the row go with it and
        dependents' supersedes links are repaired, so no citation is left dangling.
        """
        row = self._conn.execute(
            "SELECT scope FROM quarantined_records WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            return False
        if not isinstance(row[0], str) or not self.scope_engine.authorize_delete(row[0], ctx).allowed:
            self._journal("delete_denied", None, None, "delete not permitted")
            return False
        dependents = self._conn.execute(
            "SELECT record_id, scope FROM records WHERE supersedes=?", (record_id,)).fetchall()
        if any(not self.scope_engine.authorize_delete(s, ctx).allowed for _, s in dependents):
            self._veto(record_id, row[0], "a dependent record is outside your granted scopes")
            return False
        plan = self._plan_cascade(record_id, row[0], ctx)
        if isinstance(plan, str):
            self._veto(record_id, row[0], plan)
            return False
        members, extra_deps = plan
        dependents.extend(extra_deps)
        for sid, sscope in members:
            self._remove_lineage_member(sid, sscope)
            self._journal("unlink_summary", None, sscope, "removed derived artifact (cascade)")
        for dep_id, dep_scope in dependents:
            self._unlink_supersedes(dep_id)
            self._journal("unlink_supersedes", dep_id, dep_scope,
                          "cleared dangling supersedes link (target deleted)")
        self._conn.execute("DELETE FROM quarantined_records WHERE record_id=?", (record_id,))
        self._redact_journal(record_id)
        self._journal("delete_quarantined", _ERASED, row[0], f"reason={_ERASED}")
        self._bump_version()
        return True

    def list_quarantined(self, ctx: ScopeContext | None = None) -> list[dict[str, Any]]:
        """Quarantined legacy rows in scopes the caller may read: id, scope, type, reason (no payload)."""
        if ctx is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_id, scope, record_type, reason FROM quarantined_records ORDER BY record_id"
            ).fetchall()
        return [{"record_id": r[0], "scope": r[1], "record_type": r[2], "reason": r[3]}
                for r in rows
                if isinstance(r[1], str) and self.scope_engine.authorize_read(r[1], ctx, "").allowed]

    def purge_quarantined(self, ctx: ScopeContext | None = None, *, scope: str | None = None,
                          record_id: str | None = None) -> int:
        """Erase quarantined rows (optionally one scope / one id) the caller may delete. Returns the count."""
        if ctx is None:
            return 0
        with self._lock:
            self._auto_retry_scrub()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                sql, params = "SELECT record_id FROM quarantined_records", []
                clauses = []
                if scope is not None:
                    clauses.append("scope = ?")
                    params.append(scope)
                if record_id is not None:
                    clauses.append("record_id = ?")
                    params.append(record_id)
                if clauses:
                    sql += " WHERE " + " AND ".join(clauses)
                count = sum(1 for (rid,) in self._conn.execute(sql, params).fetchall()
                            if self._erase_quarantined(rid, ctx, "purge_quarantined"))
                if count:
                    self._set_scrub_flag(True)
                self._conn.execute("COMMIT")
            except Exception:
                self._rollback_if_open()
                raise
            if count:
                self._scrub()
            return count

    def _lineage(self, record_id: str, scope: str) -> tuple[list[tuple[str, str]], bool]:
        """Transitive same-scope derived lineage that removing ``record_id`` must cascade to.

        Returns (members, blocked). blocked is true when a record outside that lineage (notably a
        verified Claim) still cites a member, so removal would leave a dangling citation.
        Historical foreign-scope citations are deliberately not a cross-scope veto.
        """
        candidates = self._conn.execute(
            "SELECT record_id, scope, source_ids FROM records "
            "WHERE epistemic_status='derived' "
            "OR record_type IN ('Observation', 'Summary', 'Hypothesis', 'Reasoning')"
        ).fetchall()
        removed = {record_id}
        members: list[tuple[str, str]] = []
        while True:
            new = [(sid, sscope) for sid, sscope, src in candidates
                   if sid not in removed and sscope == scope
                   and any(self._cites(src, parent) for parent in removed)]
            if not new:
                break
            members.extend(new)
            removed.update(sid for sid, _ in new)
        retained = self._conn.execute(
            "SELECT record_id, source_ids FROM records WHERE scope=?", (scope,)).fetchall()
        blocked = any(rid not in removed and any(self._cites(src, parent) for parent in removed)
                      for rid, src in retained)
        return members, blocked

    def _remove_lineage_member(self, record_id: str, scope: str) -> None:
        """Remove one cascaded record with its indexes, vectors and entity links."""
        self._conn.execute("DELETE FROM records WHERE record_id = ?", (record_id,))
        self._fts_remove(record_id)
        self._vec_remove(record_id, scope)
        self._conn.execute("DELETE FROM entity_links WHERE source_id=? OR target_id=?", (record_id, record_id))
        self._conn.execute("DELETE FROM jobs WHERE scope = ? AND source_id = ?", (scope, record_id))
        self._conn.execute("DELETE FROM lineage_erased WHERE record_id=?", (record_id,))
        self._redact_journal(record_id)

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

    def distinct_scopes(self) -> list[str]:
        """Every scope present in the store, regardless of the caller's grants.

        Used to build ``ScopeEngine.known_scopes`` for a shared store: a scope the engine has never
        heard of is denied before grants are consulted, so a host-level store must register all of
        them. Knowing a scope exists confers nothing — reads and writes still need a grant.

        Unions every table that carries a scope: a scope whose last record was deleted still owns
        journal rows, jobs or quarantined records, and denying those operations would make the store
        look corrupt.
        """
        with self._lock:
            scopes: set[str] = set()
            for table in ("records", "journal", "entity_links", "jobs", "quarantined_records"):
                has_scope = self._conn.execute(
                    "SELECT 1 FROM pragma_table_info(?) WHERE name='scope'", (table,)).fetchone()
                if not has_scope:
                    continue
                scopes.update(row[0] for row in self._conn.execute(
                    f"SELECT DISTINCT scope FROM {table} WHERE scope IS NOT NULL"))
        return sorted(scopes)

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
                "records": [self._export_with_tombstone(r) for r in readable],
            }

    def import_scope(self, data: dict[str, Any], ctx: ScopeContext | None = None, *, strict: bool = False) -> int:
        """Import records from an export dict into the substrate.

        The caller must have a granted write scope for the exported scope.
        Returns the number of records imported. Raises ``ValueError`` if the
        schema version does not match or the scope is missing.

        With strict=True, policy refusals raise KnowledgeRefusal instead of
        returning zero, distinguishing a rejected restore from an empty one.
        """
        if ctx is None:
            raise ValueError("context required for import")
        scope = data.get("scope")
        if not scope:
            raise ValueError("export payload missing scope")
        if data.get("schema_version") not in _IMPORTABLE_SCHEMA_VERSIONS:
            raise ValueError(f"schema version mismatch: {data.get('schema_version')} != {SCHEMA_VERSION}")
        raw = data.get("records", [])
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise SchemaViolation("export records must be a list of objects")
        records = []
        marked: dict[str, Any] = {}  # record_id -> the raw lineage_erased value, when present
        for rec_dict in raw:
            rec = self._record_from_export(rec_dict)
            self._validate_integrity(rec)
            records.append(rec)
            if "lineage_erased" in rec_dict:
                marked[rec.record_id] = rec_dict["lineage_erased"]
        self.last_import_refusal = None

        def refused(reason: str) -> int:
            # A refused restore is atomic: nothing was written. The reason is kept (and raised
            # with strict=True) so it can never be mistaken for an empty bundle.
            self.last_import_refusal = reason
            if strict:
                raise KnowledgeRefusal(reason)
            return 0

        bundle_ids = {r.record_id for r in records}
        for rec in records:
            if rec.record_id in marked and (
                    marked[rec.record_id] is not True
                    or rec.record_type != "Correction" or rec.supersedes is not None):
                # Same generic refusal as an unmarked, target-less Correction.
                return refused(f"record {rec.record_id!r}: Correction without a supersedes target")

        # A bundle holding ANY active rule is refused whole, before any idempotent skip or
        # insertion: an import never restores approvals, so it never restores an active rule, and
        # "this rule already exists identically" must not turn the refusal into a partial import.
        for rec in records:
            if rec.record_type == "ProposedRule" and isinstance(rec.payload, dict) \
                    and rec.payload.get("status", "proposed") == "active":
                return refused(
                    f"record {rec.record_id!r}: an active rule is never restored by import; "
                    "export it as proposed (or re-propose it) and approve it again here")

        # Order bundle-local dependencies (supersedes and source_ids) before dependents,
        # independent of export order (an overwritten Source exports after its Observation).
        # Refuse ambiguous duplicates and cycles before opening a transaction.
        if len({r.record_id for r in records}) != len(records):
            return refused("duplicate record_id in bundle")
        ordered = []
        pending = list(records)
        while pending:
            pending_ids = {r.record_id for r in pending}
            ready = [r for r in pending if not (set(r.source_ids) | ({r.supersedes} if r.supersedes else set())) & pending_ids]
            if not ready:
                return refused("dependency cycle in bundle (source_ids/supersedes)")
            ordered.extend(ready)
            ready_ids = {id(r) for r in ready}
            pending = [r for r in pending if id(r) not in ready_ids]
        class _ImportRefused(Exception):
            def __init__(self, reason: str = "import refused"):
                super().__init__(reason)
                self.reason = reason

        imported = 0
        with self._lock:
            import_own = self._begin_write("aletheia_import")
            try:
                for rec in ordered:
                    if rec.scope != scope:
                        raise _ImportRefused(f"record {rec.record_id!r} is outside the bundle scope")
                    decision = self.scope_engine.authorize_write(rec, ctx)
                    if not decision.allowed:
                        raise _ImportRefused("write not permitted")
                    # Import must also have authority over any incumbent record
                    # with the same ID; otherwise a cross-scope write can replace
                    # an existing record by ID collision.
                    incumbent = self._lookup_record(rec.record_id)
                    if incumbent is not None:
                        if (ctx is None or incumbent.scope != scope or incumbent.scope not in ctx.granted_scopes
                                or not self.scope_engine.authorize_write(incumbent, ctx).allowed
                                or not self.scope_engine.authorize_read(
                                    incumbent.scope, ctx, self.scope_engine._payload_text(incumbent)).allowed):
                            raise _ImportRefused("write not permitted")
                    if incumbent is not None and (
                            json.dumps(self._record_to_export(incumbent), sort_keys=True)
                            == json.dumps(self._record_to_export(rec), sort_keys=True)):
                        # Idempotent restore: an identical record already present is skipped, so a
                        # partially damaged store can be repaired from its own export.
                        continue
                    if incumbent is not None and (incumbent.epistemic_status == "verified"
                            or (incumbent.record_type == "Verification" and self._relied_on(rec.record_id))
                            or self._active_rule_relies_on(rec.record_id)):
                        raise _ImportRefused(
                            f"record {rec.record_id!r} would overwrite a protected record "
                            "(verified, relied-on Verification, or promoter of an active rule)")
                    if rec.supersedes:
                        if rec.record_type != "Correction" or incumbent is not None:
                            raise _ImportRefused(
                                f"record {rec.record_id!r}: a supersedes link restores only as a new "
                                "Correction (not over an existing record, not for an active rule's promoter)")
                        # Scope before relevance: a missing, foreign or unreadable target gets one
                        # refusal, decided before the target's role (e.g. promoter) is consulted.
                        target = self._lookup_record(rec.supersedes)
                        if (target is None or target.scope != rec.scope
                                or not self.scope_engine.authorize_read(
                                    target.scope, ctx, self.scope_engine._payload_text(target)).allowed):
                            raise _ImportRefused(f"record {rec.record_id!r}: superseded record not available")
                        if self._active_rule_relies_on(rec.supersedes):
                            raise _ImportRefused(
                                f"record {rec.record_id!r}: a supersedes link restores only as a new "
                                "Correction (not over an existing record, not for an active rule's promoter)")
                    elif rec.record_type == "Correction":
                        # Only a Correction marked lineage_erased may arrive without a target, and
                        # only from a user-authored import. No other id is looked up, so this path
                        # is neither an existence oracle nor subject to id reuse.
                        if marked.get(rec.record_id) is not True or not ctx.user_authored:
                            raise _ImportRefused(f"record {rec.record_id!r}: Correction without a supersedes target")
                    self._validate(rec)
                    reason = check_record(
                        rec, ctx, is_verifier=self._is_verifier, lookup=self._lookup_record
                    )
                    if reason is not None:
                        raise _ImportRefused(f"record {rec.record_id!r}: {reason}")
                    if not self._summary_sources_available(rec, ctx):
                        raise _ImportRefused(f"record {rec.record_id!r}: cited source not available in this scope")
                    # Rules: an import never transitions an existing rule (identical content was
                    # skipped above). A new id may arrive only as 'proposed', or as an inert
                    # 'rejected'/'deprecated' rule exactly as exported; 'active' never restores.
                    if rec.record_type == "ProposedRule":
                        if incumbent is not None:
                            raise _ImportRefused(
                                f"record {rec.record_id!r}: an existing rule differs from the bundle's "
                                f"(content or status); imports never change an existing rule")
                        new_status = (rec.payload or {}).get("status", "proposed")
                        if new_status == "active":
                            raise _ImportRefused(
                                f"record {rec.record_id!r}: an active rule is never restored by import; "
                                "export it as proposed (or re-propose it) and approve it again here")
                        if new_status not in ("proposed", "rejected", "deprecated"):
                            raise _ImportRefused(f"record {rec.record_id!r}: unknown rule status {new_status!r}")
                    elif incumbent is not None and incumbent.record_type == "ProposedRule":
                        raise _ImportRefused(f"record {rec.record_id!r}: cannot replace a ProposedRule with another type")
                    if rec.supersedes:
                        target = self._lookup_record(rec.supersedes)
                        if (target is not None and target.record_type == "ProposedRule"
                                and (target.payload or {}).get("status", "proposed") in ("proposed", "active")):
                            if rec.supersedes in bundle_ids:
                                raise _ImportRefused(
                                    f"record {rec.record_id!r}: this bundle restores rule {rec.supersedes!r} "
                                    f"as live ({(target.payload or {}).get('status', 'proposed')}) and also a Correction of it; "
                                    "a corrected rule must be exported as 'deprecated'")
                            raise _ImportRefused(
                                f"record {rec.record_id!r}: importing a Correction would deprecate the existing "
                                f"rule {rec.supersedes!r}; imports never change an existing rule")
                    own = False
                    try:
                        own = self._begin_write("aletheia_write")
                        if incumbent is not None:
                            self._vec_remove(rec.record_id, incumbent.scope)
                        if rec.supersedes:
                            self._conn.execute("DELETE FROM rule_approvals WHERE promoter_id=?", (rec.supersedes,))
                        self._conn.execute(
                            "INSERT OR REPLACE INTO records "
                            "(record_id, record_type, scope, epistemic_status, payload, "
                            "created_at, effective_at, supersedes, source_ids, provenance, attribution, "
                            "valid_from, valid_to) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            rec.to_row(),
                        )
                        self._conn.execute("DELETE FROM rule_approvals WHERE promoter_id=?", (rec.record_id,))
                        self._conn.execute("DELETE FROM lineage_erased WHERE record_id=?", (rec.record_id,))
                        if marked.get(rec.record_id) is True and rec.supersedes is None:
                            self._conn.execute("INSERT OR IGNORE INTO lineage_erased(record_id) VALUES (?)",
                                               (rec.record_id,))
                        if rec.record_type == 'ProposedRule' and incumbent is None:
                            self._conn.execute("INSERT OR REPLACE INTO rule_incarnations VALUES (?, ?)",
                                               (rec.record_id, os.urandom(32).hex()))
                        self._fts_index(self._lookup_record(rec.record_id))
                        self._journal("import", rec.record_id, rec.scope, f"type={rec.record_type}")
                        self._bump_version()
                        self._finish_write("aletheia_write", own)
                        imported += 1
                    except Exception:
                        self._abort_write("aletheia_write", own)
                        raise
                self._finish_write("aletheia_import", import_own)
            except _ImportRefused as exc:
                self._abort_write("aletheia_import", import_own)
                return refused(exc.reason)
            except BaseException:
                self._abort_write("aletheia_import", import_own)
                raise
        return imported

    def _export_with_tombstone(self, rec: Record) -> dict[str, Any]:
        out = self._record_to_export(rec)
        if rec.record_type == "Correction" and rec.supersedes is None:
            row = self._conn.execute(
                "SELECT 1 FROM lineage_erased WHERE record_id=?", (rec.record_id,)).fetchone()
            if row is not None:
                out["lineage_erased"] = True  # a boolean only; no id or content of the erased record
        return out

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
            "valid_from": rec.valid_from,
            "valid_to": rec.valid_to,
        }

    @staticmethod
    def _record_from_export(d: Any) -> Record:
        if not isinstance(d, dict) or not all(k in d for k in ("record_id", "record_type", "scope", "epistemic_status")):
            raise SchemaViolation("exported record is missing required fields")
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
            valid_from=d.get("valid_from"),
            valid_to=d.get("valid_to"),
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
        """Delete derived records in ``scope`` created before ``older_than`` whose sources still exist.

        Selection is by epistemic status ('derived'), never by record type: a verified record is
        never pruned, nor is a record a verified or active-rule record relies on. Each prune applies
        delete()'s transitive lineage cascade (dependent derived records, Observations, their
        entity links, indexes and vectors go with it), so no citation is left dangling. A candidate
        whose removal a retained record would observe is skipped, not forced.

        Returns the number of records removed (pruned plus cascaded). No context -> 0.
        """
        if ctx is None:
            return 0
        with self.ingestion_transaction():
            rows = self._conn.execute(
                "SELECT record_id, scope, source_ids FROM records "
                "WHERE scope = ? AND epistemic_status = 'derived' AND created_at < ? "
                "ORDER BY created_at, record_id",
                (scope, older_than),
            ).fetchall()
            removed_total = 0
            for rid, rscope, src_json in rows:
                if self._lookup_record(rid) is None:
                    continue  # already cascaded by an earlier prune
                if not self.scope_engine.authorize_delete(rscope, ctx).allowed:
                    continue
                src_ids = json.loads(src_json)
                if not src_ids or not all(self._lookup_record(sid) is not None for sid in src_ids):
                    continue
                if self._relied_on(rid) or self._active_rule_relies_on(rid):
                    continue
                members, blocked = self._lineage(rid, rscope)
                if blocked:
                    continue
                skip = False
                for sid, sscope in members:
                    dependent = self._lookup_record(sid)
                    if (not self.scope_engine.authorize_delete(sscope, ctx).allowed
                            or not self.scope_engine.authorize_read(
                                sscope, ctx, self.scope_engine._payload_text(dependent)).allowed
                            or self._relied_on(sid) or self._active_rule_relies_on(sid)):
                        skip = True
                        break
                if skip:
                    continue
                doomed = [(rid, rscope)] + members
                for did, _ in doomed:
                    dependents = self._conn.execute(
                        "SELECT record_id, scope FROM records WHERE supersedes=?", (did,)).fetchall()
                    if any(not self.scope_engine.authorize_delete(s, ctx).allowed for _, s in dependents):
                        raise ScopeViolation("prune not permitted")
                    for dep_id, dep_scope in dependents:
                        self._unlink_supersedes(dep_id)
                        self._journal("unlink_supersedes", dep_id, dep_scope,
                                      "cleared dangling supersedes link (target deleted)")
                for sid, sscope in members:
                    self._remove_lineage_member(sid, sscope)
                    self._journal("unlink_summary", None, sscope, "removed derived artifact (cascade)")
                self._remove_lineage_member(rid, rscope)
                self._journal("prune_derived", _ERASED, rscope, "retention TTL exceeded")
                removed_total += len(doomed)
            for qid, qscope in self._conn.execute(
                    "SELECT record_id, scope FROM quarantined_records WHERE scope = ? "
                    "AND epistemic_status = 'derived' AND typeof(created_at) IN ('real','integer') "
                    "AND created_at < ?", (scope, older_than)).fetchall():
                if self._erase_quarantined(qid, ctx, "retention TTL exceeded"):
                    removed_total += 1
            if removed_total:
                self._bump_version()
                self._set_scrub_flag(True)
        if removed_total:
            with self._lock:
                self._scrub()  # once, after the outermost transaction committed
        return removed_total

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

    @staticmethod
    def _validate_integrity(record: Record) -> None:
        _check_id(record.record_id)
        if not isinstance(record.payload, dict) or not isinstance(record.provenance, dict):
            raise SchemaViolation("payload and provenance must be objects")
        if 'text' in record.payload and not isinstance(record.payload['text'], str):
            raise SchemaViolation("payload text must be a string")
        for name in ('created_at', 'effective_at', 'valid_from', 'valid_to'):
            value = getattr(record, name)
            if value is None and name != 'created_at':
                continue
            try:
                valid = (type(value) in (int, float) and math.isfinite(value)
                         and (type(value) is float or abs(value) < 2 ** 53))
            except OverflowError:
                valid = False
            if not valid:
                raise SchemaViolation("invalid timestamp")
        if (record.valid_from is not None and record.valid_to is not None
                and record.valid_from > record.valid_to):
            raise SchemaViolation("valid_from must not be after valid_to")
        if not isinstance(record.source_ids, list) or not all(isinstance(x, str) for x in record.source_ids):
            raise SchemaViolation("source_ids must be a list of strings")
        try:
            stack = [record.payload, record.provenance, record.source_ids]
            seen = set()
            while stack:
                value = stack.pop()
                if isinstance(value, (dict, list)):
                    if id(value) in seen:
                        continue  # json.dumps below detects cycles
                    seen.add(id(value))
                    if isinstance(value, dict):
                        if not all(isinstance(k, str) for k in value):
                            raise ValueError("JSON keys must be strings")
                        stack.extend(value.keys())
                        stack.extend(value.values())
                    else:
                        stack.extend(value)
                elif isinstance(value, str):
                    value.encode("utf-8")
                elif value is None or isinstance(value, (bool, int)):
                    pass
                elif isinstance(value, float) and math.isfinite(value):
                    pass
                else:
                    raise ValueError("invalid JSON value")
            json.dumps([record.payload, record.provenance, record.source_ids], allow_nan=False)
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise SchemaViolation("record contains invalid JSON") from exc

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
            self._drop_fts_corpus()
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
