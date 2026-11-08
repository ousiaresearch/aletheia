import argparse
import os
import shlex
import re
from pathlib import Path

import pytest

import aletheia.retrieval as retrieval
import aletheia.temporal as temporal
from aletheia import cli
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Record, Substrate

ROOT = Path(__file__).resolve().parents[1]


class FakeEmbedder:
    model_id = "fake"

    def embed_query(self, q):
        return [1.0]


def _store(tmp_path, kind="Source", conf=None):
    sub = Substrate(str(tmp_path / "w.db"), ScopeEngine(known_scopes={"a"}))
    ctx = ScopeContext(granted_scopes={"a"}, user_authored=True)
    for rid in ("x1", "x2"):
        payload = {"text": "needle haystack"}
        if kind == "Claim":
            payload["confidence"] = conf[rid]
            sub.put(Record(rid, "Claim", "a", payload, epistemic_status="derived",
                           source_ids=[], attribution="agent"), ctx)
        else:
            sub.put(Record(rid, "Source", "a", payload, attribution="user"), ctx)
    return sub, ctx


def _hybrid(sub, ctx, monkeypatch, vec_same_order, boost_id):
    lex = sub.fts_candidates('"needle"', ctx, 10, scopes=["a"])
    assert set(lex) == {"x1", "x2"}
    vec = list(lex) if vec_same_order else list(reversed(lex))
    monkeypatch.setattr(sub, "vector_index_complete", lambda *a, **k: True)
    monkeypatch.setattr(sub, "vector_candidates", lambda *a, **k: vec)
    monkeypatch.setattr(temporal, "temporal_boost",
                        lambda *a, **k: {boost_id: 5.0})
    items, _ = retrieval.recall(sub, "needle", ctx, scope_hint="a", mode="hybrid",
                                embedder=FakeEmbedder(), rerank=False, limit=2)
    return lex, [i.record_id for i in items]


def test_hybrid_equal_bm25_unequal_fused_not_reordered_by_temporal(tmp_path, monkeypatch):
    sub, ctx = _store(tmp_path)
    lex = sub.fts_candidates('"needle"', ctx, 10, scopes=["a"])
    lex, got = _hybrid(sub, ctx, monkeypatch, True, lex[1])
    assert got == lex  # equal BM25 but vector agrees with lexical: fused unequal


def test_hybrid_equal_fused_allows_temporal_tiebreak(tmp_path, monkeypatch):
    sub, ctx = _store(tmp_path)
    lex = sub.fts_candidates('"needle"', ctx, 10, scopes=["a"])
    lex, got = _hybrid(sub, ctx, monkeypatch, False, lex[1])
    assert got == [lex[1], lex[0]]  # fused scores equal: boost may break the tie


def test_hybrid_equal_bm25_unequal_fused_not_reordered_by_confidence(tmp_path, monkeypatch):
    sub, ctx = _store(tmp_path, "Claim", {"x1": 0.1, "x2": 0.1})
    lex = sub.fts_candidates('"needle"', ctx, 10, scopes=["a"])
    conf = {lex[0]: 0.1, lex[1]: 0.9}
    for rid, c in conf.items():
        sub._conn.execute("UPDATE records SET payload=json_set(payload,'$.confidence',?) WHERE record_id=?", (c, rid))
    sub._conn.commit()
    monkeypatch.setattr(sub, "vector_index_complete", lambda *a, **k: True)
    monkeypatch.setattr(sub, "vector_candidates", lambda *a, **k: list(lex))
    items, _ = retrieval.recall(sub, "needle", ctx, scope_hint="a", mode="hybrid",
                                embedder=FakeEmbedder(), rerank=False, limit=2)
    assert [i.record_id for i in items] == lex


def test_boost_pairs_docstring_mentions_fused():
    assert "fused" in retrieval._boost_adjacent_pairs.__doc__.lower()


def test_lexical_mode_equal_bm25_still_tied(tmp_path, monkeypatch):
    sub, ctx = _store(tmp_path)
    lex = sub.fts_candidates('"needle"', ctx, 10, scopes=["a"])
    monkeypatch.setattr(temporal, "temporal_boost", lambda *a, **k: {lex[1]: 5.0})
    items, _ = retrieval.recall(sub, "needle", ctx, scope_hint="a", mode="lexical", limit=2)
    assert [i.record_id for i in items] == [lex[1], lex[0]]


