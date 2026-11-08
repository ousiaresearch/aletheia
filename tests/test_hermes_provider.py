"""The Hermes memory-provider surface: import graph, readiness, profile binding, tool contract.

These tests never load a model and never touch the network. The provider's own readiness gate is
exercised by patching presence checks, and the recall path by substituting the retrieval function —
what is under test here is the contract with Hermes, not Aletheia's ranking.
"""

from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

import aletheia.providers.hermes as provider_mod
from aletheia.providers.hermes import AletheiaMemoryProvider

REPO = pathlib.Path(__file__).resolve().parents[1]
PROVIDER_FILE = REPO / "aletheia" / "providers" / "hermes.py"

FORBIDDEN_ROOTS = {"fastapi", "uvicorn", "starlette"}


@pytest.fixture(autouse=True)
def no_model_load_for_provider_tests(monkeypatch):
    monkeypatch.setattr(provider_mod, "_preflight_reranker", lambda: None)


class StubEmbedder:
    model_id = "stub-embedder"
    dim = 8

    def embed_query(self, text):
        return [0.1] * self.dim

    def embed_documents(self, texts):
        return [[0.1] * self.dim for _ in texts]


def _names_in(tree) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_provider_module_never_imports_the_server_surface():
    """A memory provider runs in-process: the FastAPI surfaces must stay out of its import graph."""
    imported = _names_in(ast.parse(PROVIDER_FILE.read_text()))
    offenders = {name for name in imported
                 if name.split(".")[0] in FORBIDDEN_ROOTS
                 or name in {"aletheia.server", "aletheia.mcp_server", "aletheia.shadow", "aletheia.server_common"}}
    assert offenders == set(), f"provider imports server-only modules: {sorted(offenders)}"


def test_importing_the_provider_does_not_import_the_server():
    """Order-independent: check in a clean interpreter, not against this process's sys.modules."""
    code = (
        "import json, sys;"
        "import aletheia.providers.hermes;"
        "print(json.dumps(sorted(m for m in ('aletheia.server','aletheia.mcp_server','aletheia.shadow','fastapi') "
        "if m in sys.modules)))"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-400:]
    assert json.loads(done.stdout.strip()) == [], "importing the provider pulled in a server module"


def test_provider_is_a_fresh_instance_per_profile():
    """The multiplexing contract: no profile state at module or class level."""
    a, b = AletheiaMemoryProvider(), AletheiaMemoryProvider()
    assert a is not b
    assert a._pending is not b._pending


def test_readiness_rejects_unloadable_reranker(monkeypatch):
    monkeypatch.setattr(provider_mod, "default_text_embedder", lambda: StubEmbedder())
    def fail():
        raise RuntimeError("broken local checkpoint")
    monkeypatch.setattr(provider_mod, "_preflight_reranker", fail, raising=False)
    provider = AletheiaMemoryProvider()
    assert not provider.is_available()
    assert "reranker" in provider.unavailable_reason()
    assert "broken local checkpoint" in provider.unavailable_reason()


def test_initialize_rejects_unloadable_reranker(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_mod, "default_text_embedder", lambda: StubEmbedder())
    def fail():
        raise RuntimeError("broken local checkpoint")
    monkeypatch.setattr(provider_mod, "_preflight_reranker", fail, raising=False)
    provider = AletheiaMemoryProvider()
    provider.initialize("s", hermes_home=str(tmp_path))
    assert not provider._ready
    assert "reranker" in provider._error


def test_readiness_names_the_missing_model(monkeypatch):
    def missing():
        raise RuntimeError("not present")
    monkeypatch.setattr(provider_mod, "default_text_embedder", missing)
    monkeypatch.setattr(provider_mod, "_preflight_reranker", missing)
    provider = AletheiaMemoryProvider()
    assert provider.is_available() is False
    reason = provider.unavailable_reason()
    assert "bge-m3" in reason and "cross-encoder" in reason
    assert "no" in reason.lower() and "fall" in reason.lower()  # says it does not fall back


def test_readiness_is_true_when_both_models_and_runtimes_are_present(monkeypatch):
    monkeypatch.setattr(provider_mod, "default_text_embedder", lambda: StubEmbedder())
    monkeypatch.setattr(provider_mod, "_preflight_reranker", lambda: None)
    provider = AletheiaMemoryProvider()
    assert provider.is_available() is True
    assert provider.unavailable_reason() == ""


