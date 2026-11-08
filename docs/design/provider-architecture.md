# Aletheia Provider Architecture: Adapters, Not Bundled Models

**Status:** Decision (user, Oct 24): "We simply suggest to users options, and build the implementations for it."
**Supersedes:** the implicit assumption in `model-selection-2026-10.md` and `alpha-formats-models-retrieval.md` that Aletheia would distribute or depend on specific model weights.

---

## 1. Principle

Aletheia ships **code and interfaces**. Models are **user choices**.

- Aletheia defines a small set of **task interfaces** (embed, transcribe, describe, OCR, reason).
- Aletheia ships **adapters** that implement those interfaces. Each adapter is tested in the release.
- A model is a **configuration entry** that names an adapter and its parameters. Aletheia does not ship weights.
- The model catalogue in the docs is a **list of suggestions with evidence**, not a bill of materials.

Consequences:

| Area | Effect |
|---|---|
| Distribution size | Small. No weights in the package. |
| Licence exposure | Aletheia's own code (AGPL) and its library dependencies. Model licences are shown to the user at fetch time, not redistributed by us. |
| Inventory (design doc §6) | Covers shipped code and dependencies only. Model weights get a separate per-model "terms shown to user" record. |
| Claims | We claim support only for adapters that pass the conformance suite and the Phase 4 evaluation. Everything else is labelled **experimental** or **untested**. |
| Defaults | Defaults are recommended configuration in the install guide. Users can change them without code changes. |

---

## 2. Task interfaces

Each interface is narrow, typed, and returns **receipts-ready metadata**. An adapter never writes records itself; the pipeline does (plan §2, invariant 1 and 11).

| Interface | Input | Output | Required metadata in every output |
|---|---|---|---|
| `Embedder` | list of texts (with optional instruction prefix) | vectors, dimension | adapter id, model id, revision, quantization, dimension, locality |
| `Transcriber` | audio segment (16 kHz mono PCM) | segments with time ranges, language, confidence if available | same, plus decode settings |
| `Describer` | image (resized) + prompt template | description text | same, plus prompt template id |
| `OCR` | image or page | text with optional regions | same |
| `Reasoner` | structured prompt over in-scope evidence only | answer text + cited evidence IDs + abstention flag | same, plus sampling settings |

**Rules every interface enforces:**
1. **Scope first.** The caller passes only evidence that has already passed the scope engine. An adapter has no handle to the store.
2. **Locality declared.** Each adapter declares `local` or `external(provider)`. The egress policy (plan §9.2) is checked by the pipeline before the call, not by the adapter.
3. **Failure is named.** Errors are typed: `model_unavailable`, `resource_limit`, `egress_denied`, `invalid_output`, `timeout`. No empty success.
4. **No hidden state.** Adapters are stateless between calls, so a receipt can reproduce the call's configuration.

---

## 3. Adapter families

Three families cover almost every real option. Writing one adapter per model does not scale; writing one per protocol does.

| Family | Covers | Notes |
|---|---|---|
| **OpenAI-compatible HTTP** (`/v1/chat/completions`, `/v1/embeddings`, audio transcription endpoint where offered) | Local servers (Ollama, llama.cpp server, vLLM, LM Studio), many hosted providers | One adapter, many options. Locality is set by configuration: a loopback address is `local`, anything else is `external`. |
| **Local in-process runtimes** | whisper.cpp (transcription), a small embedder via a Python worker, a VLM via a local runtime | Each runtime gets one adapter. These run in a worker subprocess with memory limits. |
| **Native external APIs** | Providers whose APIs are not OpenAI-compatible | Added only when a user needs one. Each is a separate adapter. |

**Locality check (important):** a `localhost` URL does not prove locality. A user can point the "local" adapter at a remote host through a tunnel. The adapter reports the **resolved address**, and the egress policy evaluates the address, not the label. Test: a loopback-bound config that resolves to a non-loopback address is refused.

---

## 4. Alpha reference adapters (what we build and test)

Chosen to cover each interface with the fewest adapters, using the evidence in `model-selection-2026-10.md`.

| Interface | Reference adapter | Why this one |
|---|---|---|
| Embedder | OpenAI-compatible embeddings adapter, exercised against a local server running harrier-oss-v1-0.6b (MIT) | One adapter covers local and external. The suggested model is MIT. |
| Transcriber | whisper.cpp local adapter, whisper large-v3-turbo (MIT) | Mature Metal runtime on the M3. |
| Describer | OpenAI-compatible adapter, exercised against a local server running Qwen3-VL-4B-Instruct (Apache-2.0) | Local, multimodal request format. |
| Reasoner | OpenAI-compatible adapter; external provider chosen by user; tested against one external and one local endpoint | Protocol coverage matters more than a single model. |
| OCR | OpenAI-compatible adapter, exercised against a local Qwen3.5-2B (Apache-2.0) run through mlx-vlm on Apple silicon | Accepted for alpha by the user (Oct 25). Qwen3.5 is required for local runtimes; case fidelity is a known gap (see conformance). |

Everything else in the earlier catalogues (Qwen3-ASR, Granite Speech, Cohere Transcribe, gpt-oss, Qwen3.8, DeepSeek, GLM) is an **option the user can configure**. It becomes "supported" only after an adapter exists and passes the conformance suite and our evaluation. Until then it is "configurable, untested," and the UI says so.

