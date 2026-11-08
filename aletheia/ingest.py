"""Phase 3 ingestion (spec §1, D3-3..D3-9).

Every entry point returns either a record id or a Rejection with a named reason.
Nothing is recorded as a success when extraction fails (D3-9).
"""

from __future__ import annotations

import hashlib
import json
import io
import os
import re
import uuid
import zipfile
from dataclasses import dataclass
from typing import Optional

from aletheia.substrate import Record, RecordConflict, ScopeViolation

def _centroid(vectors: list) -> list:
    """Normalised mean of the given vectors (note-level representation, no extra model calls)."""
    dim = len(vectors[0])
    mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    norm = sum(x * x for x in mean) ** 0.5 or 1.0
    return [x / norm for x in mean]


DOC_HEAD_CHARS = 2000  # opening text embedded per document when doc_vectors is on
# The role a turn is recorded as (spec §7.1). A role is not a name: hosts name the human side after
# whoever spoke — an iMessage handle, a display name — and the agent side after the provider. Those
# names are provenance, not attribution, so they are carried verbatim in ``provenance`` and never
# decide whether the turn can be stored.
_TURN_ATTRIBUTION = {"user": "user", "assistant": "agent", "agent": "agent", "system": "system"}
_TURN_ROLES = frozenset({"user", "agent", "system"})
_TURN_ROLE_ALIASES = {"assistant": "agent"}  # the role name a host uses, the attribution Aletheia stores
# Ambiguity is never the human's voice: ``user`` is authority-bearing (D2-2), so an unrecognised
# speaker falls to the agent side, and a caller that knows better passes ``attribution``.
TURN_ROLE_FALLBACK = "agent"
_STRAY = re.compile(r"</?aletheia-(?:recall|memory)\b[^>]*>", re.IGNORECASE)
_OPEN = re.compile(r"<aletheia-(?:recall|memory)\b[^>]*>", re.IGNORECASE)
_CLOSE = re.compile(r"</aletheia-(?:recall|memory)\s*>", re.IGNORECASE)


@dataclass(frozen=True)
class Rejection:
    name: str
    reason: str


def fence(text: str) -> tuple[str, bool]:
    """Strip recall blocks and stray recall tags (D3-5). Returns (text, changed).

    Blocks are removed until none remain (review G-P3-8: nested or repeated blocks
    must not leave recalled content behind), then any stray tag token is removed.
    """
    cleaned = text
    while True:
        opened = _OPEN.search(cleaned)
        if opened is None:
            break
        closes = list(_CLOSE.finditer(cleaned, opened.end()))
        if not closes:
            break
        # Conservative: everything from the first open tag through the last close tag goes.
        cleaned = cleaned[:opened.start()] + cleaned[closes[-1].end():]
    cleaned = _STRAY.sub("", cleaned)
    return cleaned, cleaned != text


def _extract_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_digest(record: Record) -> str:
    row = list(record.to_row())
    del row[5]  # creation time is not part of the intended content
    return _digest(json.dumps(row, separators=(',', ':')).encode())


def _parse(name: str, data: bytes):
    """Return extracted text, or a Rejection. Never returns partial text on failure."""
    ext = os.path.splitext(name.lower())[1]
    if ext in (".txt", ".md"):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return Rejection(name, "encoding_unsupported")
    if ext == ".pdf":
        if not data.startswith(b"%PDF"):
            return Rejection(name, "parse_failed")
        if b"/Encrypt" in data:
            return Rejection(name, "encrypted_pdf")
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                return Rejection(name, "encrypted_pdf")
            text = "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
        except Exception:
            return Rejection(name, "parse_failed")
        return text if text else Rejection(name, "no_text_layer")
    if ext == ".docx":
        try:
            import docx
            doc = docx.Document(io.BytesIO(data))
            text = "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception:
            return Rejection(name, "parse_failed")
        return text if text else Rejection(name, "no_text_layer")
    return Rejection(name, "unsupported_format")


def turn_attribution(speaker: str, role: Optional[str] = None) -> str:
    """The attribution a turn is stored with: the caller's role, else the speaker's own role name.

    An unrecognised speaker is not a reason to lose a turn. Hermes names the human half after the
    identity that spoke (an iMessage handle, a display name) and the agent half after the provider;
    refusing those made every turn the provider tried to store fail. The role is what the store
    reasons about, and the speaker name is kept verbatim in provenance either way.

    Ambiguity resolves *downward*: ``user`` attribution is authority-bearing (D2-2), so a name the
    store does not recognise is recorded as the agent side. A caller that knows the turn is the
    human's passes ``role="user"``.
    """
    if role is not None:
        wanted = str(role).strip().lower()
        wanted = _TURN_ROLE_ALIASES.get(wanted, wanted)
        if wanted not in _TURN_ROLES:
            raise ValueError(f"unknown attribution: {role!r} (expected one of {sorted(_TURN_ROLES)})")
        return wanted
    return _TURN_ATTRIBUTION.get(str(speaker or "").strip().lower(), TURN_ROLE_FALLBACK)


