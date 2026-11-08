"""Substrate tests, including crash and concurrency checks.

Crash-consistency claims (docs/adr/0001-phase1-decisions.md), tested for real:
  - A kill during a write leaves no partial record and no orphan journal entry.
  - A commit with no projection leaves a projection the status check reports stale.
  - Concurrent writers from separate processes never lose or duplicate a record.
  - Hand-edited projection files are detected as tampered and never read back.
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, RecordConflict, ScopeViolation, SchemaViolation, Substrate

REPO = Path(__file__).resolve().parents[1]
KNOWN = {"ariel-continuity", "acs-project", "memory-providers"}
MARKERS = {"acs-project": {"ACS", "Listening Commonwealth"}}

CTX = ScopeContext(granted_scopes=frozenset(KNOWN))
USER_CTX = ScopeContext(granted_scopes=frozenset(KNOWN), user_authored=True, actor="johann")
MEM = ScopeContext(granted_scopes=frozenset({"memory-providers"}))


def _seed_source(s, scopes):
    """Phase 3 (R-6): observations must cite an existing same-scope Source.

    Seeds one Source per scope with a raw insert: no journal row and no index row,
    so the seeds do not show up in journal or search assertions.
    """
    for sc in scopes:
        s._conn.execute(
            "INSERT OR IGNORE INTO records (record_id, record_type, scope, epistemic_status, payload, "
            "created_at, effective_at, supersedes, source_ids, provenance, attribution) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"src-1-{sc}", "Source", sc, "stated", '{"text": "seed source"}', 0.0, None, None, "[]", "{}", "document"),
        )
    s._conn.commit()


SEEDED_SOURCE_IDS = {"src-1-acs-project", "src-1-memory-providers", "src-1-ariel-continuity"}

@pytest.fixture
def engine():
    return ScopeEngine(known_scopes=KNOWN, markers=MARKERS)


@pytest.fixture
def substrate(tmp_path, engine):
    s = Substrate(tmp_path / "aletheia.db", scope_engine=engine)
    _seed_source(s, sorted(KNOWN))
    _seed_source(s, sorted(KNOWN))
    yield s
    s.close()


def _raw_journal(s):
    """Audit-table rows (op, detail). Test-only access; the public journal view is scope-filtered."""
    return [(r[0], r[1]) for r in s._conn.execute("SELECT op, detail FROM journal ORDER BY seq")]


def _raw_ops(s):
    return [r[0] for r in _raw_journal(s)]


def make_correction(rid, scope="memory-providers", text="corrected"):
    return Record(record_id=rid, record_type="Correction", scope=scope,
                  payload={"text": text}, attribution="user")


def make_record(rid="r1", scope="memory-providers", text="Honcho uses hybrid recall."):
    return Record(record_id=rid, record_type="Observation", attribution="document", source_ids=[f"src-1-{scope}"], scope=scope, payload={"text": text})


class TestBasicOperations:
    def test_put_and_get(self, substrate):
        substrate.put(make_record(), CTX)
        got = substrate.get("r1", CTX)
        assert got is not None
        assert got.payload["text"] == "Honcho uses hybrid recall."

    def test_put_rejected_when_scope_denied(self, substrate):
        with pytest.raises(ScopeViolation):
            substrate.put(make_record(scope="unknown-scope"), CTX)
        assert substrate.get("r1", CTX) is None
        assert "write_denied" in _raw_ops(substrate)

    def test_scope_check_runs_before_validation(self, substrate):
        rec = make_record(scope="unknown-scope")
        rec.epistemic_status = "bogus-status"
        with pytest.raises(ScopeViolation):
            substrate.put(rec, CTX)

    def test_scope_and_schema_violations_are_distinct(self, substrate):
        assert not issubclass(ScopeViolation, SchemaViolation)
        assert not issubclass(SchemaViolation, ScopeViolation)

    def test_unknown_scope_is_denied_not_created(self, substrate):
        with pytest.raises(ScopeViolation):
            substrate.put(make_record(scope="brand-new-scope"), CTX)
        assert substrate.get("r1", CTX) is None
        denials = [r for r in _raw_journal(substrate) if r[0] == "write_denied"]
        assert denials and denials[0][1] == "write not permitted"  # G10: generic, no scope named

    def test_invalid_epistemic_status_rejected(self, substrate):
        rec = make_record()
        rec.epistemic_status = "bogus"
        with pytest.raises(SchemaViolation):
            substrate.put(rec, CTX)
        assert substrate.get("r1", CTX) is None

    def test_correct_preserves_lineage(self, substrate):
        substrate.put(make_record(text="The decision was X."), CTX)
        substrate.correct("r1", make_correction("r2", text="The decision was changed to Y."), USER_CTX)
        assert substrate.get("r1", CTX) is not None, "original must not be erased"
        assert substrate.get("r2", CTX).supersedes == "r1"

    def test_delete_removes_record(self, substrate):
        substrate.put(make_record(), CTX)
        assert substrate.delete("r1", CTX, reason="privacy request") is True
        assert substrate.get("r1", CTX) is None
        assert "delete" in [j["op"] for j in substrate.journal_entries(CTX)]

    def test_delete_missing_returns_false(self, substrate):
        assert substrate.delete("nope", CTX) is False

    def test_delete_clears_dangling_supersedes(self, substrate):
        substrate.put(make_record("r1", text="original"), CTX)
        substrate.correct("r1", make_correction("r2", text="corrected"), USER_CTX)
        assert substrate.get("r2", CTX).supersedes == "r1"

        assert substrate.delete("r1", CTX, reason="privacy request") is True
        assert substrate.get("r1", CTX) is None
        assert substrate.get("r2", CTX).supersedes is None, "dangling supersedes link survived deletion"
        ops = [(j["op"], j["record_id"]) for j in substrate.journal_entries(CTX)]
        assert ("unlink_supersedes", "r2") in ops

    def test_delete_cannot_be_used_to_reconstruct_lineage(self, substrate):
        substrate.put(make_record("r1", text="sensitive original"), CTX)
        substrate.correct("r1", make_correction("r2", text="correction"), USER_CTX)
        substrate.correct("r2", make_correction("r3", text="second correction"), USER_CTX)
        substrate.delete("r1", CTX, reason="privacy")
        assert not any("r1" in (r.supersedes or "") for r in substrate.all_records(CTX))

    def test_journal_has_single_code_path(self, substrate):
        assert not hasattr(substrate, "_journal_locked")
        assert callable(substrate._journal)

    def test_journal_carries_no_payload(self, substrate):
        """The journal is an audit trail. It must not hold record content, so deletion cannot
        leave content behind in it. (Replaces the earlier rebuild-from-journal claim; see ADR.)"""
        substrate.put(make_record(text="Honcho uses hybrid recall."), CTX)
        blob = json.dumps(substrate.journal_entries(CTX))
        assert "hybrid recall" not in blob


class TestDurabilityPragmas:
    def test_synchronous_is_full(self, tmp_path, engine):
        s = Substrate(tmp_path / "d.db", scope_engine=engine)
        _seed_source(s, sorted(KNOWN))
        try:
            assert s._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        finally:
            s.close()

    def test_wal_mode_enabled(self, tmp_path, engine):
        s = Substrate(tmp_path / "d.db", scope_engine=engine)
        _seed_source(s, sorted(KNOWN))
        try:
            assert s._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        finally:
            s.close()

    def test_foreign_keys_enabled(self, tmp_path, engine):
        s = Substrate(tmp_path / "d.db", scope_engine=engine)
        _seed_source(s, sorted(KNOWN))
        try:
            assert s._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        finally:
            s.close()


class TestProjectionIsDerived:
    def test_projection_written_and_current(self, substrate, tmp_path):
        substrate.put(make_record(), CTX)
        out = substrate.write_projection(tmp_path / "proj", MEM)
        assert (out / "r1.json").exists()
        assert json.loads((out / "r1.json").read_text())["text"] == "Honcho uses hybrid recall."
        assert substrate.projection_status(out, MEM)["state"] == "current"

    def test_hand_edited_projection_is_tampered_and_store_unaffected(self, substrate, tmp_path):
        """Projection files are never authoritative. A hand edit is detected, not read back."""
        substrate.put(make_record(), CTX)
        out = substrate.write_projection(tmp_path / "proj", MEM)
        (out / "r1.json").write_text(json.dumps({"text": "TAMPERED"}))
        assert substrate.projection_status(out, MEM)["state"] == "tampered"
        assert substrate.get("r1", CTX).payload["text"] == "Honcho uses hybrid recall."

    def test_projection_is_idempotent(self, substrate, tmp_path):
        substrate.put(make_record(), CTX)
        out = substrate.write_projection(tmp_path / "proj", MEM)
        first = (out / "r1.json").read_text()
        substrate.write_projection(out, MEM)
        assert (out / "r1.json").read_text() == first

    def test_projection_without_context_writes_nothing(self, substrate, tmp_path):
        substrate.put(make_record(), CTX)
        out = substrate.write_projection(tmp_path / "proj", None)
        assert list(out.glob("r*.json")) == []

    def test_projection_excludes_out_of_scope_records(self, substrate, tmp_path):
        substrate.put(make_record("r1", scope="memory-providers"), CTX)
        substrate.put(make_record("r2", scope="acs-project", text="ACS note"), CTX)
        out = substrate.write_projection(tmp_path / "proj", MEM)
        assert {p.stem for p in out.glob("r*.json")} == {"r1"}


class TestFaultInjection:
    def test_kill_mid_write_leaves_no_partial_record(self, tmp_path):
        """Kill the real write path between the record insert and its journal entry."""
        db = tmp_path / "aletheia.db"
        setup = Substrate(db, scope_engine=ScopeEngine(known_scopes=KNOWN))
        _seed_source(setup, sorted(KNOWN))
        setup.put(make_record("committed"), CTX)
        setup.close()

        script = textwrap.dedent(
            f"""
            import os, signal, sys
            sys.path.insert(0, {str(REPO)!r})
            from aletheia.scope import ScopeContext, ScopeEngine
            from aletheia.substrate import Record, Substrate
            s = Substrate({str(db)!r}, scope_engine=ScopeEngine(known_scopes={sorted(KNOWN)!r}))
            def boom(*a, **k):
                os.kill(os.getpid(), signal.SIGKILL)
            s._journal = boom
            ctx = ScopeContext(granted_scopes=frozenset({sorted(KNOWN)[0], "memory-providers"}))
            s.put(Record(record_id="doomed", record_type="Observation",
                         scope="memory-providers", payload={{"text": "never committed"}}), ctx)
            """
        )
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)
        assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()

        s2 = Substrate(db, scope_engine=ScopeEngine(known_scopes=KNOWN))
        _seed_source(s2, sorted(KNOWN))
        try:
            assert s2.get("committed", CTX) is not None
            assert s2.get("doomed", CTX) is None, "uncommitted record leaked"
            assert "doomed" not in [j["record_id"] for j in s2.journal_entries(CTX)]
        finally:
            s2.close()

    def test_commit_without_projection_is_reported_stale(self, substrate, tmp_path):
        substrate.put(make_record("r1"), CTX)
        out = substrate.write_projection(tmp_path / "proj", MEM)
        substrate.put(make_record("r2"), CTX)  # committed, never projected
        assert substrate.projection_status(out, MEM)["state"] == "stale"
        assert substrate.get("r2", CTX) is not None, "committed write must not be lost"
        substrate.write_projection(out, MEM)
        assert substrate.projection_status(out, MEM)["state"] == "current"

    def test_journal_accounts_for_every_committed_write(self, substrate):
        for i in range(20):
            substrate.put(make_record(f"r{i}", text=f"fact {i}"), CTX)
        put_ids = {j["record_id"] for j in substrate.journal_entries(CTX) if j["op"] == "put"}
        # the seeded Source is written by the fixture, not by the test; exclude it explicitly
        put_ids.discard("src-1-memory-providers")
        assert put_ids == {r.record_id for r in substrate.all_records(CTX) if r.record_id not in SEEDED_SOURCE_IDS}

    def test_concurrent_writers_no_lost_or_duplicate_record(self, tmp_path):
        """Two processes race for the same ids. Each id is created exactly once."""
        db = tmp_path / "aletheia.db"
        setup = Substrate(db, scope_engine=ScopeEngine(known_scopes=KNOWN))
        _seed_source(setup, sorted(KNOWN))
        setup.close()

        writer = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(REPO)!r})
            from aletheia.scope import ScopeContext, ScopeEngine
            from aletheia.substrate import Record, RecordConflict, Substrate
            s = Substrate({str(db)!r}, scope_engine=ScopeEngine(known_scopes={sorted(KNOWN)!r}), busy_timeout=10)
            ctx = ScopeContext(granted_scopes=frozenset({sorted(KNOWN)[0], "memory-providers"}))
            won = 0
            for i in range(30):
                try:
                    s.put(Record(record_id=f"shared{{i}}", record_type="Observation", attribution="document", source_ids=[f"src-1-memory-providers"],
                                 scope="memory-providers", payload={{"text": "x"}}), ctx)
                    won += 1
                except RecordConflict:
                    pass
            print(won)
            s.close()
            """
        )
        procs = [subprocess.Popen([sys.executable, "-c", writer], stdout=subprocess.PIPE) for _ in range(2)]
        wins = [int(p.communicate(timeout=120)[0].strip()) for p in procs]
        assert sum(wins) == 30, f"each id must be created exactly once, got wins={wins}"

        s2 = Substrate(db, scope_engine=ScopeEngine(known_scopes=KNOWN))
        _seed_source(s2, sorted(KNOWN))
        try:
            ids = [r.record_id for r in s2.all_records(MEM)]
            ids = [x for x in ids if x != "src-1-memory-providers"]
            assert len(ids) == len(set(ids)) == 30
        finally:
            s2.close()


