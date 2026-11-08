from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from aletheia.llm_client import complete_text


def _response(payload: dict):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    manager = MagicMock()
    manager.__enter__.return_value = response
    manager.__exit__.return_value = False
    return manager


def test_openai_compatible_endpoint_uses_user_selected_model_and_key_env(monkeypatch):
    monkeypatch.setenv("ALETHEIA_LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("ALETHEIA_LLM_BASE_URL", "https://coding.example/v1")
    monkeypatch.setenv("ALETHEIA_LLM_MODEL", "user-selected-test-model")
    monkeypatch.setenv("ALETHEIA_LLM_API_KEY_ENV", "ALIBABA_CODING_PLAN_API_KEY")
    monkeypatch.setenv("ALIBABA_CODING_PLAN_API_KEY", "secret-test-key")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _response({"choices": [{"message": {"content": "expanded"}}]})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = complete_text("expand this", max_tokens=77, temperature=0.25)

    request = captured["request"]
    payload = json.loads(request.data)
    assert result == "expanded"
    assert request.full_url == "https://coding.example/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer secret-test-key"
    assert payload["model"] == "user-selected-test-model"
    assert payload["max_tokens"] == 77
    assert payload["temperature"] == 0.25
    assert captured["timeout"] == 30


def test_anthropic_provider_uses_messages_api_and_anthropic_headers(monkeypatch):
    monkeypatch.setenv("ALETHEIA_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ALETHEIA_LLM_MODEL", "claude-sonnet-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-key")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return _response({"content": [{"type": "text", "text": "answer"}]})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = complete_text("prompt", max_tokens=99, temperature=0.4)

    request = captured["request"]
    payload = json.loads(request.data)
    assert result == "answer"
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_header("X-api-key") == "anthropic-test-key"
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert payload["model"] == "claude-sonnet-test"
    assert payload["max_tokens"] == 99
    assert payload["messages"] == [{"role": "user", "content": "prompt"}]


def test_native_openai_provider_uses_chat_completions_api(monkeypatch):
    monkeypatch.setenv("ALETHEIA_LLM_PROVIDER", "openai")
    monkeypatch.setenv("ALETHEIA_LLM_MODEL", "openai-test-model")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return _response({"choices": [{"message": {"content": "openai answer"}}]})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = complete_text("prompt")

    request = captured["request"]
    assert result == "openai answer"
    assert request.full_url == "https://api.openai.com/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer openai-test-key"
    assert json.loads(request.data)["model"] == "openai-test-model"
def test_missing_provider_configuration_does_not_make_network_call(monkeypatch):
    for name in (
        "ALETHEIA_LLM_PROVIDER",
        "ALETHEIA_LLM_MODEL",
        "ALETHEIA_LLM_API_KEY",
        "ALETHEIA_LLM_API_KEY_ENV",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    with patch("urllib.request.urlopen") as urlopen:
        assert complete_text("prompt") is None
    urlopen.assert_not_called()


def test_resolved_configuration_redacts_api_key_from_repr(monkeypatch):
    monkeypatch.setenv("ALETHEIA_LLM_PROVIDER", "openai")
    monkeypatch.setenv("ALETHEIA_LLM_MODEL", "model")
    monkeypatch.setenv("OPENAI_API_KEY", "distinct-test-secret")
    from aletheia.llm_client import get_config

    config = get_config()
    assert config is not None
    assert "distinct-test-secret" not in repr(config)


def test_unknown_provider_is_rejected_without_network_call(monkeypatch):
    monkeypatch.setenv("ALETHEIA_LLM_PROVIDER", "tokenharbor")
    monkeypatch.setenv("ALETHEIA_LLM_MODEL", "some-model")
    monkeypatch.setenv("ALETHEIA_LLM_API_KEY", "key")
    with patch("urllib.request.urlopen") as urlopen:
        with pytest.raises(ValueError, match="unsupported Aletheia LLM provider"):
            complete_text("prompt")
    urlopen.assert_not_called()
