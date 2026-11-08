"""Provider-agnostic LLM transport for optional Aletheia features.

Supports native OpenAI and Anthropic APIs plus arbitrary OpenAI-compatible
endpoints. No provider is selected by default; external calls require explicit
provider/model/key configuration.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse


_OPENAI_BASE = "https://api.openai.com/v1"
_ANTHROPIC_BASE = "https://api.anthropic.com/v1"
_SUPPORTED = {"openai", "anthropic", "openai-compatible"}


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)


def _base_url(provider: str) -> str:
    configured = os.environ.get("ALETHEIA_LLM_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    if provider == "openai":
        return _OPENAI_BASE
    if provider == "anthropic":
        return _ANTHROPIC_BASE
    raise ValueError("ALETHEIA_LLM_BASE_URL is required for openai-compatible providers")


def _api_key(provider: str, override: str | None = None) -> str:
    if override and override.strip():
        return override.strip()
    explicit = os.environ.get("ALETHEIA_LLM_API_KEY", "").strip()
    if explicit:
        return explicit
    key_env = os.environ.get("ALETHEIA_LLM_API_KEY_ENV", "").strip()
    if key_env:
        return os.environ.get(key_env, "").strip()
    defaults = {
        "openai": ("OPENAI_API_KEY",),
        "anthropic": ("ANTHROPIC_API_KEY",),
        "openai-compatible": (
            "OPENAI_API_KEY", "DASHSCOPE_API_KEY", "ALIBABA_CODING_PLAN_API_KEY",
        ),
    }
    for name in defaults[provider]:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def get_config(api_key: str | None = None) -> LLMConfig | None:
    """Resolve configuration from environment without exposing credential values."""
    provider = os.environ.get("ALETHEIA_LLM_PROVIDER", "").strip().lower()
    if not provider:
        return None
    if provider not in _SUPPORTED:
        raise ValueError(f"unsupported Aletheia LLM provider: {provider!r}")
    model = os.environ.get("ALETHEIA_LLM_MODEL", "").strip()
    if not model:
        return None
    key = _api_key(provider, api_key)
    if not key:
        return None
    return LLMConfig(provider=provider, model=model, base_url=_base_url(provider), api_key=key)


def is_configured() -> bool:
    """Whether a provider and model are selected (independent of key availability)."""
    provider = os.environ.get("ALETHEIA_LLM_PROVIDER", "").strip().lower()
    model = os.environ.get("ALETHEIA_LLM_MODEL", "").strip()
    return bool(provider and model)


def destination_host() -> str:
    """Host configured for the selected provider, or a safe label if unset."""
    provider = os.environ.get("ALETHEIA_LLM_PROVIDER", "").strip().lower()
    if provider not in _SUPPORTED:
        return "not-configured"
    try:
        return urlparse(_base_url(provider)).hostname or "not-configured"
    except ValueError:
        return "not-configured"


def complete_text(
    prompt: str,
    *,
    max_tokens: int = 256,
    temperature: float = 0.0,
    api_key: str | None = None,
    timeout: float = 30,
) -> str | None:
    """Send one prompt to the selected provider and return its text response.

    Returns ``None`` when no provider/model/key is configured. Transport and
    response errors are allowed to propagate so callers can apply their own
    fail-closed or local-fallback policy.
    """
    config = get_config(api_key)
    if config is None:
        return None

    if config.provider == "anthropic":
        endpoint = config.base_url
        if not endpoint.endswith("/messages"):
            endpoint += "/messages" if endpoint.endswith("/v1") else "/v1/messages"
        payload = {
            "model": config.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    else:
        endpoint = config.base_url
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        payload = {
            "model": config.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }

    from urllib.request import Request, urlopen

    request = Request(endpoint, data=json.dumps(payload).encode(), headers=headers)
    with urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode())

    if config.provider == "anthropic":
        blocks = result.get("content", [])
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    else:
        text = result["choices"][0]["message"]["content"]
        if isinstance(text, list):
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    text = text.strip() if isinstance(text, str) else ""
    return text or None
