"""W2.4 — getting existing material in: a directory of files, and a page the operator names.

Two boundaries are drawn here rather than left to convention:

* **Nothing is fetched unless a command names the URL and passes the egress flag.** A memory
  substrate that reaches the network on its own is a substrate whose contents nobody can predict, so
  the fetch is a call an operator makes, it records the URL and the time it was fetched, and it
  refuses every scheme except http and https. A ``file://`` URL is a local read dressed as a fetch.
* **A batch import does not stop at the first bad file, and does not pretend a bad file worked.**
  Every input gets an outcome — ``stored``, ``refused`` with the reason the parser gave, or
  ``skipped`` — and the summary counts each. A run that silently drops what it could not parse is
  indistinguishable from a run that had nothing to do.

Retries are deliberately absent. Ingest is idempotent by content hash, so re-running the import is
the retry, and a half-written job that resumes from a cursor is a second source of state to keep
correct for no benefit at this size.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

#: Extensions the existing ingestion path can parse. Anything else is reported, not guessed at.
SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".pdf", ".docx"})

#: A page this large is not an article, and reading it into memory to find out is the problem.
MAX_FETCH_BYTES = 5 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 20.0
ALLOWED_SCHEMES = frozenset({"http", "https"})


@dataclass
class ImportOutcome:
    """One input, and what happened to it. ``detail`` never contains the file's text."""

    name: str
    status: str  # stored | refused | skipped
    detail: str = ""
    record_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "record_ids": list(self.record_ids)}


@dataclass
class ImportReport:
    outcomes: list[ImportOutcome] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> dict:
        counts = {"stored": 0, "refused": 0, "skipped": 0}
        for outcome in self.outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return {"files": len(self.outcomes), **counts, "dry_run": self.dry_run,
                "outcomes": [o.as_dict() for o in self.outcomes]}


def import_paths(ingestor, ctx, scope: str, paths, *, author: str = "", recursive: bool = False,
                 limit: int = 100, dry_run: bool = False) -> ImportReport:
    """Ingest every supported file under ``paths``; report each one.

    A directory is walked in sorted order so two runs describe the same corpus in the same order.
    Unreadable files and unsupported extensions are reported as ``skipped`` with a reason rather
    than raising: one unreadable PDF in a folder of forty should not cost the other thirty-nine.
    """
    report = ImportReport(dry_run=dry_run)
    collected: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            pattern = "**/*" if recursive else "*"
            collected.extend(sorted(p for p in path.glob(pattern) if p.is_file()))
        elif path.is_file():
            collected.append(path)
        else:
            report.outcomes.append(ImportOutcome(str(raw), "skipped", "no such file or directory"))

    for path in collected[:limit]:
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            report.outcomes.append(ImportOutcome(path.name, "skipped",
                                                 f"unsupported type {path.suffix or '(none)'}"))
            continue
        if dry_run:
            report.outcomes.append(ImportOutcome(path.name, "skipped", "dry run"))
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            report.outcomes.append(ImportOutcome(path.name, "skipped", f"unreadable: {exc.__class__.__name__}"))
            continue
        result = ingestor.ingest_document(path.name, data, ctx, scope, author or "operator")
        if isinstance(result, str):
            report.outcomes.append(ImportOutcome(path.name, "stored", "", (result,)))
        else:
            reason = getattr(result, "reason", None) or "refused"
            report.outcomes.append(ImportOutcome(path.name, "refused", str(reason)))
    if len(collected) > limit:
        report.outcomes.append(ImportOutcome("(limit)", "skipped",
                                             f"{len(collected) - limit} file(s) beyond --limit"))
    return report


@dataclass
class FetchedPage:
    url: str
    final_url: str
    status: int
    text: str
    fetched_at: float

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8", "replace"))


def _validate_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"refusing to fetch {parsed.scheme or 'a relative'} URL; only http and https")
    if not parsed.netloc:
        raise ValueError("refusing to fetch a URL with no host")
    return url


def fetch_page(url: str, *, timeout: float = FETCH_TIMEOUT_SECONDS,
               max_bytes: int = MAX_FETCH_BYTES, opener=None) -> FetchedPage:
    """Fetch one page the operator named. Raises for a refused URL or a failure — never silently.

    ``urllib`` and not a browsing stack: this is for a page whose address somebody typed, not for a
    site that needs script execution. Redirects are followed by urllib but the final URL is recorded,
    because "what I fetched" has to be the address the bytes came from.
    """
    _validate_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "aletheia-ingest/1.0"})
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)
        final_url = getattr(response, "url", url) or url
        status = getattr(response, "status", 0) or 0
    if len(raw) > max_bytes:
        raise ValueError(f"response exceeds {max_bytes} bytes; refusing to ingest it whole")
    text = _readable_text(raw)
    if not text.strip():
        raise ValueError("response had no readable text")
    return FetchedPage(url=url, final_url=final_url, status=status, text=text, fetched_at=time.time())


def _readable_text(raw: bytes) -> str:
    """Decode, and strip markup only if it looks like HTML.

    A dependency-free strip rather than a parser: the point is to remove tags, not to normalise a
    document. ``<script>`` and ``<style>`` bodies go first, because a page's inline JavaScript is the
    last thing anybody wants in their memory store.
    """
    import re

    body = raw.decode("utf-8", "replace")
    if "<html" not in body.lower() and "<body" not in body.lower():
        return body
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?s)<!--.*?-->", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    return re.sub(r"\n{2,}", "\n", body).strip()


def ingest_fetched(ingestor, ctx, scope: str, page: FetchedPage, *, author: str = "web") -> str:
    """Ingest a fetched page as a document named by its final URL.

    The provenance carries the address and the fetch time — through the ordinary document path,
    which already records the document name and content hash, so nothing here is a special case
    downstream.
    """
    name = page.final_url.rsplit("/", 1)[-1] or "page.html"
    if not os.path.splitext(name)[1]:
        name += ".txt"
    return ingestor.ingest_document(name, page.text.encode("utf-8"), ctx, scope,
                                    f"{author} ({page.final_url} fetched {int(page.fetched_at)})")