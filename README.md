# Aletheia

Evidence-bound scoped memory for Hermes agents. Aletheia is the memory-provider surface over the
**Weft** substrate: turns are written to a local SQLite store inside the active profile, recall is
**scoped before it is ranked**, and every recall returns a receipt naming the scope consulted, the ids
selected and the tier.

This repository is the plugin. The substrate ships as the [`weft-memory`](https://pypi.org/project/weft-memory/)
package (AGPL-3.0), so the plugin here is only a manifest and an entry point.
Version 1.1.1 requires `weft-memory>=1.1.1,<2`; earlier substrate releases do not satisfy
the dependency. The 1.1.1 substrate must be published before a normal installation can resolve it.

## Install

```bash
hermes plugins install ousiaresearch/aletheia      # installs disabled
hermes plugins enable aletheia                      # then opt in
```

Admission to the Hermes plugin catalog is a separate, human-reviewed PR; until that lands, install by
`owner/repo` as above or pin a commit with `--ref <sha>`.

## Activate

Aletheia is an **external memory provider**, so it is selected per profile rather than auto-enabled:

```bash
hermes config set memory.provider aletheia
```

The built-in store (MEMORY.md / USER.md) keeps working: Hermes runs the builtin provider first and at
most one external provider, and a failure in either never blocks the other. Enabling Aletheia is
additive, not a swap.

## What it needs

Retrieval quality is part of the contract, so Aletheia does not fall back to keyword search. The
default embedder requires Apple Silicon macOS. It needs:

- the `mlx-community/bge-m3-mlx-4bit` embedder and the `cross-encoder/ms-marco-MiniLM-L-6-v2`
  reranker, downloaded separately into the Hugging Face cache as described in
  [Weft's model setup](https://github.com/ousiaresearch/weft#models); the provider does not download
  them at runtime;
- `weft-memory` (declared as a Python dependency), `sentence-transformers>=3.0,<6`, and on Apple silicon
  `mlx-embeddings`.

The Hermes provider exposes text ingestion, not an image or audio ingestion setup. Weft's separate
Python media-ingestion API accepts caller-supplied image describer or audio transcriber adapters;
the supplied Qwen adapter additionally needs `mlx-vlm` and a local model, and the Whisper adapter
needs `pywhispercpp`, a local model and `ffmpeg`. Installing models alone does not wire media into
the Hermes provider.

If a required model is missing, `is_available()` is false and `unavailable_reason()` says which model
and how to fetch it — the provider never activates as a silent downgrade.

## What it does per turn

| Hermes hook | What happens |
|---|---|
| `initialize` | store and scope bind to the profile it is handed: `<profile>/weft/weft.sqlite3` |
| `queue_prefetch` / `prefetch` | recall on a worker thread; the block lands on the next turn (measured ~30 ms) |
| `sync_turn` | both sides of the turn are ingested under the session's scope |
| `handle_tool_call` | `weft_recall`, `weft_ingest`, `weft_forget`, `weft_receipt` |
| `recall_status` | count and glyph for the recall indicator |
| `shutdown` | flush and close |

Scope is deny-by-default and deterministic before any ranking runs. Recalled text is data: it cannot
widen scope, authorize a write, or alter the store.

## Licence

AGPL-3.0. The plugin is thin, but it imports the AGPL substrate, so the same licence applies.
