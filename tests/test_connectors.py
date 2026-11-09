"""W2.4 — connectors: a directory of files, and a page the operator names.

What is asserted here is mostly about *refusal* and *honest accounting*, because an importer that
half-works is worse than one that does not run:

* one unreadable file does not cost the batch, and it is reported as skipped with a reason;
* nothing is fetched from a `file://`, `javascript:` or relative URL;
* an oversized response is refused rather than swallowed;
* the fetch records the address the bytes came from (the final URL after redirects) and the time;
* a dry run reports what it would do and stores nothing.
"""

from __future__ import annotations

import io

import pytest

from aletheia.connectors import (
    MAX_FETCH_BYTES,
    FetchedPage,
    fetch_page,
    import_paths,
    ingest_fetched,
)
from aletheia.ingest import Ingestor
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


class _Response(io.BytesIO):
    """Minimal stand-in for an http response object."""

    def __init__(self, body: bytes, *, url: str, status: int = 200):
        super().__init__(body)
        self.url = url
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _opener(body: bytes, *, url: str = "https://example.test/page.html", status: int = 200):
    calls: list = []

    def open_url(request, timeout=None):
        calls.append((request.full_url, timeout))
        return _Response(body, url=url, status=status)

    open_url.calls = calls
    return open_url


# --------------------------------------------------------------------------- directory import


def test_a_directory_is_imported_file_by_file(sub, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("the lighthouse needs paint", encoding="utf-8")
    (folder / "b.md").write_text("the buoy is red", encoding="utf-8")

    report = import_paths(Ingestor(sub), ctx_for("alpha"), "alpha", [folder], author="operator")
    summary = report.summary()
    assert summary["stored"] == 2, summary
    assert {outcome["name"] for outcome in summary["outcomes"]} == {"a.txt", "b.md"}


def test_one_bad_file_does_not_cost_the_batch(sub, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "good.txt").write_text("readable", encoding="utf-8")
    (folder / "broken.pdf").write_bytes(b"not a pdf at all")
    (folder / "notes.xyz").write_text("unsupported", encoding="utf-8")

    summary = import_paths(Ingestor(sub), ctx_for("alpha"), "alpha", [folder]).summary()
    assert summary["stored"] == 1
    assert summary["skipped"] == 1, "the unsupported extension is reported, not guessed at"
    assert summary["refused"] == 1, "the unparseable PDF is refused with its reason"
    reasons = {o["name"]: o["detail"] for o in summary["outcomes"]}
    # The exact reason belongs to the parser, not to this module: what matters is that the batch
    # named the failure instead of dropping the file or absorbing it into a success count.
    assert reasons["broken.pdf"], "a refusal must carry the parser's reason"
    assert reasons["notes.xyz"].startswith("unsupported type")


def test_a_dry_run_import_stores_nothing(sub, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("the lighthouse needs paint", encoding="utf-8")

    summary = import_paths(Ingestor(sub), ctx_for("alpha"), "alpha", [folder], dry_run=True).summary()
    assert summary["dry_run"] is True and summary["stored"] == 0
    assert sub._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0


def test_a_missing_path_is_reported_not_raised(sub):
    summary = import_paths(Ingestor(sub), ctx_for("alpha"), "alpha", ["/nope/missing"]).summary()
    assert summary["skipped"] == 1
    assert summary["outcomes"][0]["detail"] == "no such file or directory"


def test_the_limit_is_reported_rather_than_silently_applied(sub, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    for index in range(3):
        (folder / f"{index}.txt").write_text(f"file {index}", encoding="utf-8")

    summary = import_paths(Ingestor(sub), ctx_for("alpha"), "alpha", [folder], limit=1).summary()
    assert summary["stored"] == 1
    assert any(o["name"] == "(limit)" for o in summary["outcomes"]), "the truncation is visible"


# --------------------------------------------------------------------------- fetching


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "/relative/path",
                                 "ftp://example.test/x", "data:text/plain,hello"])
def test_only_http_and_https_are_fetched(url):
    with pytest.raises(ValueError):
        fetch_page(url, opener=_opener(b"never"))


def test_a_fetch_records_the_final_url_and_the_time():
    opener = _opener(b"<html><body><p>hello</p></body></html>", url="https://example.test/final")
    page = fetch_page("https://example.test/start", opener=opener)
    assert page.url == "https://example.test/start"
    assert page.final_url == "https://example.test/final", "the address the bytes came from"
    assert page.fetched_at > 0
    assert "hello" in page.text
    assert "<p>" not in page.text, "markup does not survive"


def test_a_script_body_is_not_kept():
    opener = _opener(b"<html><script>var secret = 'x'</script><body>the buoy is red</body></html>")
    page = fetch_page("https://example.test/p", opener=opener)
    assert "secret" not in page.text
    assert "the buoy is red" in page.text


def test_an_oversized_response_is_refused():
    opener = _opener(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError):
        fetch_page("https://example.test/big", opener=opener, max_bytes=1024 * 1024)


def test_a_page_with_no_readable_text_is_refused():
    opener = _opener(b"<html><body>   </body></html>")
    with pytest.raises(ValueError):
        fetch_page("https://example.test/empty", opener=opener)


def test_the_cli_imports_a_directory(tmp_path, capsys):
    import json

    from aletheia import cli

    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("the lighthouse needs paint", encoding="utf-8")
    db = tmp_path / "cli.db"
    Substrate(db, ScopeEngine(known_scopes={"alpha"})).close()

    assert cli.main(["--db", str(db), "import", "--scope", "alpha", "--path", str(folder)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["stored"] == 1


def test_the_cli_refuses_to_fetch_without_the_egress_flag(tmp_path, capsys):
    import json

    from aletheia import cli

    db = tmp_path / "cli.db"
    Substrate(db, ScopeEngine(known_scopes={"alpha"})).close()

    assert cli.main(["--db", str(db), "fetch", "--scope", "alpha",
                     "--url", "https://example.test/p"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "refused"
    assert "--allow-egress" in printed["reason"]


def test_the_cli_refuses_a_non_http_url_even_with_the_flag(tmp_path, capsys):
    import json

    from aletheia import cli

    db = tmp_path / "cli.db"
    Substrate(db, ScopeEngine(known_scopes={"alpha"})).close()

    assert cli.main(["--db", str(db), "fetch", "--scope", "alpha",
                     "--url", "file:///etc/passwd", "--allow-egress"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


def test_the_default_size_cap_is_documented_and_finite():
    assert MAX_FETCH_BYTES == 5 * 1024 * 1024


def test_a_fetched_page_is_ingested_through_the_document_path(sub):
    page = FetchedPage(url="https://example.test/a", final_url="https://example.test/articles/tides",
                       status=200, text="The tide table is wrong in March.\n\nAsk the harbour office.",
                       fetched_at=1.0)
    record_id = ingest_fetched(Ingestor(sub), ctx_for("alpha"), "alpha", page)
    record = sub.get(record_id, ctx_for("alpha"))
    assert "tide table" in record.payload["text"]
    assert "articles/tides" in record.provenance["author"], "the source address is on the record"
    observations = sub._conn.execute(
        "SELECT COUNT(*) FROM records WHERE record_type='Observation' AND scope='alpha'").fetchone()[0]
    assert observations >= 1, "the document became citable observations"