import json
import pytest
from aletheia.substrate import SCHEMA_VERSION, Substrate, Record
from aletheia.scope import ScopeContext, ScopeEngine

KNOWN = {"s"}


def _seed_source(sub, scope):
    sub._conn.execute(
        "INSERT OR IGNORE INTO records (record_id, record_type, scope, epistemic_status, payload, "
        "created_at, effective_at, supersedes, source_ids, provenance, attribution) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("src-1-s", "Source", scope, "stated", '{"text": "seed"}', 0.0, None, None, "[]", "{}", "document"),
    )
    sub._conn.commit()


def test_export_scope_returns_portable_dict(tmp_path):
    sub = Substrate(tmp_path / "a.db", ScopeEngine(known_scopes=KNOWN))
    try:
        _seed_source(sub, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        sub._write(Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                          source_ids=["src-1-s"], payload={"text": "v1"}, effective_at=100.0),
                    ctx, validate_schema=True, overwrite=False)
        data = sub.export_scope("s", ctx)
        assert data["schema_version"] == SCHEMA_VERSION
        assert data["scope"] == "s"
        assert len(data["records"]) == 2  # Source + Observation
        texts = {r["payload"]["text"] for r in data["records"]}
        assert texts == {"seed", "v1"}
    finally:
        sub.close()


def test_import_scope_round_trip_preserves_content(tmp_path):
    sub1 = Substrate(tmp_path / "src.db", ScopeEngine(known_scopes=KNOWN))
    sub2 = Substrate(tmp_path / "dst.db", ScopeEngine(known_scopes=KNOWN))
    try:
        _seed_source(sub1, "s")
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        sub1._write(Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                           source_ids=["src-1-s"], payload={"text": "v1"}, effective_at=100.0),
                    ctx, validate_schema=True, overwrite=False)
        data = sub1.export_scope("s", ctx)
        imported = sub2.import_scope(data, ctx)
        assert imported == 2
        rows = sub2._conn.execute("SELECT record_id, payload FROM records ORDER BY record_id").fetchall()
        assert rows[0][0] == "r1"
        assert json.loads(rows[0][1])["text"] == "v1"
    finally:
        sub1.close()
        sub2.close()


def test_import_scope_rejects_bad_schema_version(tmp_path):
    sub = Substrate(tmp_path / "a.db", ScopeEngine(known_scopes=KNOWN))
    try:
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        with pytest.raises(ValueError, match="schema version mismatch"):
            sub.import_scope({"schema_version": 999, "scope": "s", "records": []}, ctx)
    finally:
        sub.close()


def test_export_scope_requires_context(tmp_path):
    sub = Substrate(tmp_path / "a.db", ScopeEngine(known_scopes=KNOWN))
    try:
        assert sub.export_scope("s", None) == {}
    finally:
        sub.close()


def test_import_scope_requires_context(tmp_path):
    sub = Substrate(tmp_path / "a.db", ScopeEngine(known_scopes=KNOWN))
    try:
        with pytest.raises(ValueError, match="context required"):
            sub.import_scope({"schema_version": 6, "scope": "s", "records": []}, None)
    finally:
        sub.close()


def test_import_scope_rejects_unverified_claim_without_verification(tmp_path):
    sub = Substrate(tmp_path / "a.db", ScopeEngine(known_scopes=KNOWN))
    try:
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True, actor="verifier")
        sub._conn.execute("INSERT INTO verifiers (actor_id, registered_at) VALUES (?, ?)", ("verifier", 0.0))
        sub._conn.commit()
        data = {
            "schema_version": 6,
            "scope": "s",
            "records": [
                {
                    "record_id": "v1",
                    "record_type": "Verification",
                    "scope": "s",
                    "epistemic_status": "stated",
                    "payload": {"text": "verifier note"},
                    "created_at": 0.0,
                    "effective_at": None,
                    "supersedes": None,
                    "source_ids": [],
                    "provenance": {},
                    "attribution": "verifier",
                },
                {
                    "record_id": "c1",
                    "record_type": "Claim",
                    "scope": "s",
                    "epistemic_status": "verified",
                    "payload": {"text": "unverified claim"},
                    "created_at": 0.0,
                    "effective_at": None,
                    "supersedes": None,
                    "source_ids": ["v1"],
                    "provenance": {},
                    "attribution": "verifier",
                },
            ],
        }
        # Refusing the invalid Claim rolls back the entire import, including v1.
        imported = sub.import_scope(data, ctx)
        assert imported == 0
        assert sub.get("c1", ctx) is None
        assert sub.get("v1", ctx) is None
    finally:
        sub.close()


def test_deleted_records_absent_from_export(tmp_path):
    sub = Substrate(tmp_path / "a.db", ScopeEngine(known_scopes=KNOWN))
    try:
        ctx = ScopeContext(granted_scopes=frozenset({"s"}), user_authored=True)
        _seed_source(sub, "s")
        sub._write(Record(record_id="r1", record_type="Observation", scope="s", attribution="document",
                           source_ids=["src-1-s"], payload={"text": "keep"}, effective_at=100.0),
                    ctx, validate_schema=True, overwrite=False)
        sub._write(Record(record_id="r2", record_type="Observation", scope="s", attribution="document",
                           source_ids=["src-1-s"], payload={"text": "delete me"}, effective_at=100.0),
                    ctx, validate_schema=True, overwrite=False)
        sub._conn.commit()
        exported = sub.export_scope("s", ctx)
        assert len(exported["records"]) == 3
        sub.delete("r2", ctx, reason="test purge")
        exported_after = sub.export_scope("s", ctx)
        assert len(exported_after["records"]) == 2
        assert {r["record_id"] for r in exported_after["records"]} == {"src-1-s", "r1"}
    finally:
        sub.close()
