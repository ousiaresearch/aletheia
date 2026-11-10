"""Test-suite isolation.

An LLM provider configured in the developer's shell must not change what a test means. This came up
for real: running a benchmark in the same terminal exported ALETHEIA_LLM_PROVIDER,
ALETHEIA_LLM_MODEL and ALETHEIA_LLM_API_KEY_ENV, after which five tests failed — three reader tests
that assert behaviour when no reader is configured, and two transport tests that expect their own
provider's key to be used, because the ambient ALETHEIA_LLM_API_KEY_ENV shadowed it. None of those
failures had anything to do with the code under test.

Tests that need a provider set one explicitly with monkeypatch, which runs after this fixture.
"""

from __future__ import annotations

import pytest

#: Variables that select a provider, point at an endpoint, name a key, or switch on a feature whose
#: behaviour tests assert in its default state.
_AMBIENT = (
    "ALETHEIA_LLM_PROVIDER",
    "ALETHEIA_LLM_MODEL",
    "ALETHEIA_LLM_BASE_URL",
    "ALETHEIA_LLM_API_KEY",
    "ALETHEIA_LLM_API_KEY_ENV",
    "ALETHEIA_QUERY_EXPANSION",
)


@pytest.fixture(autouse=True)
def _isolate_ambient_llm_config(monkeypatch):
    for name in _AMBIENT:
        monkeypatch.delenv(name, raising=False)
    yield