---

## 5. Conformance suite (every adapter, every release)

An adapter is **supported** only if it passes all of these:

| Test | Pass condition |
|---|---|
| Interface contract | Output shape and types match; malformed model output raises `invalid_output` |
| Metadata completeness | Every output carries adapter id, model id, revision, locality, and settings |
| Locality | Resolved address matches declared locality; loopback-labelled remote addresses are refused |
| Egress gate | With egress set to local-only, an external-declared adapter makes **zero** network calls (asserted by a socket-level test) |
| Scope isolation | Adapter receives only the passed evidence; a canary record outside scope never appears in the request body (asserted on the captured request) |
| Failure naming | Missing model, timeout, memory limit, and bad input each raise the named error |
| Determinism settings | Temperature and seed settings are recorded; a repeated call with fixed seed is compared and the variance reported |
| Resource limits | Worker exceeds memory or time limit → killed, named error, no partial record |
| Receipts | A pipeline run produces a receipt that reproduces the call configuration |

The conformance suite is a **release gate**. A failing adapter is removed from the supported list, not left in with a warning.

---

## 6. Model acquisition and consent

Aletheia does not fetch models silently.

1. `aletheia models list` shows options with status (supported / configurable / untested), size, locality, and licence from the model's own metadata.
2. `aletheia models fetch <id>` shows the licence text or its URL, size, source, and expected checksum, and requires confirmation.
3. Weights are stored in a Aletheia-managed directory with checksum verification.
4. Gated models (for example Cohere Transcribe) require the user to accept terms on the provider's site. Aletheia does not accept terms on the user's behalf, and does not store provider tokens outside the profile-scoped credential store.
5. Models can be removed. Removing a model removes its weights, and receipts keep the model id and checksum only.

---

## 7. Configuration sketch

```yaml
# aletheia.yaml (illustrative)
egress: local_only            # local_only | confirm_external | external_allowed:<tasks>
tasks:
  embed:
    adapter: openai_compatible
    endpoint: http://127.0.0.1:11434/v1      # resolved and checked at startup
    model: harrier-oss-v1-0.6b
    locality: declared_local
  transcribe:
    adapter: whisper_cpp
    model_path: models/whisper-large-v3-turbo-q5_0.bin
  describe:
    adapter: openai_compatible
    endpoint: http://127.0.0.1:11434/v1
    model: qwen3-vl-4b-instruct
  reason:
    adapter: openai_compatible
    endpoint: https://provider.example/v1    # external → requires egress policy allowing it
    model: user-chosen
    locality: declared_external
```

The startup check fails if a `declared_local` endpoint resolves outside loopback, or if an external endpoint is configured while egress is `local_only`.

---

## 8. What changes in the existing docs

| Document | Change |
|---|---|
| `model-selection-2026-10.md` | Reframed: the tables are **suggestions with evidence**, not a bill of materials. §6 inventory applies to shipped code only. |
| `alpha-formats-models-retrieval.md` §3 | "Model defaults" become "recommended configuration." §6 inventory: model rows move to a per-model "terms shown" list. |
| `alpha-formats-models-retrieval.md` §2 | Format matrix is unchanged; extractors are shipped code. Where it names a model, read it as a configurable adapter target. |
| Plan §9.2 | "Model layer" is now the provider-adapter layer with the interfaces in §2 above. |
| Plan §15 (licensing) | Aletheia ships no weights. Add "model terms displayed at fetch time" as an obligation. |

---

## 9. Decisions (resolved by user, Oct 24)

1. **Alpha adapters: confirmed, then amended.** Ship the four in §4 (embed, transcribe, describe, reason), plus OCR. OCR was deferred on Oct 24 and accepted for alpha on Oct 25 by the user.
2. **Community adapters: first-party only through the first release.** A documented plug-in contract comes after release, under the same conformance suite.
3. **Hosted providers: two chat wire formats, not one.** The reasoning adapter must speak both:
   - **OpenAI chat format** (`/v1/chat/completions`), spoken by most local servers and many hosted providers.
   - **Anthropic Messages format** (`/v1/messages`), spoken by Anthropic's API and some compatible hosts.

   Both sit behind the same `Reasoner` interface. The adapter is chosen by configuration (`format: openai | anthropic`). Neither format is preferred in the core. Each gets its own conformance tests.

## 9a. Later: proprietary models per adapter

Each adapter family may later get proprietary-model support, for example a hosted provider's embedding endpoint or a vendor's transcription API. These are additional adapters behind the same interfaces, not changes to the core. They inherit the locality rule (§3), the egress gate, and the conformance suite. They are out of the first release.

## 9b. Adapter examples, in plain terms

- **Embedding adapter (OpenAI format).** Sends text to a local server at a configured address, receives vectors, and stores the model name and version with each vector.
- **Reasoning adapter (two formats).** Same request shape to Aletheia, translated to either OpenAI or Anthropic wire format. Aletheia's core only sees a normalized answer with cited evidence IDs and an abstention flag.

---

## 10. Decision record

- Aletheia ships interfaces and adapters; models are user-selected configuration.
- Support claims apply only to adapters that pass the conformance suite.
- Model weights are fetched with user consent and are never redistributed by Aletheia.
- Locality is decided by resolved address, checked by the egress policy, not by a label.
