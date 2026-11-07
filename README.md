# Aletheia

Evidence-bound scoped memory for Hermes agents. Aletheia is the memory-provider surface over the
**Weft** substrate: turns are written to a local SQLite store inside the active profile, recall is
**scoped before it is ranked**, and every recall returns a receipt naming the scope consulted, the ids
selected and the tier.

This repository is the plugin. The substrate ships as the [`weft-memory`](https://pypi.org/project/weft-memory/)
package (AGPL-3.0), so the plugin here is only a manifest and an entry point.

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

Retrieval quality is part of the contract, so Aletheia does not fall back to keyword search. It needs:

- the `bge-m3` embedder (~640 MB) and the `cross-encoder/ms-marco-MiniLM-L-6-v2` reranker (~90 MB),
  fetched once at setup from their official Hugging Face repositories;
- `weft-memory` (declared as a Python dependency), `sentence-transformers`, and on Apple silicon
  `mlx-embeddings`.

Media ingest is optional: with Qwen3.5-2B (2.5 GB) and Whisper large-v3-turbo (547 MB) it accepts
images and audio; without them it refuses those types by name and text keeps working fully.

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
