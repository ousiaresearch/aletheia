"""Phase 12 — Bi-temporal validity windows + temporal extraction/boosting.

Records can carry valid_from / valid_to timestamps so that recall can
filter by "effective at time T" without mutating history. Corrections
and supersessions close the previous window automatically.

Temporal extraction: date expressions are extracted from record text and
stored as metadata. During recall, temporal queries get a boost from
records containing date expressions.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from aletheia.substrate import Record, Substrate
from aletheia.scope import ScopeContext


# ---------------------------------------------------------------------------
# Bi-temporal validity (original Phase 12)
# ---------------------------------------------------------------------------

def set_validity(record: Record, valid_from: float | None = None, valid_to: float | None = None) -> Record:
    """Return a copy of *record* with updated validity window."""
    return Record(
        record_id=record.record_id,
        record_type=record.record_type,
        scope=record.scope,
        payload=record.payload,
        epistemic_status=record.epistemic_status,
        created_at=record.created_at,
        effective_at=record.effective_at,
        supersedes=record.supersedes,
        source_ids=list(record.source_ids),
        provenance=dict(record.provenance),
        attribution=record.attribution,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def close_window(sub: Substrate, ctx: ScopeContext, record_id: str, close_at: float | None = None) -> bool:
    """Close the validity window of *record_id* at *close_at* (default now).

    Returns True if the record was found and updated, False otherwise.
    Fail-closed: returns False on any error.
    """
    if close_at is None:
        close_at = time.time()
    try:
        rec = sub.get(record_id, ctx)
        if rec is None:
            return False
        updated = set_validity(rec, valid_from=rec.valid_from, valid_to=close_at)
        sub.put(updated, ctx, overwrite=True)
        return True
    except Exception:
        return False


def effective_at(sub: Substrate, ctx: ScopeContext, scope: str, when: float) -> list[Record]:
    """Return all records in *scope* readable by *ctx* that were valid at timestamp *when*.

    A record is valid when:
      valid_from is None OR valid_from <= when
      AND
      valid_to is None OR valid_to > when
    The caller's scope grant and the substrate read gate decide what is returned: a caller with
    no grant for *scope* (or no context) reads nothing.
    Fail-closed: returns [] on any error.
    """
    try:
        if ctx is None or not sub.scope_engine.authorize_read(scope, ctx, "").allowed:
            return []
        readable, _ = sub.query(ctx, scope=scope)
        return [r for r in readable
                if (r.valid_from is None or r.valid_from <= when)
                and (r.valid_to is None or r.valid_to > when)]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Temporal extraction
# ---------------------------------------------------------------------------

# Patterns are tried in order; first match wins per expression.
_DATE_PATTERNS = [
    # Absolute: 8 May 2023, 8th May 2023
    (r"\b\d{1,2}(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b", "absolute"),
    # Absolute: May 8, 2023; May 2023
    (r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b", "absolute"),
    # ISO: 2023-05-08
    (r"\b\d{4}-\d{2}-\d{2}\b", "absolute"),
    # MM/DD/YYYY or M/D/YY
    (r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", "absolute"),
    # Relative + anchor: last Saturday, next month, last year, recently, two weeks ago
    (r"\b(last|next|this)\s+(week|month|year|tuesday|wednesday|thursday|friday|saturday|sunday|monday|weekend)\b", "relative"),
    (r"\b(recently|last week|last month|last year|this week|this month|this year)\b", "relative"),
    (r"\b\d+\s+(days?|weeks?|months?|years?)\s+ago\b", "relative"),
    # Relative + absolute anchor: the week before 9 June 2023, two weekends before 17 July 2023
    (r"\b(the\s+)?(week|month|year|weekend|friday|monday|tuesday|wednesday|thursday|saturday|sunday)\s+before\s+\d{1,2}\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b", "relative_anchor"),
    (r"\b(two|three|four|five)\s+(weekends?|weeks?|days?)\s+before\s+\d{1,2}\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b", "relative_anchor"),
    # Time of day: 1:56 pm, 12:48 am
    (r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", "time"),
]

_TEMPORAL_KEYWORDS = {
    "when", "date", "year", "month", "week", "day", "time",
    "how long", "how many", "ago", "recently", "before", "after",
    "temporal", "schedule", "period", "duration",
}


def extract_dates(text: str) -> list[dict[str, Any]]:
    """Return extracted date expressions from *text*.

    Each result: {"expr": matched_text, "kind": pattern_label, "start": idx}
    Fail-closed: returns [] on any error.
    """
    try:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for pattern, label in _DATE_PATTERNS:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                expr = m.group(0).strip()
                if expr.lower() not in seen:
                    seen.add(expr.lower())
                    results.append({"expr": expr, "kind": label, "start": m.start()})
        return sorted(results, key=lambda r: r["start"])
    except Exception:
        return []


def is_temporal_query(query: str) -> bool:
    """Heuristic: does *query* ask about time/date?"""
    q = query.lower()
    return any(kw in q for kw in _TEMPORAL_KEYWORDS)


def apply_temporal_metadata(record: Record) -> Record:
    """Return a copy of *record* with ``temporal_dates`` and ``is_temporal``
    added to payload if any date expressions were found.
    """
    text = ""
    if isinstance(record.payload, dict):
        text = record.payload.get("text", "") or ""
    dates = extract_dates(text)
    if not dates:
        return record
    payload = dict(record.payload)
    payload["temporal_dates"] = dates
    payload["is_temporal"] = True
    return Record(
        record_id=record.record_id,
        record_type=record.record_type,
        scope=record.scope,
        payload=payload,
        epistemic_status=record.epistemic_status,
        created_at=record.created_at,
        effective_at=record.effective_at,
        supersedes=record.supersedes,
        source_ids=list(record.source_ids),
        provenance=dict(record.provenance),
        attribution=record.attribution,
        valid_from=record.valid_from,
        valid_to=record.valid_to,
    )


def temporal_boost(
    sub: Substrate,
    ctx: ScopeContext,
    query: str,
    candidate_ids: list[str],
    boost: float = 0.15,
) -> dict[str, float]:
    """Boost candidate records that contain date expressions when the query
    is temporal. Returns {record_id: boost_score}.

    Uses precomputed ``is_temporal`` flag on payload to avoid re-parsing.
    Fail-closed: returns {} on any error.
    """
    try:
        if not is_temporal_query(query):
            return {}
        if ctx is None or not candidate_ids:
            return {}
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = sub._conn.execute(
            f"SELECT * FROM records WHERE record_id IN ({placeholders})",
            tuple(candidate_ids),
        ).fetchall()
        # Only records the caller may read take part: a score must not reveal that an
        # unreadable record exists or carries dates.
        rows = [(rec.record_id, json.dumps(rec.payload)) for rec in map(Record.from_row, rows)
                if sub.scope_engine.authorize_read(
                    rec.scope, ctx, sub.scope_engine._payload_text(rec)).allowed]
        scores: dict[str, float] = {}
        for record_id, payload_json in rows:
            try:
                payload = json.loads(payload_json) if payload_json else {}
            except Exception:
                continue
            if payload.get("is_temporal"):
                dates = payload.get("temporal_dates", [])
                # Scale boost by number of date expressions (capped)
                n = min(len(dates), 5)
                scores[record_id] = boost * (0.5 + 0.5 * n / 5)
        return scores
    except Exception:
        return {}
