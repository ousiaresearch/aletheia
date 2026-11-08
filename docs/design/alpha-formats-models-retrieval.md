# Aletheia Alpha: Formats, Models, and Vector Retrieval

**Status:** Design proposal. Not approved. Supports `2026-10-24_aletheia-system-full-plan.md` §7, §8, §9.2.
**Scope:** Open decisions 3 and 4 from the full plan, elaborated. Decision 5 (delete policy) and 6 (hosted service) are deferred.
**Clean-room posture (user decision, Oct 24):** Aletheia's own code is written from specifications. Nothing here copies Arsumbris, Honcho, or OpenViking source, types, or SDKs. Third-party *libraries* and *model weights* are dependencies with their own licenses, recorded in the inventory (§6). This reading of "clean room" is an assumption; it is listed in §8 for confirmation.

**Evidence base.** Facts below come from public model cards, package license declarations, and public benchmark summaries retrieved 2026-10-24. Items marked **[verify]** have not been checked against the artifact itself. Nothing here is a measured Aletheia result. Phase 3 must measure everything marked as a choice.

---

## 1. Hardware reality (this is the constraint that drives the defaults)

| Resource | Local (Arien's Mac) | Colab (Google AI Pro) |
|---|---|---|
| Chip / memory | Apple M3, **8 GB unified** | A100 40 GB or 80 GB (primary tier per user direction), L4/T4 for debug only |
| Usable for models | roughly 4–5 GB after the OS and apps | 40–80 GB |
| Disk free | ~31 GB | ephemeral; persist via Drive or upload |
| Installed ML tooling | none (no Ollama, Whisper, sentence-transformers) | install per session |

**Consequences**
- Any model above ~3B parameters at 4-bit does not comfortably fit local memory alongside the service. A 7–8B reasoning model is **not a local default**.
- Local defaults must be small: embeddings ≤ ~600M parameters, transcription via a quantized build, image description ≤ 3B.
- Heavy evaluation (§5, Phase 4 gold set, any 7B+ model comparison) runs on Colab, and only after the user authorizes each run.
- A user with a larger machine should be able to swap any default. Defaults are configuration, not code.

---

## 2. Alpha formats (decision 3)

### 2.1 Principles that apply to every format

1. **Magic bytes decide the type, not the extension.** A `.pdf` that is actually a ZIP is rejected.
2. **Parsers are untrusted-input surfaces.** PDF and DOCX parsing runs in a worker subprocess with a memory cap, a wall-clock timeout, and no network access. A parser crash or timeout produces a failed-import receipt, never a partial silent import.
3. **Document contents are evidence, never instructions.** An imported document cannot create a Decision, a Commitment, or a scope change (plan §2, invariant 13).
4. **Every derived observation carries a locator** (document ID, version, and page, paragraph, or time range) so a citation can point back to the exact place.
5. **Every extracted observation is marked machine-derived or verbatim.** Only plain-text paragraphs are verbatim. Transcripts and captions are never verbatim.
6. **Failure is explicit.** Unsupported format, encrypted file, scanned PDF without a text layer, zero-byte audio, and extraction timeout each produce a named rejection reason. None produce an empty success.

### 2.2 Format matrix

| Format | Ingest path | Library / model (license) | Locator | Derived output | Known failure modes → rejection reason |
|---|---|---|---|---|---|
| **Markdown** (`.md`) | Parse headings and paragraphs; keep front matter as metadata | Standard library (PSF) | heading path + line range | Paragraph observations, verbatim | Non-UTF-8 → `encoding_unsupported`; binary content → `not_text` |
| **Plain text** (`.txt`) | Paragraphs split on blank lines | Standard library | line range | Paragraph observations, verbatim | Same as Markdown |
| **PDF with text layer** | Extract text per page; keep page numbers | **pypdf (BSD-3)** [verify] | page + paragraph index | Paragraph observations, verbatim (text layer only) | Encrypted → `encrypted_pdf`; no text layer → `no_text_layer_ocr_out_of_alpha`; corrupt → `parse_failed`; over page limit → `page_limit` |
| **DOCX** | Headings, paragraphs, tables as rows | **python-docx (MIT)** [verify] | heading path + paragraph index | Paragraph and table-row observations, verbatim | Corrupt zip → `parse_failed`; tracked changes and comments → recorded as `has_revisions` / `has_comments` counts and surfaced, not silently dropped; embedded images → listed as `unprocessed_image`, not dropped |
| **Audio** (`.m4a`, `.mp3`, `.wav`, `.ogg`/`.opus`, `.flac`) | Decode to 16 kHz mono, transcribe in segments | Decode: ffmpeg (**license depends on the build** — LGPL vs GPL; must be recorded in the inventory before alpha). Transcription: **whisper large-v3-turbo, MIT**, run via whisper.cpp (MIT) | time range per segment | Segment transcripts, machine-derived, with model and version | Length over limit → `duration_limit` (default 2 h per file in alpha); silence-only → `no_speech_detected`; decode failure → `decode_failed` |
| **Images** (`.png`, `.jpg`, `.webp`) | Resize, then describe | Local: **Qwen2.5-VL-3B (Apache-2.0)** [weights Q4 ~3.2 GB per Ollama listing]; lightweight option: **Florence-2-base (MIT, 0.23B)** for captions | image region not used in alpha; whole-image locator | Description, machine-derived, with model and version | HEIC → `format_unsupported_alpha`; corrupt → `decode_failed`; **text inside the image is not extracted in alpha** and the observation says so |

**OCR in alpha (accepted by the user, Oct 25):** text inside images, and PDF pages with no text layer, go through the OCR adapter (Qwen3.5-2B via mlx-vlm). Output is marked machine-derived and carries page/region locators.

**Explicitly out of alpha:** spreadsheets, slides, web crawling, video, archives, email containers.

### 2.3 Why these defaults

- **pypdf over PyMuPDF.** PyMuPDF is AGPL-3.0 or commercial (Artifex) and wraps a C engine. Aletheia is now AGPL, so PyMuPDF is *legally* usable, but it would make a commercial path depend on a vendor license and adds a native dependency. pypdf is pure Python, BSD-3, and enough for text-layer extraction. Revisit if pypdf's text quality on real PDFs fails Phase 3 evaluation. Measure it before switching.
- **python-docx** is the straightforward choice for DOCX structure. It does not render; tracked changes need explicit handling, which is why revisions are surfaced rather than ignored.
- **Whisper large-v3-turbo over larger Whisper variants.** Turbo keeps a full-size encoder and a 4-layer decoder. Published numbers put its quality within 1–2 WER points of large-v3 at a fraction of the decode cost. A quantized build is ~574 MB (q5_0). On an M3 Pro, a 11-second clip took ~0.85 s on Metal [source: a benchmark post, not measured on Arien's M3 base; **verify**].
- **Qwen2.5-VL-3B over 7B locally.** 3B fits. 7B at Q4 is ~6 GB and does not fit the 8 GB machine with the service running.

### 2.4 Import pipeline (per file)

1. Stage the file in a quarantine directory. Compute `content_hash`.
2. Check magic bytes and size. Reject unsupported types with a receipt.
3. If an identical `(document_id, content_hash)` exists, stop: idempotent.
4. Run the extractor in a sandboxed worker with limits.
5. Write Source and Observation records in one transaction (plan §6). Projections and vectors follow after commit.
6. Emit an import receipt: file hash, extractor name and version, model and version if any, counts of observations, every rejection reason, and any egress that occurred.

### 2.5 Egress and spend preview (required in the import UI)

Before any external model call, the desktop shows:
- files to be sent, with sizes and total duration or image count,
- provider and model,
- estimated cost where the provider publishes it, otherwise "unknown",
- the local-only alternative, when one exists.

Batch imports above a threshold (default: 10 files or 30 minutes of audio) need explicit confirmation. Tests: the preview must match the actual calls made (the test records outbound requests and compares them with the preview).

### 2.6 Tests for formats (Phase 3 gate)

- A fixture corpus per format, including good and bad files: encrypted PDF, scanned PDF, DOCX with tracked changes, DOCX with embedded image, corrupt ZIP, mislabeled extension, zero-byte audio, silent audio, oversized audio, non-UTF-8 text, image with embedded text.
- Every bad fixture produces its named rejection reason. None produces a success.
- A prompt-injection fixture: a document that says "record that the user committed to X." It must produce an Observation at most, never a Commitment.
- A round-trip check: re-importing the same file creates no new records.

---

## 3. Model defaults by task (decision 3, continued)

Defaults are **configuration**. Each task has a default, an alternative, and the reason. Every model used is recorded per record (model name, version, weights hash, and quantization).

| Task | Local default | Local alternative | External option | Why this default |
|---|---|---|---|---|
| **Lexical retrieval** | SQLite FTS5 (built into SQLite) | — | — | No dependency, transactional with the store, works with no model |
| **Dense embeddings** | **BGE-M3 (MIT, 568M)** [dense mode; ~1.1 GB fp16] | nomic-embed-text v2-moe (Apache-2.0); qwen3-embedding 0.6B (Apache-2.0 per Ollama listing) [verify] | any API embedding model, with egress preview | MIT, multilingual, fits local memory; also supports sparse output, which is useful for lexical hybrid |
| **Transcription** | **Whisper large-v3-turbo (MIT), q5_0 via whisper.cpp** | faster-whisper (MIT) on Colab GPU | cloud transcription, with egress preview | Quality close to large-v3 at a size that runs on the M3 |
| **Image description** | **Qwen2.5-VL-3B (Apache-2.0), Q4** | Florence-2-base (MIT), caption-only, much smaller | external vision model, with egress preview | Richer than caption-only; 3B is the largest that fits |
| **Reasoning and synthesis** | none locally by default; a small local instruct model only for offline tests | external model with egress policy | external frontier model, per-query opt-in | 7B+ does not fit the local machine; reasoning quality is what the Phase 4 evaluation has to measure |
| **Contradiction check** | deterministic first (same subject, opposing polarity, overlapping time), then model | — | — | Deterministic checks catch the cheap cases and cannot hallucinate |

**Egress policy levels** (set per install, shown in the desktop):
1. **Local-only** (default). No outbound model calls. Anything that needs a missing model is rejected with `model_unavailable_local`.
2. **Local, external on confirmation.** Each external call needs a confirmation that shows what leaves.
3. **External allowed for a named task.** For example, "external for synthesis only," with the preview still shown per batch.

Aletheia never switches a task to an external model silently. A missing local model is a visible failure, not a fallback to the cloud.

**Specialized 2–4B memory model** (decision 7, deferred): it would sit in the same task table. It is evaluated against the default on the same gold set (plan §9.3). No capability is assumed from size.

**License record for models.** Model licenses are recorded beside code licenses in the inventory. Gemma-terms models (for example EmbeddingGemma) are not OSI-open and are **not defaults**. They can be offered as optional choices with the terms shown.

---

## 4. Vector retrieval in alpha (decision 4)

### 4.1 Recommendation

**Ship lexical retrieval in alpha, with vector retrieval included behind a switch that is on by default only if Phase 3 meets the gate in §4.6.** Scope filtering runs before both.

Why not lexical only: paraphrase. A question about "memory providers" should find a record that says "Honcho recall defect", and FTS5 will not. Why not vector-only: vector results are fuzzy and cannot be explained as cleanly. Hybrid, with lexical as a first-class component, gives an explanation the user can check.

### 4.2 Architecture

```
query
  │
  ▼
scope resolve  ──►  allowed scope set S   (deny by default, plan §5)
  │
  ├──► FTS5 lexical over records where scope ∈ S   ──► ranked list L
  │
  └──► vector KNN over vectors where scope ∈ S     ──► ranked list V
  │
  ▼
reciprocal rank fusion (k=60)  ──►  top-N candidates
  │
  ▼
tiered load (L0 → L1 → L2), receipt records every stage
```

- **One store.** Vectors live in the same SQLite file via **sqlite-vec (MIT/Apache-2)** [verified license on the project page]. This keeps one transaction boundary and one backup, and it makes the vector index a rebuildable projection like everything else.
- **Fusion is reciprocal rank fusion**, with no learned reranker in alpha. It is simple, explainable, and needs no training data.

### 4.3 The leak risk and how the design closes it

A vector index is the most likely place for a scope bypass. Typical KNN returns the nearest vectors globally, then a filter is applied afterward, so out-of-scope nearest neighbours can leak through counts, timing, or the ranking of the top-N before filtering.

Design rules:
1. **Scope is a partition, not a filter.** Each scope has its own vector table (`vec_<scope_id>`). A query touches only the tables in S. An out-of-scope table is never opened.
2. **No global vector table exists.** A global table would make the partition rule depend on a filter, which is the thing that fails.
3. **Receipts never leak counts across scopes.** The receipt records "vector candidates: N in scope" for in-scope tables only. Out-of-scope vectors are not counted.
4. **Model changes require reindex.** Each vector row records `embedding_model_id` and `index_generation`. A query with a mismatched generation is refused, not mixed.

**Tests (hard gate):** insert records in an out-of-scope scope whose embeddings are the *closest* to the query. Assert they never appear in results, receipts, timing-independent counts, or the candidate list. This is the vector version of the memory-provider regression test.

### 4.4 Chunking

- **Records** (facts, claims) are short. One record, one vector. No chunking.
- **Documents** are chunked by section, with a target of ~400–512 tokens and overlap of ~50. Chunk locator = (document, version, section path, paragraph range).
- A chunk stores the parent record ID. A deleted document deletes its chunks and vectors in the same transaction (cascade test).

### 4.5 Storage and quantization

- Dense BGE-M3 vectors are 1024-dimensional. float32 is 4 KB per vector. At 100,000 chunks that is ~400 MB before indexing overhead.
- Options: int8 quantization (~4× smaller) or binary (~32× smaller, with more recall loss). Choose by measured recall loss, not by assumption. Default to float32 for the first alpha run; test int8 in Phase 3.
- Index build time and memory on the M3 are measured in Phase 3, not estimated here.

### 4.6 Phase 3 gate for vector retrieval (must pass to enable by default)

| Metric | Requirement | Measurement |
|---|---|---|
| Scope leakage | **Zero** out-of-scope results, in any test | §4.3 test suite |
| Paraphrase recall@10 | Beats lexical-only on a paraphrase set by a margin set before the test, and reported with a confidence interval | Gold set of paraphrased queries (size reported; no claim from small N) |
| Lexical regression | Hybrid is not worse than lexical on exact-term queries | Exact-term set |
| Abstention | Low-similarity queries abstain rather than return noise | Out-of-corpus query set |
| Latency | p95 under a stated budget on the M3 for the alpha corpus size | Benchmark run |
| Rebuild | Full index rebuild from records reproduces identical results | Rebuild test |
| Deletion | Deleting a source removes all its vectors | Cascade test |

**Failure behaviour.** If the embedding model is missing or fails, retrieval falls back to lexical and the receipt says `vector: unavailable (reason)`. It never returns invented similarity.

**If the gate fails:** vector stays off in alpha. Lexical retrieval ships, and the gap is documented in the release notes.

---

## 5. Evaluation plan (feeds Phase 4 and this decision)

| Set | Size | Purpose | Notes |
|---|---|---|---|
| Scope-leak set | Constructed, ~50 cases | Zero leakage | Adversarial; includes closest-vector traps |
| Paraphrase set | ~100 queries | Vector gain | Hand-written, reviewed by the user |
| Exact-term set | ~100 queries | Lexical regression | Includes names, IDs, error strings |
| Abstention set | ~50 queries | Abstain when absent | Out-of-corpus |
| Format fixtures | ~20 files | Format gate | §2.6 |

Sizes are small. Every reported number carries its N and an interval. No parity or superiority claim is made from these sets.

Heavier comparisons (for example, larger embedding models, or the 7B reasoning candidates) run on Colab after authorization. Compute is spent per run, not per session.

---

## 6. Dependency and license inventory (required before alpha distribution)

| Component | Role | License (as found) | Status |
|---|---|---|---|
| pypdf | PDF text layer | BSD-3 | [verify against installed package] |
| python-docx | DOCX | MIT | [verify] |
| ffmpeg | audio decode | LGPL or GPL depending on build | must be pinned to a build; record which |
| whisper.cpp | transcription runtime | MIT [verify] | |
| Whisper large-v3-turbo weights | transcription | MIT (per model card summaries) | [verify model card] |
| BGE-M3 | embeddings | MIT (per MTEB and model listings) | [verify] |
| sqlite-vec | vector search | MIT / Apache-2.0 | verified on project page |
| SQLite FTS5 | lexical | public domain | built into SQLite |
| Qwen2.5-VL-3B weights | image description | Apache-2.0 (per Ollama listing) | [verify on Hugging Face card] |
| Florence-2-base weights | caption fallback | MIT (per model card) | verified on model card |
| PyMuPDF | **not used in alpha** | AGPL-3.0 or commercial | excluded by decision in §2.3 |
| EmbeddingGemma | **not default** | Gemma terms | optional only, with terms shown |

The inventory is produced by a script that reads installed package metadata, not by hand, and is regenerated before every release.

---

## 7. Phase 3 deliverables from this design

1. Format fixture corpus and the rejection-reason tests (§2.6).
2. Sandboxed extractor worker with limits.
3. Egress preview and the test that compares preview with actual outbound calls.
4. Scope-partitioned vector store on sqlite-vec, with the leak test suite (§4.3).
5. Hybrid retrieval with RRF, receipts, and the §4.6 measurements.
6. Measured numbers for the M3 (transcription speed, embedding speed, index size, memory peak) replacing every **[verify]** above.

---

## 8. Open items for the user

1. **"Clean room" reading.** I read it as: Aletheia's own code is written from specs; no copying of Arsumbris, Honcho, or OpenViking code, types, or SDKs; third-party libraries and model weights are allowed as dependencies with recorded licenses. Confirm, or say what should be stricter.
2. **Reading Arsumbris material.** Public documentation (README, SAFETY, au-engine.md, au-mcp.md) may be studied for ideas. Source code must not be read by the team that writes Aletheia's specs. See §9.
3. **ffmpeg build choice.** LGPL build (simpler to ship) or GPL build (more codecs). Decide before Phase 3.
4. **Default egress level.** Proposed: Local-only. Confirm.
5. **Batch confirmation thresholds.** Proposed: 10 files or 30 minutes of audio. Confirm.

---

## 9. Clean-room protocol (applies to all external systems, including Arsumbris)

Arsumbris is strong work: detailed, exhaustive, and carefully reasoned. The plan should take ideas from it, not code. The protocol below separates reading from writing so that Aletheia's implementation can be defended as independent.

**Two roles, kept separate per session.**
- **Study role** reads public documentation and writes *behavioural* notes: what a component promises, what it refuses, what it fails on, and why. It writes no implementation and copies no text. Notes are in our words, with the source URL and access date.
- **Implementation role** works only from the Aletheia spec and the study notes. It does not open Arsumbris, Honcho, or OpenViking source. If it needs something the notes lack, it sends a question back to the study role, not to the source.

**Provenance log.** One entry per absorbed idea:
`idea` · `source (URL, date)` · `study note file` · `spec section in Aletheia` · `implementer` · `tests that cover it`.

**Prohibited.** Reading source files in external repositories for the purpose of implementation. Copying identifiers, comments, schemas, or test fixtures. Using the earlier REA-style "reverse-engineer anything" workflow on these repositories, because that workflow reads source.

**Permitted.** Reading documentation, running a system we have legitimate access to and observing behaviour, and writing down what it does. Behavioural experiments are recorded with inputs and outputs in our own files.

**Ideas already identified for study (behavioural level only):**
- typed record concept and advisory validation (the engine shows that advisory diagnostics do not block saves; Aletheia will make scope checks authoritative instead),
- hook shapes for governance (mediator, stamper, session-start) as a concept: Aletheia's version is a write-boundary check, fail-closed,
- tiered loading L0 → L1 → L2, which is independently useful and already in the plan,
- the observation that Arsumbris mediation fails open when its daemon is unreachable. This is a *failure* to avoid, and it is recorded as a negative requirement.

This protocol is the working rule for every external system. It is written into the plan's licensing section as a binding constraint.
