"""Default recall mode is hybrid after review 3544e20 APPROVE_WITH_RESIDUALS (user: ship with vector on).

Tests first. A default recall with no embedder is a visible error, never a silent lexical fallback.
"""

import inspect

import pytest

from aletheia.retrieval import recall
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate

KNOWN = {"acs-project"}
CTX = ScopeContext(granted_scopes=frozenset(KNOWN), actor="x")


def test_default_mode_is_hybrid():
    default = inspect.signature(recall).parameters["mode"].default
    assert default == "hybrid"


def test_default_recall_without_embedder_is_a_visible_error(tmp_path):
    sub = Substrate(str(tmp_path / "w.db"), ScopeEngine(KNOWN, {}))
    try:
        with pytest.raises(ValueError):
            recall(sub, "anything", CTX)
    finally:
        sub.close()
