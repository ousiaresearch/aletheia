"""Phase 11 — Background extraction / consolidation.

Optional module: import aletheia.extraction only when extraction is enabled.
Extracts discrete facts from raw Observations as Claims, with deduplication
and importance scoring. External model calls use the operator-selected provider
and send scope text off-host, so they are refused unless the operator opts in
explicitly (ALETHEIA_EXTRACT_ALLOW_EXTERNAL=1 or `aletheia extract --allow-external`).
The provider key is read only from the configured environment variable.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aletheia.knowledge import RULES, check_record
from aletheia.substrate import Record, Substrate
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.llm_client import complete_text, destination_host as _llm_destination_host, get_config


def destination_host() -> str:
    """Return the host selected by the operator's LLM provider configuration."""
    return _llm_destination_host()


def external_allowed() -> bool:
    """Explicit operator opt-in for sending scope text to the selected endpoint."""
    return os.environ.get("ALETHEIA_EXTRACT_ALLOW_EXTERNAL", "") == "1"


def _extract_facts(text: str, scope: str) -> list[dict[str, Any]]:
    """Extract factual claims with the explicitly selected provider; fail closed."""
    if not external_allowed():
        return []

    prompt = f"""Extract discrete factual claims from the following observation text.
Each claim must be a simple (subject, predicate, object) triple.

Rules:
- Extract ALL factual claims that are explicitly stated or directly implied.
- Prefer specific named entities over pronouns (resolve "he/she/they" to names if clear).
- Use concise, specific predicates (e.g., "painted", "attended", "lived in", "earned", "married").
- If the text mentions a date, location, quantity, or affiliation, capture it as a separate claim.
- Do not merge unrelated facts into one claim.
- Assign fact_type: event, attribute, relation, location, temporal, or identity.
- Assign confidence: 0.95-1.0 for explicit statements, 0.7-0.94 for direct implications, 0.3-0.69 for inferred but uncertain.
- If context is ambiguous, prefer lower confidence rather than skipping.

Return ONLY a JSON array of objects with these fields:
- subject: entity the claim is about (specific name, not "the user")
- predicate: relationship or property (concise verb phrase)
- object: value or target
- fact_type: one of [event, attribute, relation, location, temporal, identity]
- confidence: 0.0-1.0

Observation: {text[:3000]}

Return empty array [] if no extractable facts."""
    try:
        if get_config() is None:
            return []
        text_out = complete_text(prompt, max_tokens=1200, temperature=0.0)
        if not text_out:
            return []
        start = text_out.find("[")
        end = text_out.rfind("]") + 1
        if start >= 0 and end > start:
            facts = json.loads(text_out[start:end])
            if isinstance(facts, list):
                return [f for f in facts if isinstance(f, dict) and f.get("subject") and f.get("predicate")]
    except Exception:
        pass
    return []


def _fact_to_claim(fact: dict[str, Any], source_id: str, scope: str, index: int) -> Record:
    """Convert extracted fact to a Claim record."""
    claim_text = f"{fact['subject']} {fact['predicate']} {fact['object']}"
    return Record(
        record_id=f"extracted-{hashlib.sha256(claim_text.encode()).hexdigest()[:12]}-{index}",
        record_type="Claim",
        scope=scope,
        payload={
            "text": claim_text,
            "subject": fact["subject"],
            "predicate": fact["predicate"],
            "object": fact["object"],
            "fact_type": fact.get("fact_type", "attribute"),
            "confidence": fact.get("confidence", 0.5),
        },
        epistemic_status="derived",
        attribution="agent",
        source_ids=[source_id],
        provenance={"extracted": True, "extracted_at": time.time()},
    )


