"""W2.6 slice 2 — redaction on the way in, so the raw value never reaches the store or the index.

The test that matters most is the index one: a redaction that happens after the FTS row is written
leaves the secret searchable while the record looks clean. Redaction runs before the record exists,
so this is asserted by searching for the raw value and finding nothing.

Also asserted: the record says *that* something was removed and never *what*, redaction is
idempotent, and a number that merely looks long is left alone (over-redaction of a corpus makes it
useless, which is a different failure from a leak and should not be hidden behind one).
"""

from __future__ import annotations

import pytest

from aletheia.ingest import Ingestor
from aletheia.redaction import PATTERNS, redact, scan
from aletheia.scope import ScopeContext, ScopeEngine
from aletheia.substrate import Substrate


@pytest.fixture
def engine():
    return ScopeEngine(known_scopes={"alpha"})


@pytest.fixture
def sub(tmp_path, engine):
    substrate = Substrate(tmp_path / "aletheia.db", scope_engine=engine)
    yield substrate
    substrate.close()


def ctx_for(*scopes: str) -> ScopeContext:
    return ScopeContext(granted_scopes=frozenset(scopes), user_authored=True)


# --------------------------------------------------------------------------- shapes


@pytest.mark.parametrize("text,kind", [
    ("write to melanie.wren@example.com about it", "email"),
    ("call +1 (555) 010-4477 tomorrow", "phone"),
    ("call 555-010-4477 later", "phone"),
    ("card 4111 1111 1111 1111 expires soon", "card"),
    ("ssn 123-45-6789 on file", "ssn"),
    ("token sk-abcdef0123456789abcdef0123456789 leaked", "credential"),
    ("push with ghp_abcdefghijklmnopqrstuvwxyz0123", "credential"),
])
def test_shaped_secrets_are_redacted(text, kind):
    result = redact(text)
    assert result.findings.get(kind) == 1, result.findings
    assert f"[redacted:{kind}]" in result.text
    assert "[redacted:" in result.text


@pytest.mark.parametrize("text", [
    "the year 2024 and 42 boats and order 1234567890123",
    "version 2.1.0 shipped in 2026",
    "the lighthouse needs paint",
])
def test_ordinary_numbers_and_text_are_untouched(text):
    assert redact(text).text == text
    assert redact(text).findings == {}


def test_an_invalid_card_number_is_left_alone():
    """Luhn is what stops a 16-digit serial from being read as a card."""
    text = "serial 4111 1111 1111 1112 is not a card"
    assert redact(text).findings.get("card") is None


def test_a_grouped_serial_can_still_read_as_a_phone_and_that_is_documented():
    """The honest limitation: a 4-3-4 grouped run is shaped like a phone number.

    Over-redaction is the accepted failure here — a corpus that is slightly less readable is a
    smaller harm than a telephone number left in an index — but it is a real cost and it is asserted
    so nobody later mistakes it for a bug.
    """
    result = redact("serial 4111-1111-1111-1112 not a card")
    assert result.findings.get("phone") == 1


def test_findings_are_counts_never_values():
    result = redact("mail a@b.co and c@d.co, call 555-010-4477")
    assert result.findings == {"email": 2, "phone": 1}
    assert "a@b.co" not in str(result.findings)
    assert scan("a@b.co") == {"email": 1}


def test_redaction_is_idempotent():
    once = redact("mail melanie.wren@example.com")
    twice = redact(once.text)
    assert twice.text == once.text
    assert twice.findings == {}


def test_every_pattern_kind_is_reachable():
    kinds = {kind for kind, _ in PATTERNS}
    assert kinds == {"ssn", "card", "credential", "email", "phone"}


# --------------------------------------------------------------------------- ingestion


def test_a_turn_is_stored_redacted_and_the_index_never_sees_the_raw_value(sub):
    ctx = ctx_for("alpha")
    ingestor = Ingestor(sub)
    record_id = ingestor.ingest_turn("my email is melanie.wren@example.com, write soon",
                                     "melanie", ctx, "alpha", "conv-1", attribution="user")
    record = sub.get(record_id, ctx)

    assert "melanie.wren@example.com" not in record.payload["text"]
    assert "[redacted:email]" in record.payload["text"]
    assert record.provenance["redacted"] == {"email": 1}, "the record says something was removed"

    # The index is built from the stored text, so no token of the raw address is searchable — while
    # the marker is, which proves the row exists rather than the text having vanished.
    def indexed(term: str) -> list[str]:
        return [row[0] for row in sub._conn.execute(
            "SELECT record_id FROM fts WHERE fts MATCH ?", (term,)).fetchall()]

    assert indexed("wren") == [], "the raw address left a token in the index"
    assert indexed("melanie") == []
    assert indexed("redacted") == [record_id]


def test_the_cli_reports_stored_secrets_without_rewriting_them(tmp_path, capsys):
    """Records already stored cannot be un-leaked by a rewrite, and the report says so."""
    import json

    from aletheia import cli
    from aletheia.substrate import Record

    db = tmp_path / "cli.db"
    engine = ScopeEngine(known_scopes={"alpha"})
    sub = Substrate(db, engine)
    ctx = ctx_for("alpha")
    try:
        # Written directly, i.e. before any ingestion hook existed.
        sub.put(Record(record_id="old", record_type="Claim", scope="alpha",
                       epistemic_status="derived",
                       payload={"text": "call 555-010-4477 or mail a@b.co", "confidence": 0.9},
                       attribution="agent"), ctx)
    finally:
        sub.close()

    assert cli.main(["--db", str(db), "redact", "--scope", "alpha"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["findings"] == {"phone": 1, "email": 1}
    assert report["record_ids"] == ["old"]

    reopened = Substrate(db, engine)
    try:
        stored = reopened.get("old", ctx)
        assert "555-010-4477" in stored.payload["text"], "the record was not rewritten"
    finally:
        reopened.close()


def test_a_clean_turn_carries_no_redaction_marker(sub):
    record_id = Ingestor(sub).ingest_turn("the lighthouse needs paint", "melanie",
                                          ctx_for("alpha"), "alpha", "conv-1", attribution="user")
    record = sub.get(record_id, ctx_for("alpha"))
    assert "redacted" not in record.provenance
    assert "[redacted:" not in record.payload["text"]


def test_a_document_is_redacted_in_the_source_and_in_its_observations(sub):
    ctx = ctx_for("alpha")
    ingestor = Ingestor(sub)
    body = ("Contact melanie.wren@example.com about the mooring.\n\n"
            "The card 4111 1111 1111 1111 was declined.\n")
    result = ingestor.ingest_document("notes.txt", body.encode("utf-8"), ctx, "alpha", "melanie")
    source_id = result if isinstance(result, str) else getattr(result, "record_id", result)

    rows = sub._conn.execute(
        "SELECT record_id, payload, provenance FROM records WHERE scope='alpha'").fetchall()
    assert rows, "the document was stored"
    for _record_id, payload, _provenance in rows:
        assert "melanie.wren@example.com" not in payload
        assert "4111 1111 1111 1111" not in payload
    assert source_id