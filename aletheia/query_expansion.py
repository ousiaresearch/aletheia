"""Query expansion for improved recall coverage.

Generates alternative phrasings of a query using an LLM, runs recall
for each, and fuses results with RRF. This helps match paraphrases
and synonym uses in the corpus.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Sequence

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


def expand_query(token_getter, query: str, n: int = 3) -> list[str]:
    """Generate alternative phrasings for a query.

    Tries TokenHarbor first; falls back to local flan-t5-small if no token
    or network fails. Returns [original, alt1, alt2, ...] up to n+1 total.
    Fail-closed: returns [query] on any error.
    """
    token = token_getter()
    if token:
        prompt = (
            f"Generate {n} alternative ways to ask the following question, "
            f"preserving the same meaning but using different wording, synonyms, "
            f"or paraphrases. Return ONLY a JSON array of strings.\n\n"
            f"Original: {query}\n\n"
            f"Alternatives:"
        )
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://tokenharbor.ai/v1/chat/completions",
                data=json.dumps({
                    "model": "deepseek-v4.1-flash",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 150,
                    "temperature": 0.7,
                }).encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
            text = result["choices"][0]["message"]["content"].strip()
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
            logger.warning("TokenHarbor query expansion failed: %s", e)

    # Local fallback
    return _local_expand_query(query, n)


def expand_query_hyde(token_getter, query: str) -> list[str]:
    """P1: HyDE-style expansion — generate a hypothetical answer to the query.

    Tries TokenHarbor first; falls back to local flan-t5-small. Returns
    [original, hypothetical_answer] so vector recall can match the answer
    text against the corpus, not just the question text.
    Fail-closed: returns [query] on any error.
    """
    token = token_getter()
    if token:
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://tokenharbor.ai/v1/chat/completions",
                data=json.dumps({
                    "model": "deepseek-v4.1-flash",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 120,
                    "temperature": 0.3,
                }).encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
            text = result["choices"][0]["message"]["content"].strip()
            if text:
                return [query, text]
        except Exception as e:
            logger.warning("TokenHarbor HyDE expansion failed: %s", e)

    # Local fallback
    try:
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
        import torch

        model_name = "google/flan-t5-small"
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSeq2SeqLM.from_pretrained(model_name)

        prompt = (
            "Write a brief, factual hypothetical answer to the following question. "
            "Do not hedge or say 'I don't know'. Keep it to 2-3 sentences.\n\n"
            f"Question: {query}\n\n"
            "Hypothetical answer:"
        )
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


def expand_query_multihop(token_getter, query: str) -> list[str]:
    """P1: decompose a multi-hop question into sub-questions.

    Returns [original, sub1, sub2, ...] if the question looks multi-hop,
    otherwise [query].
    """
    if not re.search(r"\b(and|or|also|then|after|before|because|since|while)\b", query, re.IGNORECASE):
        return [query]

    token = token_getter()
    if not token:
        return [query]

    prompt = (
        "Decompose the following question into 2 simpler sub-questions that "
        "together would answer the original. Return ONLY a JSON array of strings.\n\n"
        f"Original: {query}\n\n"
        "Sub-questions:"
    )
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://tokenharbor.ai/v1/chat/completions",
            data=json.dumps({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 120,
                "temperature": 0.0,
            }).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        text = result["choices"][0]["message"]["content"].strip()
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            subs = json.loads(text[start:end])
            if isinstance(subs, list) and subs:
                return [query] + [s for s in subs if isinstance(s, str) and s.strip()]
    except Exception as e:
        logger.warning("Multi-hop expansion failed: %s", e)
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
