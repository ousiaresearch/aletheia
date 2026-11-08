# Aletheia Model Selection and Open-Item Decisions (Oct 2026)

**Status:** Evidence-backed proposal. Supersedes the model table in `alpha-formats-models-retrieval.md` §3 and the open items in its §8.
**Method:** Licences and parameter counts were read from the Hugging Face model API (`/api/models/<id>`, `license:*` tag and `safetensors.total`) and PyPI/GitHub metadata on 2026-10-24. Benchmark numbers come from the model card where stated; where they come from a leaderboard snapshot or a blog, the source is named and the figure is marked **[secondary]**. Nothing here is a Aletheia measurement. Phase 3 and 4 measure everything on this machine.

---

## 0. Corrections to the earlier design doc

- **openai/whisper-large-v3** carries an `apache-2.0` tag on Hugging Face. The Whisper *code* repository is MIT. Earlier I wrote "MIT" for the large-v3 weights; that was incomplete. Turbo's weights are tagged MIT. Record both tags in the inventory and check the model card at pin time.
- **Whisper is no longer the accuracy leader.** On the Open ASR leaderboard snapshot (May 2026) Whisper large-v3-turbo sits near the bottom of the top twenty, at mean WER 7.83 [secondary, CodeSOTA snapshot]. Your instinct was right. Its advantages are runtime maturity (whisper.cpp with Metal) and broad language coverage, not accuracy.
- **Qwen3-Embedding-8B** was the default I cited. It is still Apache-2.0, but Microsoft's harrier-oss-v1 models now report higher Multilingual MTEB v2 scores, and the 0.6B harrier model is MIT.
- **Qwen3-Embedding's model card** describes itself as "the latest proprietary model of the Qwen family," which conflicts with its Apache-2.0 tag. Treat the tag as the licence claim and confirm against the LICENSE file at pin time.

---

## 1. Hardware budget (drives every local choice)

Local: Apple M3, 8 GB unified memory. Assume about 4–5 GB is usable for models while the service and desktop run.
Weights at 16-bit take 2 bytes per parameter. At 4-bit, about 0.5 bytes plus overhead.

Colab (Google AI Pro): A100 40 GB and A100 80 GB are the primary tier per your direction. L4 24 GB is for debug only.

| Model size | bf16 | 4-bit (approx.) | Where it runs |
|---|---|---|---|
| ≤ 1B | ≤ 2 GB | ≤ 1 GB | Local |
| 2–4B | 4–9 GB | 2–3 GB | Local at 4-bit, 2B preferred |
| 8B | ~17 GB | ~5 GB | Colab, or local only at 4-bit with nothing else loaded (not recommended) |
| 20–32B | 40–67 GB | 12–19 GB | Colab A100 40 GB at 4-bit, or A100 80 GB at bf16 |
| 290B+ MoE | — | — | Not self-hostable on Colab; external API only |

The 4-bit numbers are estimates from parameter counts, not measurements. Phase 4 measures them.

---

## 2. Embeddings

| Role | Choice | Licence (HF tag) | Params | Dim / max tokens | Benchmark (card) | Where |
|---|---|---|---|---|---|---|
| **Local default** | **microsoft/harrier-oss-v1-0.6b** | MIT | 596M | 1024 / 32,768 | MTEB v2 69.0 | Local, ~1.2 GB bf16 |
| Quality tier | **microsoft/harrier-oss-v1-27b** | MIT | 27.0B | 5376 / 32,768 | MTEB v2 **74.3**, #1 Multilingual v2 on the card's own report | Colab A100 80 GB bf16, or A100 40 GB at 8-bit |
| Alternative | Qwen/Qwen3-Embedding-0.6B | Apache-2.0 | 596M | 1024 / 32K, MRL 32–1024 | — | Local fallback |
| Alternative | Qwen/Qwen3-Embedding-8B | Apache-2.0 | 7.6B | 4096 / 32K, MRL 32–4096 | older MMTEB 70.58 (June 2025 card) | Colab |
| Reranker (candidate) | Qwen3-Reranker family | licence **not yet read** | — | — | — | Phase 3 evaluation only |

