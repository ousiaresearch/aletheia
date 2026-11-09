"""A multiplexed gateway: one store, one scope per profile, grants decided by host policy.

No model is loaded: the embedder is stubbed out and recall runs in lexical mode, which is enough to
prove which scopes an instance may read and write.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aletheia.ingest import Ingestor, ScopeViolation
from aletheia.merge import upgrade_scope
from aletheia.providers.hermes import AletheiaMemoryProvider
from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    monkeypatch.setattr(AletheiaMemoryProvider, "_missing_requirements", lambda self: [])


def _write_config(home: Path, **values) -> None:
    (home / "aletheia").mkdir(parents=True, exist_ok=True)
    (home / "aletheia" / "provider-config.json").write_text(json.dumps(values))


def _write_policy(root: Path, profiles: dict) -> None:
    path = root / "aletheia" / "policy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"profiles": profiles}))
    os.chmod(path, 0o600)


def _init(home: Path, identity: str) -> AletheiaMemoryProvider:
    provider = AletheiaMemoryProvider()
    provider.initialize("test-session", hermes_home=str(home), agent_identity=identity,
                        agent_context="primary")
    return provider


def _ingest(provider: AletheiaMemoryProvider, text: str) -> str:
    ingestor = Ingestor(provider._substrate, embedder=None)
    return ingestor.ingest_turn(text, "user", provider._ctx, provider._scope, "test-session",
                                attribution="user")


def _search(provider: AletheiaMemoryProvider, query: str):
    return recall(provider._substrate, query, provider._ctx_read,
                  provider._recall_scope_hint(), limit=5, mode="lexical")


def test_two_profiles_share_one_store(tmp_path):
    root = tmp_path / ".hermes"
    a, b = root / "profiles" / "a", root / "profiles" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    pa, pb = _init(a, "a"), _init(b, "b")
    try:
        assert pa._db_path == pb._db_path == root / "aletheia" / "aletheia.sqlite3"
        assert pa._db_path.is_file()
        assert pa._scope == "profile:a" and pb._scope == "profile:b"
        assert pa._substrate is not pb._substrate, "one connection each"
        assert (root / "aletheia").stat().st_mode & 0o077 == 0, "the shared store dir is private"
    finally:
        pa.shutdown()
        pb.shutdown()


def test_a_profile_cannot_grant_itself_by_editing_its_own_config(tmp_path):
    """The finding: config used to be the grant authority, so every profile was self-service."""
    root = tmp_path / ".hermes"
    a, b = root / "profiles" / "a", root / "profiles" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _write_config(b, read_scopes=["profile:a"])  # a request with no policy behind it

    pa, pb = _init(a, "a"), _init(b, "b")
    try:
        assert pb._read_scopes == ("profile:b",)
        _ingest(pa, "The lighthouse at Ardmore was decommissioned in 1974.")
        items, receipt = _search(pb, "Ardmore lighthouse")
        assert not items
        assert receipt.scope_consulted == ["profile:b"]
    finally:
        pa.shutdown()
        pb.shutdown()


def test_policy_grant_crosses_profiles(tmp_path):
    root = tmp_path / ".hermes"
    a, b = root / "profiles" / "a", root / "profiles" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _write_policy(root, {"b": {"read_scopes": ["profile:a"]}})
    _write_config(b, read_scopes=["profile:a"])

    pa, pb = _init(a, "a"), _init(b, "b")
    try:
        assert pb._read_scopes == ("profile:b", "profile:a")
        _ingest(pa, "The lighthouse at Ardmore was decommissioned in 1974.")
        items, receipt = _search(pb, "Ardmore lighthouse")
        assert [i.record_id for i in items]
        assert "profile:a" in receipt.scope_consulted and "profile:b" in receipt.scope_consulted
    finally:
        pa.shutdown()
        pb.shutdown()


def test_policy_without_a_request_grants_nothing_extra(tmp_path):
    root = tmp_path / ".hermes"
    a, b = root / "profiles" / "a", root / "profiles" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _write_policy(root, {"b": {"read_scopes": ["profile:a"]}})
    # The policy permits; the profile never asked.

    pa, pb = _init(a, "a"), _init(b, "b")
    try:
        assert pb._read_scopes == ("profile:b",)
    finally:
        pa.shutdown()
        pb.shutdown()


def test_read_grant_never_confers_write(tmp_path):
    root = tmp_path / ".hermes"
    a, b = root / "profiles" / "a", root / "profiles" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _write_policy(root, {"b": {"read_scopes": ["profile:a"]}})
    _write_config(b, read_scopes=["profile:a"])

    pa, pb = _init(a, "a"), _init(b, "b")
    try:
        with pytest.raises(ScopeViolation):
            Ingestor(pb._substrate, embedder=None).ingest_turn(
                "forged", "user", pb._ctx, "profile:a", "test-session", attribution="user")
        assert pb._ctx.granted_scopes == frozenset({"profile:b"})
        assert pb._ctx_read.granted_scopes == frozenset({"profile:a", "profile:b"})
    finally:
        pa.shutdown()
        pb.shutdown()


def test_shared_scope_is_opt_in_through_the_policy(tmp_path):
    root = tmp_path / ".hermes"
    a = root / "profiles" / "a"
    a.mkdir(parents=True)
    _write_config(a, shared=True, shared_write=True)
    pa = _init(a, "a")
    try:
        assert pa._read_scopes == ("profile:a",), "config alone is not authority"
        assert pa._write_scopes == ("profile:a",)
    finally:
        pa.shutdown()

    _write_policy(root, {"a": {"shared": True, "shared_write": True}})
    pa = _init(a, "a")
    try:
        assert pa._read_scopes == ("profile:a", "shared")
        assert pa._write_scopes == ("profile:a", "shared")
    finally:
        pa.shutdown()


def test_a_configured_scope_cannot_rename_the_profile(tmp_path):
    """The critical finding: config `scope` let one profile become another."""
    root = tmp_path / ".hermes"
    a, b = root / "profiles" / "a", root / "profiles" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _write_config(b, scope="profile:a")

    pa, pb = _init(a, "a"), _init(b, "b")
    try:
        assert pb._scope == "profile:b"
        assert pb._ctx.granted_scopes == frozenset({"profile:b"})
        _ingest(pa, "a's private memory")
        items, _ = _search(pb, "private memory")
        assert not items
    finally:
        pa.shutdown()
        pb.shutdown()


def test_legacy_scope_is_reported_not_silently_renamed(tmp_path):
    root = tmp_path / ".hermes"
    root.mkdir()
    db = root / "aletheia" / "aletheia.sqlite3"
    db.parent.mkdir(parents=True)
    engine = ScopeEngine(known_scopes={"profile-.hermes"})
    ctx = ScopeContext(granted_scopes=frozenset({"profile-.hermes"}), user_authored=True)
    sub = Substrate(db, engine)
    try:
        Ingestor(sub, embedder=None).ingest_turn("old memory", "user", ctx, "profile-.hermes",
                                                 "legacy", attribution="user")
    finally:
        sub.close()

    provider = _init(root, "default")
    try:
        # Opening does not move another profile's records; it names the problem.
        assert provider._scope == "profile:default"
        assert provider._legacy_scope == "profile-.hermes"
        assert provider._substrate.distinct_scopes() == ["profile-.hermes"]
    finally:
        provider.shutdown()


def test_rename_scope_moves_fts_and_recall_works(tmp_path):
    """The migration must leave a store that can actually answer a query."""
    db = tmp_path / "host.sqlite3"
    engine = ScopeEngine(known_scopes={"profile-old"})
    ctx = ScopeContext(granted_scopes=frozenset({"profile-old"}), user_authored=True)
    sub = Substrate(db, engine)
    try:
        Ingestor(sub, embedder=None).ingest_turn(
            "The lighthouse at Ardmore was decommissioned in 1974.", "user", ctx, "profile-old",
            "s", attribution="user")
    finally:
        sub.close()

    result = upgrade_scope(db, "profile-old", "profile:new")
    assert result["changed"].get("records") == 1, result
    assert result["changed"].get("fts") == 1, result

    engine = ScopeEngine(known_scopes={"profile:new"})
    ctx = ScopeContext(granted_scopes=frozenset({"profile:new"}), user_authored=True)
    sub = Substrate(db, engine)
    try:
        items, _ = recall(sub, "Ardmore lighthouse", ctx, limit=5, mode="lexical")
        assert [i.record_id for i in items], "the renamed scope must be searchable"
        assert sub.fts_index_complete("profile:new", ctx)
    finally:
        sub.close()


def test_config_round_trip(tmp_path):
    home = tmp_path / ".hermes" / "profiles" / "a"
    home.mkdir(parents=True)
    provider = AletheiaMemoryProvider()
    provider.save_config({"store_path": "/tmp/shared.sqlite3", "scope": "profile:a",
                          "read_scopes": "profile:b, shared", "shared": "false",
                          "shared_write": "true"}, str(home))
    data = json.loads((home / "aletheia" / "provider-config.json").read_text())
    assert data["store_path"] == "/tmp/shared.sqlite3"
    assert data["read_scopes"] == ["profile:b", "shared"]
    assert data["shared"] is False and data["shared_write"] is True


def test_identity_signature_reports_what_is_live(tmp_path):
    home = tmp_path / ".hermes" / "profiles" / "a"
    home.mkdir(parents=True)
    provider = _init(home, "a")
    try:
        sig = provider.identity_signature()
        assert sig["profile"] == "a" and sig["scope"] == "profile:a"
        assert sig["version"] != "" and sig["read_scopes"] == ["profile:a"]
        assert sig["legacy_scope"] is None and sig["index_degraded"] is None
    finally:
        provider.shutdown()