# ---- rebuild command order ---------------------------------------------

def _documented_commands(text):
    return re.findall(r"`(aletheia [^`]*rebuild-fts[^`]*)`", text)


def test_documented_rebuild_commands_parse_with_real_parser():
    doc = (ROOT / "docs/turnkey-deployment.md").read_text()
    cmds = _documented_commands(doc)
    assert cmds
    for c in cmds:
        assert c.startswith("aletheia --db <path> rebuild-fts --scope "), c
        argv = shlex.split(c)[1:]
        ns = cli._parser().parse_args(argv)
        assert ns.command == "rebuild-fts" and ns.db == "<path>"
    for c in ("aletheia --db <path> reembed --scope <scope>",):
        assert c in doc


def test_503_message_command_parses(tmp_path):
    import inspect, aletheia.mcp_server as m
    src = inspect.getsource(m)
    assert "rebuild-fts --db" not in src
    assert "aletheia --db <path> rebuild-fts --scope" in src
    cli._parser().parse_args(["--db", "<path>", "rebuild-fts", "--scope", "s"])


def test_readme_and_docs_have_no_invalid_order():
    for f in ("docs/turnkey-deployment.md", "README.md", "docs/local-cli.md"):
        assert "rebuild-fts --db" not in (ROOT / f).read_text(), f


# ---- serve --db ---------------------------------------------------------

def test_serve_sets_db_path_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ALETHEIA_DB_PATH", "x"); monkeypatch.delenv("ALETHEIA_DB_PATH")
    monkeypatch.setenv("ALETHEIA_API_KEY", "k")
    monkeypatch.setenv("ALETHEIA_KNOWN_SCOPES", "a")
    args = cli._parser().parse_args(["--db", str(tmp_path / "x.db"), "serve"])
    cli._prepare_serve_env(args)
    assert os.environ["ALETHEIA_DB_PATH"] == str(tmp_path / "x.db")
    assert capsys.readouterr().err == ""


def test_serve_warns_on_conflicting_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ALETHEIA_DB_PATH", str(tmp_path / "env.db"))
    monkeypatch.setenv("ALETHEIA_API_KEY", "k")
    monkeypatch.setenv("ALETHEIA_KNOWN_SCOPES", "a")
    args = cli._parser().parse_args(["--db", str(tmp_path / "x.db"), "serve"])
    cli._prepare_serve_env(args)
    assert os.environ["ALETHEIA_DB_PATH"] == str(tmp_path / "env.db")
    assert "ALETHEIA_DB_PATH" in capsys.readouterr().err


def test_serve_warns_when_unconfigured_without_secrets(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ALETHEIA_DB_PATH", "x"); monkeypatch.delenv("ALETHEIA_DB_PATH")
    monkeypatch.delenv("ALETHEIA_API_KEY", raising=False)
    monkeypatch.delenv("ALETHEIA_KNOWN_SCOPES", raising=False)
    args = cli._parser().parse_args(["--db", str(tmp_path / "x.db"), "serve"])
    cli._prepare_serve_env(args)
    err = capsys.readouterr().err
    assert "ALETHEIA_API_KEY" in err and "ALETHEIA_KNOWN_SCOPES" in err


def test_serve_does_not_print_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ALETHEIA_DB_PATH", "x"); monkeypatch.delenv("ALETHEIA_DB_PATH")
    monkeypatch.setenv("ALETHEIA_API_KEY", "supersecretvalue")
    monkeypatch.delenv("ALETHEIA_KNOWN_SCOPES", raising=False)
    cli._prepare_serve_env(cli._parser().parse_args(["--db", str(tmp_path / "x.db"), "serve"]))
    assert "supersecretvalue" not in capsys.readouterr().err


# ---- docs content -------------------------------------------------------

def test_docs_vector_and_import_statements():
    doc = " ".join((ROOT / "docs/turnkey-deployment.md").read_text().lower().split())
    assert "no vector" in doc and "hybrid recall refuses" in doc
    assert "reembed" in doc and "added in this round" not in doc  # R6-09: stale phrase removed
    assert "inert" in doc and "deprecated" in doc
    assert "never transition" in doc
    assert "fresh approval" in doc