**Decision (proposed):** harrier-oss-v1-0.6b as local default; harrier-oss-v1-27b as the Colab quality tier. Both MIT, which keeps the inventory simple.

**Why this and not Qwen3-Embedding:** harrier's card reports higher multilingual scores at both sizes, and its licence is MIT where Qwen3-Embedding is Apache-2.0. Both are fine licences. The choice comes down to measured quality on our own gold set.

**Caveat:** harrier-27b has very few downloads on the hub (5,812 at check time), so it is newer and less battle-tested than the Qwen models. Test it on our data before trusting its ranking.

**Unvetted by design:** secondary-source lists name NVIDIA Nemotron-3-Embed-8B and Jina v5, but I have not read their licences. Jina v3/v5 and several NV-Embed variants are CC-BY-NC, which is incompatible with a distributed AGPL product only if we ever want commercial use; they are excluded from defaults for that reason.

---

## 3. Transcription

| Role | Choice | Licence | Params | Quality (mean WER, Open ASR, en) | Runtime | Where |
|---|---|---|---|---|---|---|
| **Local default** | **whisper large-v3-turbo** via whisper.cpp | MIT (code and turbo weights) | 809M | 7.83 [secondary] | whisper.cpp, Metal, mature | Local, q5_0 ≈ 574 MB |
| Local candidate | **Qwen/Qwen3-ASR-0.6B** | Apache-2.0 | 938M (tagged 0.6B) | not on the snapshot used | Qwen toolkit / vLLM; **Mac runtime not verified** | Local only if a runtime exists on M3 |
| Quality tier | **ibm-granite/granite-speech-4.1-2b** | Apache-2.0 | 2.3B | **5.33** [secondary, CodeSOTA, May 2026] | HF transformers | Colab, bf16 ≈ 5 GB |
| Quality tier | **Qwen/Qwen3-ASR-1.7B** | Apache-2.0 | 2.35B | 5.76 [secondary, CodeSOTA]; 5.40 [secondary, Oct 9 leaderboard update per a blog] | Qwen toolkit / vLLM | Colab |
| Alternative | **CohereLabs/cohere-transcribe-03-2026** | Apache-2.0, **gated** (accept terms on HF) | 2.07B | 5.42 [secondary] | HF transformers | Colab |
| Not default | nvidia/parakeet-tdt-0.6b-v2 | CC-BY-4.0 | — | 6.05 [secondary] | NeMo | Only if NeMo is acceptable |

**Decision (proposed):**
- **Local:** keep whisper large-v3-turbo through whisper.cpp for now. It is the only option I have confirmed runs on the M3 today. Qwen3-ASR-0.6B is the local candidate to test once a Mac runtime is confirmed, because it is Apache-2.0 and smaller.
- **Quality path (Colab):** Granite Speech 4.1 2B as the default, with Qwen3-ASR-1.7B as the second opinion. Both are Apache-2.0 and both beat Whisper turbo on the snapshot.
- **Evaluation** on our own audio decides between them. Public WER on read speech does not predict performance on conversation recordings.

**Why this changes the earlier plan:** the original design assumed whisper was the best local choice. That still holds for local runtime reasons, but not for accuracy. Accuracy is a Colab concern. The alpha design should therefore allow the transcription backend to be swapped by configuration, with receipts recording which backend produced each transcript.

---

## 4. Vision-language models (images, and OCR-in-images)

| Role | Choice | Licence | Params | Notes | Where |
|---|---|---|---|---|---|
| **Local default** | **Qwen/Qwen3-VL-4B-Instruct** | Apache-2.0 | 4.4B | 4-bit ≈ 2.5–3 GB | Local (estimate: fits 8 GB with headroom; **Phase 4 measures**) |
| Local fast | Qwen/Qwen3-VL-2B-Instruct | Apache-2.0 | 2.1B | smaller, lower quality | Local |
| Local alternative | google/gemma-4-E2B-it | Apache-2.0 | 5.1B total (effective ~2B) | multimodal; capability to verify | Local, measure |
| Quality tier | **Qwen/Qwen3-VL-32B-Instruct** | Apache-2.0 | 33.4B | 4-bit ≈ 19 GB | Colab A100 40 GB at 4-bit |
| Quality tier, middle | Qwen/Qwen3-VL-8B-Instruct | Apache-2.0 | 8.8B | 4-bit ≈ 5–6 GB | Colab or local (tight) |
| OCR specialist (candidate) | PaddleOCR-VL-1.6 (0.9B, Apache-2.0) [secondary] or GLM-OCR (0.9B, MIT) [secondary] | not yet read on the hub | 0.9B | document-to-Markdown with tables | Local (small) |

