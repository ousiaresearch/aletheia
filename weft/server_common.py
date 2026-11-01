"""Shared helpers for Weft server modules."""

from __future__ import annotations

import os


def known_scopes() -> frozenset[str] | None:
    raw = os.environ.get("WEFT_KNOWN_SCOPES")
    if raw is None:
        return None  # unset: fail closed
    scopes = frozenset(s.strip() for s in raw.split(",") if s.strip())
    if not scopes:
        return None  # empty: fail closed
    return scopes
