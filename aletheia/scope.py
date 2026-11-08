"""Aletheia scope engine: deterministic, deny-by-default, fail-closed.

This is the component that prevents the ACS-class contamination failure. It runs
BEFORE any validation, ranking, or model call. A model may never widen scope.

Design rules (docs/architecture.md):
  1. Unknown or unstated scope -> deny.
  2. Exclusion happens before ranking, not after.
  3. Content determines scope. A record whose text names another scope's
     registered marker is mis-scoped and is refused for reads and writes.
  4. Derived artifacts (peer cards, summaries) are NOT injectable units.
  5. Cross-scope access requires a named target and user attribution.
  6. Ambiguous attribution -> fail closed.

Security boundary (decision, Oct 24, after three review rounds):
  The GRANTED-SCOPE checks are the security boundary. Read, write, and delete
  are decided by ScopeContext.granted_scopes and by explicit, attributed
  cross-scope requests. Content markers are a best-effort defence in depth that
  catches mis-filed content. They are not a guarantee, because no character
  table covers every lookalike. The residuals are recorded in
  docs/adr/0001-phase1-decisions.md.

Authority model (review fix, a37d175 F1-F6):
  - Reading needs a granted scope, a global allowlist entry, or a named,
    attributed cross-scope request.
  - Writing and deleting need a GRANTED scope only. The allowlist and
    cross-scope reads never confer write or delete authority.

This module is pure and synchronous. It does no I/O and makes no model calls.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# Advisory tagging only (never a decision by itself). Detects phrases that look
# like references to a project, organization, or body of work.
PROJECT_REFERENCE_PATTERNS = [
    re.compile(
        r"\b(?:the\s+)?[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*)*\s+"
        r"(?:Society|Commonwealth|Foundation|Consortium|Institute|Coalition|Guild|Order|"
        r"Project|Initiative|Program|Conservatory|Observatory|University|Church|Cabinet|"
        r"Council|Assembly|Committee|Bureau|Authority|Agency|Corporation|Company|Labs?)\b"
    ),
    re.compile(r"\b[A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,3}\s+"
               r"(?:Convention|Convention Letters|Accord|Treaty|Charter|Manifesto|"
               r"Constitution|Letters|Covenant|Docket|Attestation)\b"),
    re.compile(r"\bproject\s+[A-Z0-9][\w-]*\b", re.IGNORECASE),
    re.compile(r"\b(?:RC|Slice|Phase|Release)\s*[-–]?\s*0?\d+\b"),
    re.compile(r"\b[A-Z]{2,}(?:-[A-Za-z0-9]+)+\b"),
    re.compile(r"\b(?!(?:THE|AND|FOR|NOT|BUT|ALL|ANY|CAN|HAD|HER|WAS|ONE|OUR|OUT|DAY|GET|"
               r"HAS|HIM|HIS|HOW|MAN|NEW|NOW|OLD|SEE|TWO|WAY|WHO|BOY|DID|ITS|LET|PUT|SAY|"
               r"SHE|TOO|USE|YES|API|UI|UX|ID|OK|AI|ML|OS|PC|TV|UK|US|EU|AM|PM|ETC|VS|"
               r"HTTP|HTTPS|JSON|YAML|SQL|PDF|DOCX|CSV|TXT|MD|URL|URI|UUID|REST|CLI|GUI|"
               r"TUI|SDK|MCP|AGPL|MIT|LLM|GPU|CPU|RAM|SSD|OS)\b)[A-Z]{2,5}\b"),
]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


def _as_scope_set(value, name: str) -> frozenset[str]:
    """Normalise a scope collection. A bare string is rejected, never iterated.

    Review fix F6: `"acs-project" in "acs-project"` is a substring test, so a
    single string silently turned a scope grant into a prefix grant.
    """
    if value is None:
        return frozenset()
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{name} must be a set of scope names, not a single string")
    try:
        items = frozenset(value)
    except TypeError as exc:
        raise TypeError(f"{name} must be iterable of strings") from exc
    if not all(isinstance(s, str) and s for s in items):
        raise TypeError(f"{name} must contain only non-empty strings")
    return items


@dataclass(frozen=True)
class ScopeContext:
    """Who is asking, for what, and under what authorization.

    Frozen (review fix F5: authority fields cannot be mutated after
    construction). Construction validates types (review fix F6).

    Trust boundary: this library trusts the code that builds the context.
    Attribution of user-authorized cross-scope requests is enforced by the
    service boundary (Phase 6), not here.
    """

    granted_scopes: frozenset[str] = frozenset()
    user_authorized_cross_scope: bool = False
    cross_scope_targets: frozenset[str] = frozenset()
    authorization_source: str | None = None
    # Phase 2 (D2-7): a cross-scope request must come from a user-authored turn.
    user_authored: bool = False
    # Phase 2 (D2-9): the acting identity, used for verifier checks.
    actor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.user_authored, bool):
            raise TypeError("user_authored must be a bool")
        if self.actor is not None and not isinstance(self.actor, str):
            raise TypeError("actor must be a string or None")
        object.__setattr__(self, "granted_scopes", _as_scope_set(self.granted_scopes, "granted_scopes"))
        object.__setattr__(
            self, "cross_scope_targets", _as_scope_set(self.cross_scope_targets, "cross_scope_targets")
        )
        if not isinstance(self.user_authorized_cross_scope, bool):
            raise TypeError("user_authorized_cross_scope must be a bool")
        if self.authorization_source is not None and not isinstance(self.authorization_source, str):
            raise TypeError("authorization_source must be a string or None")


ALLOW = Decision(True, "in scope")
DENY = Decision(False, "out of scope")


def references_project(text: str) -> bool:
    """Advisory: True if the text looks like it references a project or body of work.

    Used for tagging and review only. It is never the basis of an allow or deny
    decision (review fix F3: the previous guard was a no-op).
    """
    if not text:
        return False
    return any(p.search(text) for p in PROJECT_REFERENCE_PATTERNS)


_WORD = re.compile(r"[^\W_]+")


# Common Cyrillic and Greek lookalikes folded to their Latin counterparts. This is
# a small, explicit table, not full Unicode confusables. Extend it with tests.
_CONFUSABLES = str.maketrans({
    "а": "a", "с": "c", "е": "e", "о": "o", "р": "p", "х": "x", "у": "y",
    "і": "i", "ј": "j", "ѕ": "s", "һ": "h", "ԁ": "d", "ɡ": "g", "ѵ": "v",
    "ν": "v", "ο": "o", "α": "a", "ι": "i", "κ": "k", "ρ": "p", "τ": "t",
    "ɑ": "a", "ς": "s", "σ": "s", "ϲ": "c", "ϳ": "j", "ѡ": "w", "ԛ": "q", "ɩ": "i",
})

# Characters with no visible content: combining marks (Mn, Mc, Me) and format
# characters (Cf) such as zero-width spaces and soft hyphens.
_INVISIBLE_CATEGORIES = frozenset({"Mn", "Mc", "Me", "Cf"})


def _normalise_tokens(text: str) -> list[str]:
    """Split text into lowercase word tokens after Unicode NFKC normalisation.

    Fullwidth and compatibility forms fold to ASCII, then common confusables fold
    to Latin. Punctuation, hyphens, and spaces separate tokens.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    visible = "".join(ch for ch in decomposed if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES)
    folded = visible.casefold().translate(_CONFUSABLES)
    return _WORD.findall(folded)