class Ingestor:
    """Writes Sources and Observations through the substrate. Holds no state of its own."""

    def __init__(self, substrate, embedder=None, doc_vectors=False, doc_centroid=True):
        self.sub = substrate
        self.embedder = embedder  # optional: anything with embed_document(text) -> vector
        self.doc_vectors = doc_vectors  # also embed each document's opening text (note-level)
        self.doc_centroid = doc_centroid  # note-level vector: normalised mean of paragraph vectors (default on)

    def _put_matching(self, record, ctx):
        """Recover only an identical, currently readable and writable incumbent."""
        try:
            self.sub.put(record, ctx)
        except RecordConflict:
            incumbent = self.sub._lookup_record(record.record_id)
            engine = self.sub.scope_engine
            if (incumbent is None
                    or not engine.authorize_write(incumbent, ctx).allowed
                    or not engine.authorize_read(incumbent.scope, ctx, engine._payload_text(incumbent)).allowed
                    or any(getattr(incumbent, field) != getattr(record, field) for field in (
                        'record_id', 'record_type', 'scope', 'payload', 'epistemic_status',
                        'effective_at', 'supersedes', 'source_ids', 'provenance', 'attribution'))):
                raise

    def ingest_turn(self, text: str, speaker: str, ctx, scope: str, conversation_id: str,
                    attribution: Optional[str] = None) -> str:
        """A conversation turn becomes a Source with the turn's attribution (spec §7.1).

        ``speaker`` is the identity that produced the turn and is kept verbatim in provenance;
        ``attribution`` is the role the store reasons about (``user`` / ``agent`` / ``system``).
        Callers that know which half of a turn this is should pass it explicitly — the speaker name
        alone cannot tell an iMessage handle from a provider name.
        """
        role = turn_attribution(speaker, attribution)
        clean, changed = fence(text)
        provenance = {"conversation_id": conversation_id, "speaker": speaker}
        if changed:
            # D3-5: text that arrived carrying recall fences is tagged, and tagged content is never
            # citable as the user's own speech. The flag alone is not enough — the attribution is
            # what recall and extraction reason about.
            provenance["fenced"] = True
            role = "system"
        rid = "turn-" + uuid.uuid4().hex[:24]
        record = Record(
            record_id=rid, record_type="Source", scope=scope, payload={"text": clean},
            attribution=role, source_ids=[], provenance=provenance,
        )
        if not self.sub.scope_engine.authorize_write(record, ctx).allowed:
            raise ScopeViolation("write not permitted", audit_recorded=False)
        vector = self.embedder.embed_document(clean) if self.embedder is not None else None
        with self.sub.ingestion_transaction():
            self.sub.put(record, ctx)
            if self.embedder is not None:
                self.sub.vector_put(rid, scope, vector, ctx=ctx, model_id=self.embedder.model_id)
        return rid

    def ingest_document(self, name: str, data: bytes, ctx, scope: str, author: str):
        """Document text is evidence: Observations carry attribution `document` (D3-3)."""
        parsed = _parse(name, data)
        if isinstance(parsed, Rejection):
            return parsed
        parsed, _ = fence(parsed)  # review G-P3-4: recall blocks never persist from documents
        if not parsed.strip():
            return Rejection(name, "empty_document")
        content_hash = _digest(data)
        sid = "doc-" + hashlib.sha256(f"{scope}\0{content_hash}".encode()).hexdigest()[:24]
        paragraphs = _extract_paragraphs(parsed)
        source = Record(
            record_id=sid, record_type="Source", scope=scope,
            payload={"text": parsed, "name": name}, attribution="document", source_ids=[],
            provenance={"author": author, "document": name, "content_hash": content_hash},
        )
        observations = [Record(
            record_id=f"{sid}-p{i:04d}", record_type="Observation", scope=scope,
            payload={"text": paragraph}, attribution="document", source_ids=[sid],
            provenance={"location": f"paragraph {i + 1}", "document": name},
        ) for i, paragraph in enumerate(paragraphs)]
        if any(not self.sub.scope_engine.authorize_write(record, ctx).allowed
               for record in [source, *observations]):
            raise ScopeViolation("write not permitted", audit_recorded=False)
        state = self.sub.job_state(sid, "ingest", content_hash, scope=scope, ctx=ctx)
        if state == "done":
            with self.sub.ingestion_transaction():
                # A completed bundle with missing members may reflect a deletion.
                # Never repair it implicitly by recreating those records on retry.
                if any(self.sub._lookup_record(record.record_id) is None
                       for record in [source, *observations]):
                    raise RecordConflict('completed ingestion is incomplete')
                self._put_matching(source, ctx)
                for observation in observations:
                    self._put_matching(observation, ctx)
                if self.embedder is None:
                    return sid
                # Existing vector rows carry no generation-configuration evidence.
                # Recompute rather than treating their presence as a matching mode.

        # Compute every embedding before writing the job, Source, or Observations. Model
        # load/inference failures therefore cannot leave an apparently successful partial import.
        paragraph_vectors = []
        source_vector = None
        if self.embedder is not None:
            paragraph_vectors = [self.embedder.embed_document(paragraph) for paragraph in paragraphs]
            if self.doc_vectors:
                source_vector = self.embedder.embed_document(parsed[:DOC_HEAD_CHARS])
            if self.doc_centroid and paragraph_vectors:
                source_vector = _centroid(paragraph_vectors)

        with self.sub.ingestion_transaction():
            # Read completion under the write transaction: another ingestion may
            # have completed and a member been deleted while embeddings ran.
            current_state = self.sub.job_state(sid, "ingest", content_hash, scope=scope, ctx=ctx)
            if (state == "done" or current_state == "done") and any(
                    self.sub._lookup_record(record.record_id) is None
                    for record in [source, *observations]):
                raise RecordConflict('completed ingestion is incomplete')
            self.sub.set_job(sid, "ingest", content_hash, "running", scope=scope, ctx=ctx)
            self._put_matching(source, ctx)
            # A preexisting source vector would contradict this requested mode.
            # Refuse rather than silently retaining it under the same model ID.
            if (self.embedder is not None and not self.doc_vectors and not self.doc_centroid
                    and self.sub.vector_has_record(sid, scope, ctx, model_id=self.embedder.model_id)):
                raise ValueError('existing source vector conflicts with requested vector mode')
            for i, observation in enumerate(observations):
                oid = observation.record_id
                self._put_matching(observation, ctx)
                if self.embedder is not None:
                    self.sub.vector_put(oid, scope, paragraph_vectors[i], ctx=ctx, model_id=self.embedder.model_id)
            if self.embedder is not None and source_vector is not None:
                self.sub.vector_put(sid, scope, source_vector, ctx=ctx, model_id=self.embedder.model_id)
            self.sub.set_job(sid, "ingest", content_hash, "done", scope=scope, ctx=ctx)
        return sid

    def ingest_media(self, name: str, data: bytes, kind: str, ctx, scope: str,
                     describer=None, transcriber=None):
        """Image or audio becomes machine-derived text. Never presented as verbatim (D3-6)."""
        if kind == "image":
            adapter = describer
        elif kind == "audio":
            adapter = transcriber
        else:
            return Rejection(name, "unsupported_kind")
        if adapter is None:
            return Rejection(name, "no_adapter_configured")
        content_hash = _digest(data)
        sid = "media-" + hashlib.sha256(f"{scope}\0{content_hash}".encode()).hexdigest()[:24]
        stub = Record(record_id=sid, record_type="Source", scope=scope, payload={}, attribution="document")
        if ctx is None or not self.sub.scope_engine.authorize_write(stub, ctx).allowed:
            raise ScopeViolation("write not permitted")
        with self.sub.ingestion_transaction():
            row = self.sub._conn.execute(
                "SELECT state FROM jobs WHERE scope=? AND source_id=? AND stage='derive' AND content_hash=?",
                (scope, sid, content_hash),
            ).fetchone()
            # The public job writer and an unkeyed digest cannot attest inference.
            # Until trusted provenance exists, no completed media job is reusable.
            if row and row[0] == 'done':
                raise RecordConflict('completed media reuse is unavailable')
        self.sub.set_job(sid, "derive", content_hash, "running", scope=scope, ctx=ctx)
        try:
            text = adapter(data)
        except Exception:
            self.sub.set_job(sid, "derive", content_hash, "failed", "adapter_failed", scope=scope, ctx=ctx)
            return Rejection(name, "adapter_failed")
        if isinstance(text, str):
            text, _ = fence(text)  # review G-P3-4: derived text is fenced like any other input
        if not isinstance(text, str) or not text.strip():
            self.sub.set_job(sid, "derive", content_hash, "failed", "empty_output", scope=scope, ctx=ctx)
            return Rejection(name, "empty_output")
        vector = None
        if self.embedder is not None:
            try:
                vector = self.embedder.embed_document(text.strip())
            except Exception as exc:
                try:
                    self.sub.set_job(sid, "derive", content_hash, "failed", "embedding_failed", scope=scope, ctx=ctx)
                except Exception as job_exc:
                    exc.add_note(f"Could not record failed job: {job_exc}")
                raise
        failure_reason = "persistence_failed"
        try:
            with self.sub.ingestion_transaction():
                model = getattr(adapter, "model_id", "unknown")
                output = Record(
                    record_id=sid, record_type="Source", scope=scope, payload={"text": text.strip()},
                    attribution="media-derived", source_ids=[],
                    provenance={"machine_derived": True, "model": model, "kind": kind,
                                "name": name, "content_hash": content_hash},
                )
                # Reusing a preexisting output is not proof this adapter produced it.
                self.sub.put(output, ctx)
                if self.embedder is not None:
                    failure_reason = "embedding_failed"
                    self.sub.vector_put(sid, scope, vector, ctx=ctx, model_id=self.embedder.model_id)
                    failure_reason = "persistence_failed"
                self.sub.set_job(sid, "derive", content_hash, "done", 'sha256:' + _record_digest(output), scope=scope, ctx=ctx)
        except Exception as exc:
            try:
                self.sub.set_job(sid, "derive", content_hash, "failed", failure_reason, scope=scope, ctx=ctx)
            except Exception as job_exc:
                exc.add_note(f"Could not record failed job: {job_exc}")
            raise
        return sid
