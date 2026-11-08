import json
import logging
import pytest
from aletheia.logging_config import setup_logging, log_event, RedactingFormatter


def test_redacting_formatter_strips_untrusted_tokens(capsys):
    setup_logging(level="INFO")
    logger = logging.getLogger("aletheia.test")
    logger.info("<UNTRUSTED_DOCUMENT>secret</UNTRUSTED_DOCUMENT> [REDACTED INJECTION]")
    out = capsys.readouterr().out
    assert "<UNTRUSTED_DOCUMENT>" not in out
    assert "</UNTRUSTED_DOCUMENT>" not in out
    assert "[REDACTED INJECTION]" not in out
    assert "[REDACTED]" in out


def test_redacting_formatter_case_insensitive_and_whitespace_variants(capsys):
    setup_logging(level="INFO")
    logger = logging.getLogger("aletheia.test")
    logger.info("<untrusted_document>secret</untrusted_document> <UNTRUSTED_DOCUMENT >secret2</UNTRUSTED_DOCUMENT > [redacted injection]")
    out = capsys.readouterr().out
    assert "UNTRUSTED_DOCUMENT" not in out
    assert "untrusted_document" not in out
    assert "REDACTED INJECTION" not in out
    assert "redacted injection" not in out
    assert out.count("[REDACTED]") == 5


def test_log_event_emits_json(capsys):
    setup_logging(level="INFO")
    log_event("test_op", record_id="r1", scope="s", detail="detail", level="INFO")
    out = capsys.readouterr().out.strip()
    entry = json.loads(out)
    assert entry["operation"] == "test_op"
    assert entry["record_id"] == "r1"
    assert entry["scope"] == "s"
    assert entry["detail"] == "detail"