**Decision (proposed):**
- **Images:** Qwen3-VL-4B locally; Qwen3-VL-32B on Colab for quality checks and for the gold set.
- **Text inside images:** the earlier design said "not extracted in alpha." That is a product limitation, and many real images contain text. I recommend adding a document-OCR specialist as a **Phase 3 option**, gated behind its own licence read and our own test. Decision for you (§6).

---

## 5. Reasoning and synthesis

| Role | Choice | Licence | Params | Where | Note |
|---|---|---|---|---|---|
| **Local (honest ceiling)** | Qwen3-VL-4B text ability, or gemma-4-E2B | Apache-2.0 | ~2–4B | Local, offline tests only | A 4B model is not a reliable synthesizer. Use for tests and abstention checks, not for answers. |
| **Colab, open, default candidate** | **openai/gpt-oss-20b** | Apache-2.0 | 20.9B (MoE) | Colab A100 40 GB, MXFP4 ≈ 13 GB [estimate; verify] | Strong reasoning, runs on one A100 40 GB |
| **Colab, open, alternative** | **Qwen/Qwen3.8-27B** | Apache-2.0 | 27.8B (multimodal) | Colab A100 80 GB bf16, or A100 40 GB at 4-bit | Dense, multimodal. Its capability claims come from secondary sources. |
| Open frontier (external only) | deepseek-ai/DeepSeek-V4-Flash | MIT | 290.9B | Provider API only | Not self-hostable on Colab. Use only through the egress policy. |
| Open frontier (external only) | GLM-5.x family | MIT [secondary] | ~750B | Provider API only | Same |

**Decision (proposed):**
- **Default synthesis path:** external model under the egress policy, because local 8 GB hardware cannot run a reliable reasoner.
- **Colab evaluation set:** gpt-oss-20b and Qwen3.8-27B, both Apache-2.0, both run on A100. Compare them on the Phase 4 gold set. Either becomes the open default only after that comparison.
- **Frontier models** are used only through an external provider and are never claimed as self-hosted.

---

## 6. The five open items from the design doc (§8), decided

| # | Item | Proposed resolution | Status |
|---|---|---|---|
| 1 | "Clean room" reading | Aletheia's own code is written from specs. No copying of Arsumbris, Honcho, or OpenViking code, types, schemas, or fixtures. Libraries and model weights are dependencies with recorded licences. | Your answer ("clean room operations only") matches this reading. **Settled**, unless you say otherwise. |
| 2 | Reading Arsumbris material | Public docs may be studied for behaviour. The implementation role never reads Arsumbris source. Protocol in design doc §9. | Follows from item 1. **Settled.** |
| 3 | ffmpeg build | **LGPL build, pinned**, with no GPL-only components (no x264/x265). Verify with `ffmpeg -buildconf` and record the build hash. Decoding mp3, m4a/AAC, Opus, FLAC, and WAV is covered by the LGPL build. | **Proposed.** I'll confirm the build and record it in the inventory before Phase 3. |
| 4 | Default egress level | **Local-only.** External calls require explicit confirmation per task, with the preview shown. | **Proposed.** Low-stakes; I'll proceed unless you object. |
| 5 | Batch confirmation thresholds | Confirmation required for **any** of: more than 10 files; more than 30 minutes of audio; more than 100 MB; any external provider; any estimated spend above a configured amount. The earlier "10 files or 30 minutes" rule missed the external-provider and size cases. | **Proposed.** |

Two items **need your decision**, below.

---

## 7. Decisions needed from you

