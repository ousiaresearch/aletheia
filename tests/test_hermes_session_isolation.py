"""Regression tests for Hermes prefetch session isolation."""
from weft.providers.hermes import WeftMemoryProvider


def test_prefetch_never_consumes_another_sessions_pending_recall():
    provider = WeftMemoryProvider()
    provider._pending['session-a'] = 'private recall for A'
    assert provider.prefetch('query', session_id='session-b') == ''
    assert provider.prefetch('query', session_id='session-a') == 'private recall for A'
