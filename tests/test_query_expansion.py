"""Tests for query expansion."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from aletheia.query_expansion import expand_query, _fuse_rrf


def test_expand_query_returns_list_on_success():
    token_getter = lambda: "fake-token"
    fake_response = MagicMock()
    fake_response.read.return_value = b'{"choices":[{"message":{"content":"[\\"alt 1\\",\\"alt 2\\"]"}}]}'
    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=fake_response)
    mock_cm.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=mock_cm):
        result = expand_query(token_getter, "original query", n=2)
    assert result[0] == "original query"
    assert len(result) <= 3
    assert "alt 1" in result


def test_expand_query_fail_closed():
    token_getter = lambda: ""
    result = expand_query(token_getter, "original query", n=2)
    assert result == ["original query"]


def test_fuse_rrf_combines_rankings():
    class Dummy:
        def __init__(self, rid):
            self.record_id = rid

    list1 = [Dummy("a"), Dummy("b")]
    list2 = [Dummy("b"), Dummy("c")]
    fused = _fuse_rrf([list1, list2], pool=10)
    assert fused.index("b") < fused.index("c")
