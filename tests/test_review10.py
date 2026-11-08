"""Wire the adopted embedder into ingestion (tests first).

A missing model is a visible failure, never a silent fallback. When the real model is present,
an ingested turn gets a vector that retrieval finds under the same model id.
"""

import glob
import os

import pytest

from aletheia.adapters import default_text_embedder
from aletheia.ingest import Ingestor
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate

KNOWN = {"memory-providers"}
USER = ScopeContext(granted_scopes=frozenset(KNOWN), user_authored=True, actor="johann")
MODEL_PRESENT = bool(glob.glob(os.path.expanduser(
    "~/.cache/huggingface/hub/models--mlx-community--bge-m3-mlx-4bit/snapshots/*")))


def test_missing_model_is_a_visible_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))  # empty cache: no snapshots
    with pytest.raises(RuntimeError, match="embedding model not present"):
        default_text_embedder()


def test_partial_snapshot_is_a_visible_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    snapshot = tmp_path / ".cache/huggingface/hub/models--mlx-community--bge-m3-mlx-4bit/snapshots/incomplete"
    snapshot.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="embedding model unavailable"):
        default_text_embedder()


@pytest.mark.skipif(not MODEL_PRESENT, reason="BGE-M3 4-bit weights not in the local cache")
def test_ingested_turn_gets_a_vector_under_the_adopted_model(tmp_path):
    emb = default_text_embedder()
    sub = Substrate(str(tmp_path / "w.db"), ScopeEngine(KNOWN, {}))
    try:
        rid = Ingestor(sub, embedder=emb).ingest_turn(
            "the lighthouse keepers log the tides each night", "user", USER, "memory-providers", "c1")
        q = emb.embed_query("who records the tides at night")
        assert rid in sub.vector_candidates(q, USER, 5, model_id=emb.model_id)
    finally:
        sub.close()
