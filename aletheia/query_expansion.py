"""Query expansion for improved recall coverage.

Generates alternative phrasings of a query using an LLM, runs recall
for each, and fuses results with RRF. This helps match paraphrases
and synonym uses in the corpus.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Sequence

from aletheia.llm_client import complete_text

logger = logging.getLogger(__name__)


def _local_expand_query(query: str, n: int = 3) -> list[str]:
    """Local fallback: generate paraphrases with a small seq2seq model.

    Uses google/flan-t5-small if available. Returns [query] on any error.
    """
    try:
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
        import torch

        model_name = "google/flan-t5-small"
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSeq2SeqLM.from_pretrained(model_name)

        prompt = (
            f"Generate {n} alternative ways to ask the following question, "
            f"preserving the same meaning but using different wording, synonyms, "
            f"or paraphrases. Return ONLY a JSON array of strings.\n\n"
            f"Original: {query}\n\n"
            f"Alternatives:"
        )
        inputs = tokenizer(prompt, return_tensors="pt")
        with torch.no_grad():
            outputs = model.generate(**inputs, max_new_tokens=150, temperature=0.7)
        text = tokenizer.decode(outputs[0], skip_special_tokens=True).strip()

        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            alts = json.loads(text[start:end])
            if isinstance(alts, list):
                seen = {query}
                out = [query]
                for alt in alts:
                    if isinstance(alt, str) and alt.strip() and alt.strip() not in seen:
                        out.append(alt.strip())
                        seen.add(alt.strip())
                    if len(out) >= n + 1:
                        break
                return out
    except Exception as e:
        logger.warning("Local query expansion failed: %s", e)
    return [query]


def _provider_text(
    prompt: str,
    token_getter: Callable[[], str | None] | None = None,
    *,
    max_tokens: int,
    temperature: float,
) -> str | None:
    """Call the explicitly configured provider; token_getter remains a legacy key hook."""
    api_key = token_getter() if callable(token_getter) else None
    return complete_text(prompt, max_tokens=max_tokens, temperature=temperature, api_key=api_key)


def _json_strings(text: str | None) -> list[str]:
    if not text:
        return []
    start, end = text.find("["), text.rfind("]") + 1
    if start < 0 or end <= start:
        return []
    values = json.loads(text[start:end])
    if not isinstance(values, list):
        return []
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def expand_query(
    token_getter: Callable[[], str | None] | None = None,
    query: str = "",
    n: int = 3,
) -> list[str]:
    """Generate query alternatives through the configured provider, then local fallback.

    Provider routing is controlled by ALETHEIA_LLM_PROVIDER / MODEL / BASE_URL.
    The caller must opt into query expansion; this function never chooses a vendor.
    """
    prompt = (
        f"Generate {n} alternative ways to ask the following question, "
        f"preserving the same meaning but using different wording, synonyms, "
        f"or paraphrases. Return ONLY a JSON array of strings.\n\n"
        f"Original: {query}\n\nAlternatives:"
    )
    try:
        values = _json_strings(_provider_text(prompt, token_getter, max_tokens=150, temperature=0.7))
        seen = {query}
        out = [query]
        for value in values:
            if value not in seen:
                out.append(value)
                seen.add(value)
            if len(out) >= n + 1:
                break
        if len(out) > 1:
            return out
    except Exception as e:
        logger.warning("Configured-provider query expansion failed: %s", e)
    return _local_expand_query(query, n)


def expand_query_hyde(
    token_getter: Callable[[], str | None] | None = None,
    query: str = "",
) -> list[str]:
    """Generate a hypothetical answer through the selected provider or local fallback."""
    prompt = (
        "Write a brief, factual hypothetical answer to the following question. "
        "Do not hedge or say 'I don't know'. Keep it to 2-3 sentences.\n\n"
        f"Question: {query}\n\nHypothetical answer:"
    )
    try:
        text = _provider_text(prompt, token_getter, max_tokens=120, temperature=0.3)
        if text:
            return [query, text]
    except Exception as e:
        logger.warning("Configured-provider HyDE expansion failed: %s", e)

    try:
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
        import torch

        model_name = "google/flan-t5-small"
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSeq2SeqLM.from_pretrained(model_name)
        inputs = tokenizer(prompt, return_tensors="pt")
        with torch.no_grad():
            outputs = model.generate(**inputs, max_new_tokens=120, temperature=0.3)
        text = tokenizer.decode(outputs[0], skip_special_tokens=True).strip()
        if text:
            return [query, text]
    except Exception as e:
        logger.warning("Local HyDE expansion failed: %s", e)
    return [query]


def classify_query(query: str) -> str:
    """P1: rule-based question typing for retrieval tuning.

    Returns one of: 'who', 'what', 'when', 'where', 'why', 'how', 'which', 'other'.
    """
    q = (query or "").lower().strip()
    if not q:
        return "other"
    # Match leading interrogative patterns.
    patterns = [
        (r"^(who|what|when|where|why|how|which)\b", lambda m: m.group(1)),
        (r"\bwho\b", "who"),
        (r"\bwhat\b", "what"),
        (r"\bwhen\b", "when"),
        (r"\bwhere\b", "where"),
        (r"\bwhy\b", "why"),
        (r"\bhow\b", "how"),
        (r"\bwhich\b", "which"),
    ]
    for pattern, extractor in patterns:
        m = re.search(pattern, q)
        if m:
            val = extractor(m) if callable(extractor) else extractor
            if isinstance(val, str):
                return val
            return "other"
    return "other"


def expand_query_multihop(
    token_getter: Callable[[], str | None] | None = None,
    query: str = "",
) -> list[str]:
    """Decompose a compound question using the selected provider, if configured."""
    if not re.search(r"\b(and|or|also|then|after|before|because|since|while)\b", query, re.IGNORECASE):
        return [query]
    prompt = (
        "Decompose the following question into 2 simpler sub-questions that "
        "together would answer the original. Return ONLY a JSON array of strings.\n\n"
        f"Original: {query}\n\nSub-questions:"
    )
    try:
        values = _json_strings(_provider_text(prompt, token_getter, max_tokens=120, temperature=0.0))
        return [query] + [value for value in values if value != query]
    except Exception as e:
        logger.warning("Configured-provider multi-hop expansion failed: %s", e)
        return [query]


def _fuse_rrf(ranked_lists: Sequence[list], pool: int, k: int = 60) -> list:
    """Reciprocal Rank Fusion over multiple ranked ID lists."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for pos, item in enumerate(ranked):
            rid = getattr(item, "record_id", item) if hasattr(item, "record_id") else item
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + pos + 1)
    ordered = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    return [rid for rid, _ in ordered[:pool]]
