"""The multiplexed-gateway shape: three profiles, one process, one store, three scopes.

This is the regression that would have caught the original defect. Before 2.1 each profile opened its
own database, so a grant could never reach another profile's records, and every profile paid for its
own copy of the models.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aletheia.ingest import Ingestor
from aletheia.providers.hermes import AletheiaMemoryProvider
from aletheia.retrieval import recall


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    monkeypatch.setattr(AletheiaMemoryProvider, "_missing_requirements", lambda self: [])


def _profile(root: Path, name: str, **config) -> Path:
    home = root / "profiles" / name
    (home / "aletheia").mkdir(parents=True, exist_ok=True)
    (home / "aletheia" / "provider-config.json").write_text(json.dumps(config))
    return home


def test_three_profiles_one_store(tmp_path):
    root = tmp_path / ".hermes"
    homes = {
        "default": root,  # the default profile is the root home, not a directory named after one
        "isildur": _profile(root, "isildur"),
        "palantir": _profile(root, "palantir", read_scopes=["profile:isildur"]),
    }
    # Grants are host policy: palantir may read isildur, and asks for it in its own config.
    policy = root / "aletheia" / "policy.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps({"profiles": {"palantir": {"read_scopes": ["profile:isildur"]}}}))
    os.chmod(policy, 0o600)
    providers = {}
    for name, home in homes.items():
        home.mkdir(parents=True, exist_ok=True)
        provider = AletheiaMemoryProvider()
        provider.initialize(f"session-{name}", hermes_home=str(home), agent_identity=name,
                            agent_context="primary")
        providers[name] = provider
    try:
        # One file, three scopes, three independent connections.
        assert len({p._db_path for p in providers.values()}) == 1
        assert {p._scope for p in providers.values()} == {
            "profile:default", "profile:isildur", "profile:palantir"}
        assert len({id(p._substrate) for p in providers.values()}) == 3

        Ingestor(providers["isildur"]._substrate, embedder=None).ingest_turn(
            "Isildur keeps the northern ledger.", "user", providers["isildur"]._ctx,
            "profile:isildur", "s", attribution="user")

        granted, receipt = recall(providers["palantir"]._substrate, "northern ledger",
                                  providers["palantir"]._ctx_read,
                                  providers["palantir"]._recall_scope_hint(),
                                  limit=5, mode="lexical")
        denied, denied_receipt = recall(providers["default"]._substrate, "northern ledger",
                                        providers["default"]._ctx_read,
                                        providers["default"]._recall_scope_hint(),
                                        limit=5, mode="lexical")

        assert granted, "palantir holds a read grant for isildur"
        assert "profile:isildur" in receipt.scope_consulted
        assert not denied, "the default profile holds no grant for isildur"
        assert denied_receipt.scope_consulted == ["profile:default"]
    finally:
        for provider in providers.values():
            provider.shutdown()


def test_the_default_profile_is_never_named_after_its_directory(tmp_path):
    root = tmp_path / ".hermes"
    root.mkdir()
    provider = AletheiaMemoryProvider()
    provider.initialize("s", hermes_home=str(root), agent_identity="default")
    try:
        assert provider._scope == "profile:default"
        assert provider._profile == "default"
        assert "." not in provider._scope
    finally:
        provider.shutdown()