class TestGatedWritesAndReads:
    def test_overwrite_in_ungranted_scope_refused(self, substrate):
        substrate.put(make_record("r1", scope="acs-project", text="ACS secret"), CTX)
        with pytest.raises(ScopeViolation):
            substrate.put(make_record("r1", scope="memory-providers", text="clobber"), MEM, overwrite=True)
        assert substrate.get("r1", CTX).payload["text"] == "ACS secret"

    def test_same_id_without_overwrite_conflicts(self, substrate):
        substrate.put(make_record("r1", text="first"), CTX)
        with pytest.raises(RecordConflict):
            substrate.put(make_record("r1", text="second"), CTX)
        assert substrate.get("r1", CTX).payload["text"] == "first"

    def test_delete_refused_for_out_of_scope_caller(self, substrate):
        substrate.put(make_record("r1", scope="acs-project"), CTX)
        assert substrate.delete("r1", MEM) is False
        assert substrate.get("r1", CTX) is not None

    def test_delete_refused_without_ctx(self, substrate):
        substrate.put(make_record("r1"), CTX)
        assert substrate.delete("r1") is False
        assert substrate.get("r1", CTX) is not None

    def test_read_denial_is_journalled_with_reason(self, substrate):
        substrate.put(make_record("secret1", scope="acs-project", text="ACS secret"), CTX)
        assert substrate.get("secret1", MEM) is None
        # Round 6 (G10): denials are recorded without scope; the detail is generic.
        denials = [r for r in _raw_journal(substrate) if r[0] == "read_denied"]
        assert denials and denials[-1][1] == "read not permitted"

    def test_query_without_context_returns_nothing(self, substrate):
        substrate.put(make_record("m1"), CTX)
        assert substrate.query(None) == ([], [])

    def test_journal_is_filtered_by_context(self, substrate):
        substrate.put(make_record("secret1", scope="acs-project", text="ACS secret"), CTX)
        substrate.put(make_record("m1"), CTX)
        ids = {e["record_id"] for e in substrate.journal_entries(MEM)}
        assert "secret1" not in ids and "m1" in ids

    def test_journal_without_context_is_empty(self, substrate):
        substrate.put(make_record("m1"), CTX)
        assert substrate.journal_entries(None) == []