def extract_facts(sub: Substrate, ctx: ScopeContext, scope: str, limit: int = 100, window_size: int = 1) -> list[Record]:
    """Extract facts from Observations and Sources in a scope.

    Returns list of new Claim records. Does not write to substrate.
    Fail-closed: returns [] on any error.
    """
    if ctx is None or not external_allowed():
        return []
    try:
        # Read only through the substrate's gate: grant and content markers apply, so
        # nothing the caller cannot read ever reaches the external endpoint.
        readable, _ = sub.query(ctx, scope=scope)
        records = [
            (r.record_id, json.dumps(r.payload), r.created_at)
            for r in readable if r.record_type in ("Observation", "Source")
        ]
    except Exception:
        return []
    
    # P1: group turn-derived records by conversation_id so we can extract facts
    # from windows of adjacent turns rather than isolated lines.
    turn_windows: list[list[tuple[str, dict, float]]] = []
    standalone: list[tuple[str, dict, float]] = []
    conv_groups: dict[str, list[tuple[str, dict, float]]] = {}
    for obs_id, payload_json, created_at in records[:limit]:
        try:
            payload = json.loads(payload_json)
            conv_id = (payload.get("provenance") or {}).get("conversation_id")
            item = (obs_id, payload, created_at)
            if conv_id and window_size > 1:
                conv_groups.setdefault(conv_id, []).append(item)
            else:
                standalone.append(item)
        except Exception:
            standalone.append((obs_id, {"text": ""}, created_at))
    
    for conv_id, items in conv_groups.items():
        items.sort(key=lambda x: x[2] or 0)
        for i in range(0, len(items), window_size):
            turn_windows.append(items[i:i + window_size])
    
    # Process windows first, then standalone records.
    work_items = turn_windows + [[item] for item in standalone]
    if len(work_items) > limit:
        work_items = work_items[:limit]
    
    claims: list[Record] = []
    seen_hashes: set[str] = set()
    
    for window in work_items:
        try:
            window_texts = []
            window_source_ids = []
            for obs_id, payload, created_at in window:
                text = payload.get("text", "")
                if not text:
                    continue
                window_source_ids.append(obs_id)
                
                # P1: prepend context header so extraction LLM grounds facts in
                # speaker, timestamp, and scope rather than isolated text.
                header_parts = [f"[Record ID: {obs_id}"]
                if created_at:
                    try:
                        header_parts.append(f"| Created: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(created_at))}")
                    except Exception:
                        pass
                speaker = (payload.get("provenance") or {}).get("speaker")
                if speaker:
                    header_parts.append(f"| Speaker: {speaker}")
                header_parts.append(f"| Scope: {scope}]")
                context_header = " ".join(header_parts) + "\n\n"
                window_texts.append(context_header + text)
            
            if not window_texts:
                continue
            
            batch_text = "\n\n---\n\n".join(window_texts)
            facts = _extract_facts(batch_text, scope)
            
            for i, fact in enumerate(facts):
                # Attribute each extracted fact to the first source in the window
                # that contains the predicate or object string as a rough heuristic.
                src_id = window_source_ids[0]
                for wid, wtext in zip(window_source_ids, window_texts):
                    pred = fact.get("predicate", "").lower()
                    obj = fact.get("object", "").lower()
                    if pred and pred in wtext.lower():
                        src_id = wid
                        break
                    if obj and obj in wtext.lower():
                        src_id = wid
                        break
                
                claim = _fact_to_claim(fact, src_id, scope, i)
                claim_hash = claim.payload["text"].lower().strip()
                
                if claim_hash in seen_hashes:
                    continue
                seen_hashes.add(claim_hash)
                
                # Validate against knowledge rules
                reason = check_record(claim, ctx, is_verifier=lambda a: False, lookup=sub._lookup_record)
                if reason is None:
                    claims.append(claim)
        except Exception:
            continue
    
    return claims


def write_claims(sub: Substrate, ctx: ScopeContext, claims: list[Record], embedder=None) -> int:
    """Write extracted claims to substrate. Returns count written.
    
    If embedder is provided, also populates the vector index for new claims
    so that vector_index_complete remains true after extraction.
    """
    written = 0
    for claim in claims:
        try:
            reason = check_record(claim, ctx, is_verifier=lambda a: False, lookup=sub._lookup_record)
            if reason is None:
                sub.put(claim, ctx)
                written += 1
                if embedder is not None:
                    try:
                        text = claim.payload.get("text", "") if isinstance(claim.payload, dict) else str(claim.payload)
                        if text:
                            vector = embedder.embed_document(text)
                            sub.vector_put(claim.record_id, claim.scope, vector, ctx, model_id=embedder.model_id)
                    except Exception:
                        pass
        except Exception:
            continue
    return written


def consolidate_scope(sub: Substrate, ctx: ScopeContext, scope: str, similarity_threshold: float = 0.95,
                      *, mark: bool = False, dry_run: bool = False, embedder=None) -> dict:
    """Report near-duplicate Claims in a scope, and optionally mark them with ``duplicate_of`` edges.

    Delegates to :mod:`aletheia.consolidation`, which owns the clustering and the threshold. Two
    things stay true here: nothing is deleted, and nothing is superseded — a Correction needs user
    attribution, so an agent may mark a duplicate and may not rewrite what somebody asserted.

    Fail-closed: an error returns an empty report rather than a partial one.
    """
    from aletheia.consolidation import mark_duplicates

    if ctx is None:
        return {}
    try:
        report = mark_duplicates(sub, ctx, scope, threshold=similarity_threshold,
                                 record_type="Claim", dry_run=not mark, embedder=embedder)
    except Exception:
        return {}
    return {
        "claims_reviewed": report["records_scanned"],
        "duplicate_count": report["duplicates"],
        "kept": report["groups"],
        "edges_written": report["edges_written"],
        "dry_run": report["dry_run"],
    }
