"""Redaction on the way in: the raw value never reaches the store, so it never reaches the index.

Two decisions:

* **Redaction happens before the write, not as a sweep afterwards.** A record is supposed to be
  immutable, so "redact a stored record" means either rewriting history or living with the leak. The
  hook therefore sits in ingestion: the record is created from redacted text, and the FTS row and the
  vector are computed from what is actually stored. Nothing downstream has to remember.
* **The record says it was redacted; it never says what was removed.** Findings are counts by kind
  (`{"email": 1}`), never the matched value — a redaction log that quotes the secret is a second copy
  of the secret.

The patterns are deliberately conservative and the credit-card rule requires a valid Luhn checksum:
over-redacting a document makes it useless, and a corpus nobody can read is not a privacy win. What
this does not do is detect names, addresses or anything without a shape — that needs a model, and a
model that runs on write must be opt-in, local and slow rather than silent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Ordered: the specific shapes run before the loose one, so a card is a card rather than a phone
#: number and an SSN is an SSN. Once a kind has been replaced, the marker contains no digits, so the
#: later patterns cannot re-match the same span.
#:
#: Each pattern is deliberately narrow. A telephone pattern that matches any run of digits redacts
#: order numbers and years, and a corpus nobody can read is not a privacy win.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ \-]?){12,18}\d\b")),
    ("credential", re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                              r"AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9\-]{10,})\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    # A phone number only when it is shaped like one: a country code, a parenthesised area code, or
    # two separator groups. A bare run of digits is left alone on purpose.
    ("phone", re.compile(r"(?:\+\d{1,3}[\s.\-]?\(?\d{1,4}\)?[\s.\-]?\d{3,4}[\s.\-]?\d{3,4}"
                         r"|\(\d{2,4}\)[\s.\-]?\d{3,4}[\s.\-]?\d{3,4}"
                         r"|\b\d{3,4}[\s.\-]\d{3,4}[\s.\-]\d{3,4}\b)")),
)

#: Kinds that need a checksum before they are believed.
_LUHN_KINDS = frozenset({"card"})


def _luhn(digits: str) -> bool:
    total = 0
    for index, character in enumerate(reversed(digits)):
        value = int(character)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


@dataclass
class Redaction:
    """What a piece of text became, and how many of each kind were removed."""

    text: str
    findings: dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.findings)

    @property
    def total(self) -> int:
        return sum(self.findings.values())


def redact(text: str, *, kinds: frozenset[str] | None = None) -> Redaction:
    """Replace shaped secrets with ``[redacted:<kind>]``, keeping everything else exactly as written."""
    if not text:
        return Redaction(text=text)
    selected = {kind for kind, _ in PATTERNS} if kinds is None else set(kinds)
    findings: dict[str, int] = {}
    result = text

    for kind, pattern in PATTERNS:
        if kind not in selected:
            continue

        def replace(match: re.Match[str], kind: str = kind) -> str:
            value = match.group(0)
            if kind in _LUHN_KINDS and not _luhn(re.sub(r"\D", "", value)):
                return value
            findings[kind] = findings.get(kind, 0) + 1
            return f"[redacted:{kind}]"

        result = pattern.sub(replace, result)

    return Redaction(text=result, findings=findings)


def scan(text: str) -> dict[str, int]:
    """Count what would be redacted, without producing the redacted text."""
    return redact(text).findings