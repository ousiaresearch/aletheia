"""Tests for cross-encoder reranking."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aletheia.retrieval import Item
from aletheia.reranking import CrossEncoderReranker


def test_reranker_empty_items():
    rr = CrossEncoderReranker.__new__(CrossEncoderReranker)
    rr._model = MagicMock()
    assert rr.rerank("query", []) == []


def test_reranker_reports_prediction_failure():
    rr = CrossEncoderReranker.__new__(CrossEncoderReranker)
    rr._model = MagicMock()
    rr._model.predict.side_effect = RuntimeError("bad weights")
    items = [Item("a", "scope1", "text a", "L0")]
    with pytest.raises(RuntimeError, match="bad weights"):
        rr.rerank("query", items)


def test_reranker_loads_local_only_and_reports_failure(monkeypatch):
    import sys
    from types import SimpleNamespace

    constructor = MagicMock(side_effect=RuntimeError("not cached"))
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=constructor))
    rr = CrossEncoderReranker.__new__(CrossEncoderReranker)
    rr._model = None
    with pytest.raises(RuntimeError, match="not cached"):
        rr._load()
    constructor.assert_called_once_with(rr.model_name, local_files_only=True)
    assert rr._model is None


def test_reranker_sorts_by_score():
    rr = CrossEncoderReranker.__new__(CrossEncoderReranker)
    rr._model = MagicMock()
    rr._model.predict.return_value = [0.2, 0.8]
    items = [Item("a", "scope1", "text a", "L0"), Item("b", "scope1", "text b", "L0")]
    result = rr.rerank("query", items, top_k=2)
    assert [it.record_id for it in result] == ["b", "a"]
