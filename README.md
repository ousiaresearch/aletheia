# Weft

Evidence-bound memory substrate for people and their agents.

Weft ingests conversations, documents, and media into a scoped, auditable store. Recall is scoped first, ranked second, and explained in a receipt. Deletions cascade. Corrections preserve lineage. Nothing is injected whole.

## Status

1.0. The substrate, scope engine, ingestion, retrieval, reasoning loop, server and the Hermes memory provider are implemented and reviewed through phase 14. The version marks an API that no longer moves under adopters; it is not a claim that the system has been operated at scale — single-user on macOS is what has actually been run, with the phase-8 through phase-14 hardening in this release. `scripts/test.sh` runs the current suite: 1011 checks. A Hermes memory-provider surface ships in `weft/providers/hermes.py`. AGPL-3.0.
**Published:** `weft-memory==1.0.0` on PyPI (https://pypi.org/project/weft-memory/1.0.0/).

## Boundaries

- Weft is not a brain, a consciousness, or a proof of either.
- It does not post, message, or commit on its own.
- Recall is data, not instructions. Retrieved text cannot authorize writes or alter scope.
- Scope is deterministic and deny-by-default. Identity grants nothing.

## Install

### From PyPI (alpha)

```bash
pip install weft-memory==0.1.0a0
```

### From source

```bash
git clone https://github.com/ousiaresearch/weft.git
cd weft
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[server]"
```

Python >= 3.11 required. The `server` extra installs the HTTP server only; it does not install
sentence-transformers or any embedder. The default local embedder (an MLX BGE-M3 snapshot,
`local-embedding` extra) runs on darwin/arm64 only.

## External extraction (opt-in)

`weft extract` sends scope text to the third-party TokenHarbor endpoint
(`tokenharbor.ai`). It is off by default: pass `--allow-external` (or set
`WEFT_EXTRACT_ALLOW_EXTERNAL=1`) and provide `TOKENHARBOR_API_KEY`. The host is
printed to stderr before sending. The CLI and HTTP server make no other external calls. Library callers that pass a `token_getter` to `recall` enable query expansion, which sends query text to the same host.

## Quick start

`ingest` and hybrid `recall` need the local embedder (darwin/arm64, see above).

```bash
weft --db ~/.weft/weft.sqlite3 ingest --scope coast --file note.md
weft --db ~/.weft/weft.sqlite3 recall --scope coast --query "tides"
weft --db ~/.weft/weft.sqlite3 rebuild-fts --scope coast
```

To serve over HTTP, `WEFT_API_KEY` and `WEFT_KNOWN_SCOPES` **must be set**; the server fails
closed, so without them every request returns 401 (no or wrong key) or 403 (scope not
allowed):

```bash
export WEFT_API_KEY='replace-with-a-long-random-secret'
export WEFT_KNOWN_SCOPES='coast'
weft --db ~/.weft/weft.sqlite3 serve --host 127.0.0.1 --port 8765
```

HTTP recall is lexical-only. The Docker image and Compose file are **unverified**: Docker was
not available to build or run them. See `docs/turnkey-deployment.md`.

Default database: `~/.weft/weft.sqlite3` (owner-only, 0600).

Deletion commits logical erasure immediately, but deleted text can remain in the live `-wal` file until a checkpoint completes, and backups, exports and snapshots are never erased. See "Deletion and erasure" in `docs/turnkey-deployment.md`.

## Models

Weft ships adapters, not weights. Aletheia requires Apple Silicon macOS, the MLX BGE-M3
embedder and the cross-encoder reranker; it does not fall back to keyword-only retrieval.
In the **same Python environment as Hermes**, install Weft's model dependencies and download
the models while online (review the model cards and licences before downloading):

```bash
python -m pip install 'weft-memory[local-embedding,local-recall]'
python -c "from huggingface_hub import snapshot_download; snapshot_download('mlx-community/bge-m3-mlx-4bit'); snapshot_download('cross-encoder/ms-marco-MiniLM-L-6-v2')"
```

The `local-recall` extra requires `sentence-transformers>=3.0` for local-only cross-encoder loading.

Model cards: [BGE-M3 MLX](https://huggingface.co/mlx-community/bge-m3-mlx-4bit) and
[MiniLM reranker](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2). The embedder
currently requires the default `~/.cache/huggingface/hub` location, so do not override `HF_HOME`
or `HF_HUB_CACHE` for its download. The reranker uses the Hugging Face cache configuration.
Restart the gateway after setup. Provider readiness loads and probes both models locally; this can
take time and memory on startup. There is no `weft models fetch` command or built-in licence
consent flow. Optional media models are separate.

## Design invariants

1. Scope before relevance.
2. Deny by default.
3. Content, not location, sets scope.
4. Identity grants no scope.
5. Cross-scope needs user-attributable request.
6. Composite artifacts never injectable.
7. Status transitions gated (verified requires Verification record).
8. User attribution for Decision/Commitment.
9. Correction supersedes, keeps lineage.
10. Recall is not re-ingested.
11. Authority is singular (Substrate is only authority).
12. Fail closed.
13. Retrieved content is data, not instructions.

## License

AGPL-3.0. See `LICENSE`.

Third-party dependency licences are listed in `docs/clean-room-provenance.md`. Arsumbris, Honcho, and OpenViking are study-only under clean-room protocol; no source copied or bundled.