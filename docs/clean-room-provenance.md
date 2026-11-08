# Clean-room provenance log

Status: first version, 2026-10-26. Owner: Aletheia maintainers. Required by the user's Oct 24 instruction: "Clean room operations only", with a provenance log.

## 1. Rule

- Arsumbris, Honcho, OpenViking, and any other external memory system are clean-room subjects.
- Study role: may read public documentation and observe public behaviour. Records each source here.
- Implementation role: writes Aletheia code from Aletheia's own written specs in `docs/`. Does not read external source or types while implementing.
- Reviewers (Codex) must not read external source. Reviewers do not modify files.
- Libraries and model weights are dependencies, not clean-room subjects. They are listed in section 3 with licences.
- Design ideas taken from external systems are limited to: type-system concepts, hook shapes, and tiered loading (user, Oct 24 clarification). Each adopted idea is listed in section 2 with the Aletheia spec that restates it.

## 2. External systems: what was studied and what was adopted

| Subject | Date | Source (URL/local file) | Access | Recorded use | Source of the idea in Aletheia | Spec that restates it |
|---|---|---|---|---|---|---|
| Arsumbris | 2026-10-11 | https://github.com/arsumbris/arsumbris | Public docs and behaviour only | Design ideas: typed records, tiered loading | Aletheia specs (not copied) | `docs/design/alpha-formats-models-retrieval.md`, `docs/design/phase2-knowledge-model.md` |
| Honcho | 2026-10-10 | Arien's Hermes install (`~/.hermes/plugins/honcho`, runtime) | Public docs; behaviour observed in the operator's own Hermes install (recall injection, profile config) | Failure-mode analysis of recall injection; commits `310eba9`, `dfff027` | Observed behaviour in Arien's runtime | `docs/architecture.md` (failure modes) |
| OpenViking | 2026-10-12 | https://github.com/arsumbris/openviking | Public docs only | Licence screen; no design taken | None recorded | `docs/research/license-verification.md` |
| Memory-provider A (Solstice) | 2026-10-24 | `/Users/johannross/.hermes/hermes-agent/plugins/model-providers/solstice/` | Local Hermes plugin code read | Confirmed single OAuth PKCE, no multiple-auth pooling | Credential cycling is an operational constraint, not a design adoption | `docs/operational-requirements.md` (credential cycling constraint) |
| Memory-provider B (Honcho plugin) | 2026-10-10 | `/Users/johannross/.hermes/plugins/honcho/` | Local Hermes plugin code read | Confirmed hybrid recall, auto-injection, workspace/aiPeer fields | Recall isolation and workspace separation are failure-mode constraints, not design adoptions | `docs/architecture.md` (recall isolation, workspace boundaries) |

Commit `2777741` described a "license-screen 5 systems" step. The five systems inspected are Arsumbris, Honcho, OpenViking, Solstice, and the Honcho plugin. All five are now named above.

Access log: one entry per source session is recorded in this table (date, source, what was read). No separate access log file exists.

## 3. Dependencies (recorded licences)

Licences are read from installed package metadata in `.venv` where possible. Entries marked "verify" need a check against the source repository before the first release.

| Dependency | Purpose | Version | Licence (as recorded) | Notes |
|---|---|---|---|---|
| pypdf | PDF text extraction | 6.21.2 | BSD-3-Clause | verified via PyPI/project page |
| python-docx | DOCX text extraction | 1.2.0 | MIT | metadata |
| sqlite-vec | Vector index in SQLite | 0.1.9 | MIT and Apache-2.0 (dual) | metadata |
| mlx-embeddings | Loads embedding models on Apple MLX | 0.1.0 | GPL-3.0 | compatible with Aletheia's AGPL-3.0; needs user review |
| mlx-vlm | Runs the Qwen3.5-2B vision-language adapter | 0.7.10 | MIT | metadata |
| pywhispercpp | whisper.cpp binding for transcription | 1.5.1 | MIT | metadata |
| mlx | Apple MLX runtime | 0.32.4 | MIT | metadata |
| numpy | Numerical arrays | 2.5.4 | BSD-3-Clause | verified via PyPI |
| pytest | Test runner (dev only) | 9.1.1 | MIT | verified via PyPI |

## 4. Model weights (not shipped with Aletheia)

Aletheia ships adapters, not weights (user, Oct 24). Model licences are shown to the user at fetch time, with consent.

| Model | Role | Licence | Source | Status |
|---|---|---|---|---|
| BAAI/bge-m3, mlx-community 4-bit build | Embeddings (adopted) | MIT (bge-m3); quantised build inherits | Hugging Face | in use; 337 MB |
| Qwen3.5-2B, mlx-community 8-bit | OCR and image description | Apache-2.0 (earlier note) | Hugging Face | in use; 2.5 GB |
| whisper large-v3-turbo, ggml q5_0 | Transcription | MIT code; weights tagged apache-2.0 on HF | Hugging Face | in use; 574 MB; SHA-256 must be recomputed on the complete file before pinning |
| Qwen3-Embedding-0.6B | Embedding candidate | Apache-2.0 | Hugging Face | measured, removed |
| microsoft/harrier-oss-v1-0.6b | Embedding candidate | MIT | Hugging Face | measured, removed |

## 5. Not yet recorded

- ffmpeg build (proposed LGPL, pinned, no GPL-only codecs): not built or verified. Check with `ffmpeg -buildconf` before use.
- Access log for external study sessions (see section 2).
- Model licence acceptance flow for `aletheia models fetch`: not built.

## 6. Update procedure

1. Before reading any external source for study, add a row to section 2 or an access log line.
2. When a design idea is adopted, cite the Aletheia spec that restates it. Do not cite external code.
3. When a dependency or model is added, add a row to section 3 or 4 with licence read from metadata or the source.
4. Commit the change in the same commit as the design or dependency change.