def test_new_database_parent_directories_are_owner_only(tmp_path):
    db_path = tmp_path / "private" / "nested" / "aletheia.db"
    sub = Substrate(db_path, ScopeEngine(known_scopes=KNOWN))
    try:
        assert os.stat(db_path.parent).st_mode & 0o777 == 0o700
        assert os.stat(db_path.parent.parent).st_mode & 0o777 == 0o700
    finally:
        sub.close()


def test_constructor_closes_connection_when_database_is_malformed(tmp_path, monkeypatch):
    malformed = tmp_path / "broken.db"
    malformed.write_bytes(b"not sqlite")
    connections = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr("aletheia.substrate.sqlite3.connect", tracking_connect)
    with pytest.raises(sqlite3.DatabaseError):
        Substrate(malformed, ScopeEngine(known_scopes=KNOWN))
    assert len(connections) == 1


def test_crash_mid_write_leaves_no_partial_record_in_raw_tables(tmp_path):
    """Kill mid-write, then verify raw records and journal tables are clean."""
    db = tmp_path / "aletheia.db"
    setup = Substrate(db, scope_engine=ScopeEngine(known_scopes=KNOWN))
    _seed_source(setup, sorted(KNOWN))
    setup.put(make_record("committed"), CTX)
    setup.close()

    script = textwrap.dedent(
        f"""
        import os, signal, sys
        sys.path.insert(0, {str(REPO)!r})
        from aletheia.scope import ScopeContext, ScopeEngine
        from aletheia.substrate import Record, Substrate
        s = Substrate({str(db)!r}, scope_engine=ScopeEngine(known_scopes={sorted(KNOWN)!r}))
        def boom(*a, **k):
            os.kill(os.getpid(), signal.SIGKILL)
        s._journal = boom
        ctx = ScopeContext(granted_scopes=frozenset({sorted(KNOWN)[0], "memory-providers"}))
        s.put(Record(record_id="doomed", record_type="Observation",
                     scope="memory-providers", payload={{"text": "never committed"}}), ctx)
        """
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)
    assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT record_id FROM records WHERE record_id = 'doomed'").fetchall()
        assert len(rows) == 0, f"doomed record leaked into raw records table: {rows}"
        journal_rows = conn.execute("SELECT record_id FROM journal WHERE record_id = 'doomed'").fetchall()
        assert len(journal_rows) == 0, f"doomed journal entry leaked: {journal_rows}"
        fts_rows = conn.execute("SELECT record_id FROM fts WHERE record_id = 'doomed'").fetchall()
        assert len(fts_rows) == 0, f"doomed FTS entry leaked: {fts_rows}"
        committed_rows = conn.execute("SELECT record_id FROM records WHERE record_id = 'committed'").fetchall()
        assert len(committed_rows) == 1, "committed record missing from raw records table"
    finally:
        conn.close()