def _marker_hit(marker: str, text: str) -> bool:
    """True if the marker appears in text as a token run, with or without separators.

    'ACS', 'A.C.S.', 'A C S', and 'ＡＣＳ' all match. 'impacts' does not, because
    the match must cover whole tokens: a run of tokens whose joined form equals the
    marker's joined form, and whose boundaries fall on token edges.
    """
    marker_tokens = _normalise_tokens(marker)
    if not marker_tokens:
        return False
    compact = "".join(marker_tokens)
    tokens = _normalise_tokens(text)
    n = len(tokens)
    for i in range(n):
        acc = ""
        for j in range(i, n):
            acc += tokens[j]
            if len(acc) > len(compact):
                break
            if acc == compact:
                return True
    return False


def _flatten_payload(value: Any, out: list[str]) -> None:
    """Collect every string in a payload: keys, values, and nested containers.

    Review fix F-A: the previous flattener read only top-level strings and so was
    evadable by nesting. Non-string scalars are rendered as text.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            _flatten_payload(v, out)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _flatten_payload(item, out)
    elif value is None:
        return
    else:
        out.append(str(value))


class ScopeEngine:
    """Deterministic scope resolution. Deny by default, fail closed.

    `markers` maps a scope name to the phrases that belong to it, for example
    {"acs-project": {"ACS", "Listening Commonwealth"}}. Content naming a marker
    of scope S is S-bound: it may only be stored under S and read where S is
    eligible. Markers are user-curated configuration, not inferred.
    """

    def __init__(
        self,
        known_scopes: Iterable[str] | None = None,
        markers: Mapping[str, Iterable[str]] | None = None,
        global_allowlist: Iterable[str] | None = None,
    ):
        self.known_scopes: set[str] = set(known_scopes or ())
        self.markers: dict[str, frozenset[str]] = {
            scope: frozenset(ms) for scope, ms in (markers or {}).items()
        }
        # Configuration time only (review N9). There is no runtime mutator, and
        # the value is exposed read-only.
        allow = frozenset(global_allowlist or ())
        unknown = allow - self.known_scopes
        if unknown:
            raise ValueError(f"global allowlist names unknown scopes: {sorted(unknown)}")
        self._global_allowlist: frozenset[str] = allow

    @property
    def global_allowlist(self) -> frozenset[str]:
        return self._global_allowlist

    def register_scope(self, scope: str, markers: Iterable[str] = ()) -> None:
        self.known_scopes.add(scope)
        if markers:
            self.markers[scope] = frozenset(markers)

    # ---- content rule -------------------------------------------------

    def content_conflict(self, record_scope: str, text: str) -> str | None:
        """Return a reason if the text is bound to a scope other than record_scope."""
        if not text or not self.markers:
            return None
        owners = {s for s, ms in self.markers.items() if any(_marker_hit(m, text) for m in ms)}
        if not owners or record_scope in owners:
            return None
        # Review fix F-D: the reason names no other scope. A caller who cannot see
        # that scope must not learn that it exists or what it covers.
        return "content is bound to a different scope"

    # ---- reads --------------------------------------------------------

    def authorize_read(self, record_scope: str, ctx: ScopeContext | None, text: str = "") -> Decision:
        """May this record be consulted for this query?"""
        if ctx is None:
            return Decision(False, "no scope context")
        if record_scope not in self.known_scopes:
            return Decision(False, f"unknown scope {record_scope!r}")

        try:
            conflict = self.content_conflict(record_scope, text)
        except Exception:  # classifier fails closed (D2-4)
            return Decision(False, "read not permitted")
        if conflict is not None:
            return Decision(False, conflict)

        if record_scope in ctx.granted_scopes:
            return ALLOW
        if record_scope in self._global_allowlist:
            return ALLOW

        if ctx.user_authorized_cross_scope:
            if not ctx.user_authored:
                return Decision(False, "cross-scope requires a user-authored turn")
            if not ctx.cross_scope_targets:
                return Decision(False, "cross-scope request names no targets")
            if not ctx.authorization_source:
                return Decision(False, "cross-scope request has no attribution")
            if record_scope in ctx.cross_scope_targets:
                return Decision(True, "cross-scope, user-authorized")
            return Decision(False, "cross-scope target mismatch")

        return DENY

    # ---- writes and deletes (granted scope only) ----------------------

    def authorize_write(self, record, ctx: ScopeContext | None) -> Decision:
        """May this caller write this record?

        Review fix F1: writes take a context and need a granted scope. Being
        able to read a scope, or to see it in known_scopes, is not enough.
        """
        if ctx is None:
            return Decision(False, "no scope context")
        scope = getattr(record, "scope", None)
        if not scope:
            return Decision(False, "record has no scope")
        if scope not in self.known_scopes:
            return Decision(False, f"unknown scope {scope!r} -- denied, not created")
        if scope not in ctx.granted_scopes:
            return Decision(False, f"write not granted for scope {scope!r}")
        try:
            conflict = self.content_conflict(scope, self._payload_text(record))
        except Exception:  # classifier fails closed (D2-4)
            return Decision(False, "write not permitted")
        if conflict is not None:
            return Decision(False, conflict)
        return Decision(True, "write permitted")

    def authorize_delete(self, record_scope: str, ctx: ScopeContext | None) -> Decision:
        """May this caller delete a record in this scope?

        Deletion is a write. Only a granted scope authorizes it. The allowlist
        and cross-scope reads never do.
        """
        if ctx is None:
            return Decision(False, "no scope context")
        if record_scope not in self.known_scopes:
            return Decision(False, f"unknown scope {record_scope!r}")
        if record_scope in ctx.granted_scopes:
            return ALLOW
        return DENY

    @staticmethod
    def _payload_text(record) -> str:
        """Every string a reader sees, for content inspection (R1 closure, D2-5).

        Covers payload, provenance, source ids, and record type, not only payload.
        """
        parts: list[str] = []
        for name in ("payload", "provenance", "source_ids", "record_type"):
            _flatten_payload(getattr(record, name, None), parts)
        return " ".join(parts)

    def filter_readable(self, records, ctx: ScopeContext | None) -> list:
        """The records the caller may read, in order. Nothing else is returned.

        No exclusion reasons and no counts (review G6, G7). A withheld record is
        invisible to the caller, not described to it.
        """
        return [
            rec for rec in records
            if self.authorize_read(getattr(rec, "scope", ""), ctx, self._payload_text(rec)).allowed
        ]