def test_initialize_binds_paths_to_the_profile_it_is_given(tmp_path, monkeypatch):
    """Storage and scope come from ``hermes_home``, never from the ambient environment."""
    monkeypatch.setattr(provider_mod, "default_text_embedder", lambda: StubEmbedder())
    monkeypatch.setattr(provider_mod, "spawn_context_thread",
                        lambda target, **kw: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(provider_mod.Substrate, "reembed_missing", lambda *a, **k: {})

    one, two = tmp_path / "alpha", tmp_path / "beta"
    p1, p2 = AletheiaMemoryProvider(), AletheiaMemoryProvider()
    p1.initialize("s1", hermes_home=str(one))
    p2.initialize("s2", hermes_home=str(two))
    try:
        assert p1._db_path == one / "aletheia" / "aletheia.sqlite3"
        assert p2._db_path == two / "aletheia" / "aletheia.sqlite3"
        assert p1._db_path != p2._db_path
        assert p1._scope == "profile-alpha" and p2._scope == "profile-beta"
        assert p1._db_path.is_file(), "substrate should create the store"
    finally:
        p1.shutdown()
        p2.shutdown()


def test_initialize_fails_closed_when_the_embedder_is_missing(tmp_path, monkeypatch):
    def explode():
        raise RuntimeError("embedding model not present: expected mlx-community/bge-m3-mlx-4bit")

    monkeypatch.setattr(provider_mod, "default_text_embedder", explode)
    provider = AletheiaMemoryProvider()
    provider.initialize("s", hermes_home=str(tmp_path))
    assert provider._ready is False
    problem = json.loads(provider.handle_tool_call("aletheia_recall", {"query": "anything at all"}))
    assert "error" in problem and "bge-m3" in problem["error"]


def test_sync_skips_non_primary_contexts(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(provider_mod, "default_text_embedder", lambda: StubEmbedder())
    monkeypatch.setattr(provider_mod, "spawn_context_thread",
                        lambda target, **kw: SimpleNamespace(start=lambda: calls.append(target)))
    monkeypatch.setattr(provider_mod.Substrate, "reembed_missing", lambda *a, **k: {})
    provider = AletheiaMemoryProvider()
    provider.initialize("s", hermes_home=str(tmp_path), agent_context="subagent")
    calls.clear()  # initialize's own re-embed pass is expected; what matters is the turn write
    try:
        provider.sync_turn("hello", "hi")
        assert calls == [], "a subagent turn must not be written to the store"
    finally:
        provider.shutdown()


def test_queue_prefetch_then_prefetch_returns_the_block(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_mod, "default_text_embedder", lambda: StubEmbedder())
    monkeypatch.setattr(provider_mod, "spawn_context_thread",
                        lambda target, **kw: SimpleNamespace(start=lambda: target(*kw.get("args", ()))))
    monkeypatch.setattr(provider_mod.Substrate, "reembed_missing", lambda *a, **k: {})

    item = SimpleNamespace(record_id="abcd1234efgh", text="we agreed the ledger keeps every turn",
                           tier="full", derived=False, created_at="2026-11-01T10:00:00+00:00")
    receipt = SimpleNamespace(mode="hybrid", scope_consulted=["profile-x"],
                              selected=["abcd1234efgh"], tier="full")
    monkeypatch.setattr(provider_mod, "aletheia_recall", lambda *a, **k: ([item], receipt))

    provider = AletheiaMemoryProvider()
    provider.initialize("s", hermes_home=str(tmp_path))
    try:
        provider.queue_prefetch("what did we agree about the ledger?")
        block = provider.prefetch("what did we agree about the ledger?")
        assert block.startswith("<aletheia-recall>") and "ledger keeps every turn" in block
        assert "<receipt>hybrid" in block
        status = provider.recall_status()
        assert status is not None and status.count == 1 and status.provider_label == "Aletheia"
        assert provider.prefetch("again") == "", "a delivered block is consumed once"
    finally:
        provider.shutdown()


def test_tool_schemas_and_unknown_tool():
    provider = AletheiaMemoryProvider()
    names = {schema["name"] for schema in provider.get_tool_schemas()}
    assert names == {"aletheia_recall", "aletheia_ingest", "aletheia_forget", "aletheia_receipt"}
    for schema in provider.get_tool_schemas():
        assert schema["parameters"]["type"] == "object"
    assert "error" in json.loads(provider.handle_tool_call("aletheia_nope", {}))


def test_save_config_is_profile_scoped_and_private(tmp_path):
    provider = AletheiaMemoryProvider()
    provider.save_config({"db_path": str(tmp_path / "x.db"), "scope": "work"}, str(tmp_path))
    path = tmp_path / "aletheia" / "provider-config.json"
    assert json.loads(path.read_text()) == {"db_path": str(tmp_path / "x.db"), "scope": "work"}
    assert (path.stat().st_mode & 0o777) == 0o600


def test_backup_paths_covers_the_store(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_mod, "default_text_embedder", lambda: StubEmbedder())
    monkeypatch.setattr(provider_mod, "spawn_context_thread",
                        lambda target, **kw: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(provider_mod.Substrate, "reembed_missing", lambda *a, **k: {})
    provider = AletheiaMemoryProvider()
    provider.initialize("s", hermes_home=str(tmp_path))
    try:
        assert provider.backup_paths() == [str(tmp_path / "aletheia" / "aletheia.sqlite3")]
    finally:
        provider.shutdown()


def test_provider_satisfies_the_real_hermes_abc():
    """Against the installed Hermes, in a subprocess so this module's shim is not what is tested."""
    hermes_root = pathlib.Path.home() / ".hermes/hermes-agent"
    if not (hermes_root / "agent/memory_provider.py").is_file():
        pytest.skip("Hermes checkout not present")
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from aletheia.providers.hermes import AletheiaMemoryProvider as W;"
        "from agent.memory_provider import MemoryProvider as A;"
        "print(bool(issubclass(W, A)), sorted(W.__abstractmethods__))" % str(hermes_root)
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-500:]
    ok, abstract = done.stdout.strip().split(" ", 1)
    assert ok == "True", "provider is not a MemoryProvider subclass"
    assert abstract.strip() == "[]", f"unimplemented abstract methods: {abstract}"