1. **Image text (OCR).** Add a document-OCR specialist to alpha, behind a licence read and our own test? Or keep "text inside images is not extracted in alpha" and say so in the import receipt? My recommendation: add it as a Phase 3 option, off by default.
2. **Quality tier on Colab.** Start the Phase 4 comparison with gpt-oss-20b and Qwen3.8-27B (both Apache-2.0, both fit A100). Or name a different model you want included. Compute is spent only after you authorize the run.

Everything else above is proposed and reversible by configuration.

---

## 8. Changes to the inventory (§6 of the design doc)

| Component | Change |
|---|---|
| openai/whisper-large-v3 | Record **both** tags: code MIT, HF weights tag Apache-2.0. Check the card at pin time. |
| openai/whisper-large-v3-turbo | MIT (verified tag). |
| harrier-oss-v1-0.6b / 27b | **New.** MIT (verified tag). |
| Qwen3-Embedding 0.6B / 8B | Apache-2.0 (verified tag). Demoted from default; kept as alternative. |
| Qwen3-ASR-0.6B / 1.7B | **New.** Apache-2.0 (verified tag). |
| granite-speech-4.1-2b | **New.** Apache-2.0 (verified tag). |
| cohere-transcribe-03-2026 | **New.** Apache-2.0, gated (requires accepting terms). |
| Qwen3-VL 2B / 4B / 8B / 32B | **New.** Apache-2.0 (verified tags). |
| gemma-4-E2B / E4B | Apache-2.0 (verified tag). **Earlier Gemma-terms wording** for EmbeddingGemma is still correct for that model, which is not a default. |
| gpt-oss-20b | **New.** Apache-2.0 (verified tag). |
| Qwen3.8-27B | Apache-2.0 (verified tag). |
| DeepSeek-V4-Flash | MIT (verified tag). External only. |
| pypdf | Licence field empty on PyPI JSON; classifiers empty. **Not verified**; read the LICENSE in the wheel before pinning. |
| python-docx | MIT (verified, PyPI classifier). |
| whisper.cpp | MIT (verified, GitHub API). |
| sqlite-vec | **Apache-2.0** (verified, GitHub API). Earlier "MIT/Apache" was loose; record Apache-2.0 for the code and check the LICENSE file at pin time. |
| ffmpeg | GitHub reports no SPDX (NOASSERTION). Licence depends on build. Record the build. |

---

## 9. Evaluation that settles the defaults (Phase 4)

| Decision | Test | Pass condition |
|---|---|---|
| Embedding default | harrier-0.6b vs Qwen3-Embedding-0.6B on our gold set | Pick the winner on recall@10 with the interval reported; tie → harrier (MIT, simpler inventory) |
| Local transcription | whisper turbo vs Qwen3-ASR-0.6B on M3 (if runtime exists) | Lower WER on our own conversation recordings, plus M3 speed |
| Quality transcription | Granite 4.1 vs Qwen3-ASR-1.7B vs Cohere on our audio | Lowest WER with interval; Cohere only if terms accepted |
| Local VLM | Qwen3-VL-4B vs 2B vs gemma-4-E2B on our image set | Accuracy with peak unified-memory measured on the M3 |
| Synthesis | gpt-oss-20b vs Qwen3.8-27B vs external default on gold set | Citation precision, abstention correctness, zero scope leakage (hard gate) |

Every result is recorded with its N and an interval. Peak memory is measured, not estimated. No model is named the default by a benchmark alone; the test must also pass the scope-leakage gate.

---

## 10. Sources (retrieved 2026-10-24)

- Hugging Face model API for each repo above (licence tags, parameter totals, gating).
- Model cards: Qwen/Qwen3-Embedding-8B, Qwen/Qwen3-Embedding-0.6B, microsoft/harrier-oss-v1-27b, Qwen/Qwen3-ASR-1.7B.
- PyPI JSON: python-docx. GitHub API: whisper.cpp, sqlite-vec, FFmpeg.
- Secondary (flagged [secondary] above): Open ASR leaderboard snapshot on CodeSOTA (May 2026); a transcription comparison on pinggy.io (Oct 2026); OCR and VLM roundups (vdf.ai, presenc.ai, mixpeek, roboflow); embedding roundups (maximem.ai, eden.ai). These were used to find candidates, not to establish quality. Every candidate listed must still pass our own tests.
