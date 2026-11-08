"""Tests for query expansion."""
from __future__ import annotations

from unittest.mock import patch

from aletheia.query_expansion import expand_query, _fuse_rrf


def test_expand_query_returns_list_on_success():
    with patch("aletheia.query_expansion.complete_text", return_value='["alt 1", "alt 2"]') as complete:
        result = expand_query(None, "original query", n=2)
    complete.assert_called_once()
    assert result[0] == "original query"
    assert len(result) <= 3
    assert "alt 1" in result


def test_expand_query_falls_back_without_provider(monkeypatch):
    monkeypatch.setattr("aletheia.query_expansion.complete_text", lambda *a, **k: None)
    monkeypatch.setattr("aletheia.query_expansion._local_expand_query", lambda query, n: [query])
    result = expand_query(None, "original query", n=2)
    assert result == ["original query"]


def test_fuse_rrf_combines_rankings():
    class Dummy:
        def __init__(self, rid):
            self.record_id = rid

    list1 = [Dummy("a"), Dummy("b")]
    list2 = [Dummy("b"), Dummy("c")]
    fused = _fuse_rrf([list1, list2], pool=10)
    assert fused.index("b") < fused.index("c")
