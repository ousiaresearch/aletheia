# Changelog

All notable changes to Aletheia are documented here.

## [2.0.0] — 2026-11-07

One name. Everything the product is called is now Aletheia: the import path (`import aletheia`), the
distribution (`ousia-aletheia` — the bare `aletheia` and `aletheia-memory` project names belong to
other people on PyPI, so the project is namespaced and the import is not), the CLI (`aletheia`,
`aletheia-server`), the environment variables
(`ALETHEIA_*`), the tool names (`aletheia_recall`, `aletheia_ingest`, `aletheia_forget`,
`aletheia_receipt`), the default store (`<profile>/aletheia/aletheia.sqlite3`) and the plugin repo.
The substrate was called Weft; that word is gone from the package, the docs, the manifests and the
fixtures, except for the pre-rename evidence under `docs/reviews/`, which quotes reviewers verbatim
and is left as it was written.

### Changed
- **The plugin lives in this repository.** `plugin.yaml` and the loader entry point sit at the root
  beside the package, so one repo is both the substrate and the Hermes plugin. The catalog install
  path is unchanged: `hermes plugins install ousiaresearch/aletheia`.
- **The recall block's tag matches the ingester's fence.** It emits `<aletheia-recall>`, which
  `Ingestor.ingest_turn` recognises, so a recalled block that arrives inside a user message is
  stripped and stored with attribution `system` (D3-5) instead of being recorded as the user's own
  speech.

### Added
- **Store migration on first run** (`aletheia/paths.py`): `<profile>/weft/weft.sqlite3` with its
  `-wal`/`-shm`, `<profile>/weft/provider-config.json`, and the CLI's `~/.weft/weft.sqlite3` move to
  the Aletheia paths once. A store already at the new path wins; a legacy store that loses the race
  is left in place and never deleted; an unmovable legacy store is reported and startup continues
  on a fresh path rather than failing.
- **A hero card** (`docs/images/hero-card.jpg`, `hero.jpg`, and the `hero-card.html` source it is
  rendered from) in the house editorial standard.

### Tests
- `tests/test_paths_migration.py` (5 checks): the store move with its WAL, the new-store-wins case,
  the no-legacy-file case, the provider config move, and the CLI store move.
- Media smoke fixtures regenerated with the new name; the VLM and Whisper integration tests read
  the new fixture text. Suite: **1051**.

## [1.1.1] — 2026-11-07

### Fixed
- **Every turn was dropped by the Hermes provider.** `Ingestor.ingest_turn` derived the attribution
  from the speaker name and refused anything it did not recognise, while the provider passed the
  agent half as `aletheia` and the human half as whatever the channel called the speaker (an iMessage
  handle, a display name). Each turn raised `unknown speaker` and was lost; the store stayed empty and
  recall had nothing to return. Attribution is now the role (`user` / `agent` / `system`), resolved
  from an explicit role first and the speaker's own role name second, with the name kept verbatim in
  `provenance.speaker`. Existing `user` / `assistant` / `system` callers behave as before, and the
  provider now passes both halves explicitly.
- **Ambiguity resolves downward.** A speaker the store does not recognise is recorded as the agent
  side, never as the user: `user` attribution is authority-bearing (D2-2), so the fallback must not
  hand it out. Callers that know a turn is the human's pass `attribution="user"`.
- **The agent's own tool can no longer mint `user` records.** `aletheia_ingest` records with
  `attribution="agent"` whatever speaker it is given; only the turn mirror can write the human side.
- **One half of a turn could cost the other.** The two writes shared one `try`; a failure on the user
  half aborted the agent half. Each half is now independent, and the turn is counted by outcome.
- **A one-shot host closed the store under an in-flight write.** `sync_turn` writes on a worker
  thread, and `hermes chat -q` shuts the provider down seconds later: the turn was lost to
  `Cannot operate on a closed database`. `shutdown()` now refuses new turns before it drains, then
  joins in-flight writes for up to `SYNC_DRAIN_SECONDS` (5 s) before closing. The wait is bounded; a
  `Substrate.close()` queued behind a wedged write is not.
- **A turn arriving mid-shutdown, or a write registered before it started, could still be lost.**
  Acceptance and thread registration now happen under the provider lock, with the thread started
  before it is tracked; a turn offered after shutdown began is a counted skip (`provider closing`),
  not a doomed write.
- **Fenced content could be stored as the user's own speech.** A turn that arrives carrying recall
  fence markers is now stored with attribution `system` as well as `provenance.fenced` (D3-5).

### Added
- Turn bookkeeping in `aletheia_receipt`'s status: `turn_writes.{seen,skipped,stored,partial,failed,
  halves_written,halves_failed}`, `skipped_because` and `last_error`, plus `primary`. `seen` is
  turns; `halves_*` is halves. `stored + partial + failed + skipped == seen`, so a host that stopped
  reaching the write path is visible instead of silent. Note what `seen == 0` does and does not mean:
  the provider was never asked to write — which can be because the host produced no turn, or because
  the host dropped it upstream in its own drain or stall gate.
- An `INFO` line per provider instance naming its scope, store, primary flag and session, and a
  `WARNING` when it activates without an embedder (the failure worth debugging).

### Tests
- `tests/test_turn_persistence.py` (16 checks): role resolution, downward ambiguity, unknown-speaker
  storage, fencing, both-halves writes, handle-as-provenance, bot-authored turns, tool attribution,
  half-failure isolation, bounded drain, drain under a hung write, a turn arriving mid-shutdown,
  start-before-track, post-close accounting, non-primary accounting, receipt totals.
  Suite: 1014 → **1030**.

## [1.1.0] — 2026-11-06

### Added
- **Aletheia**: the Hermes memory provider now registers as `aletheia` (was `aletheia`), so the catalog
  entry, the manifest name and `memory.provider` are one word. The substrate keeps the name Aletheia, and
  the tools keep their `aletheia_*` names because they operate on the substrate.
- `aletheia/` replaces `hermes-plugin/` as the plugin directory the catalog pins.

### Changed
- No behaviour change: same store path (`<profile>/aletheia/aletheia.sqlite3`), same scope derivation, same
  recall path and receipts. Only the names a reader types.

## [1.0.0] — 2026-11-06

The substrate and the Hermes provider surface are stable; the operational evidence is still thin,
which is what the beta classifier is for.

### Added
- **Hermes memory provider** (`aletheia/providers/hermes.py`, `hermes-plugin/`): in-process memory for
  Hermes agents. The store and scope bind to the profile the manager hands in; recall runs on a worker
  thread so the turn's prefetch is free (measured: 30 ms for hybrid recall, 8.6 s to load models at
  startup); every recall carries a receipt; tools `aletheia_recall`, `aletheia_ingest`, `aletheia_forget`,
  `aletheia_receipt`.
- Turnkey deployment (Dockerfile, compose, HTTP tool routes).
- Procedural memory with a deny-by-default promotion gate.
- Graph and temporal reasoning: entity links in SQLite, date extraction, temporal boosting.

### Changed
- Schema v7 substrate hardening with Round 2–9 integrity gates.
- Retrieval quality is part of the contract: the local embedder and cross-encoder reranker are always
  on, and a missing model is reported rather than replaced by a keyword fallback.
- Test suite: 429 checks at 0.1.0a0 → **1014** at this commit.
- Stable version numbering: the dependency spec is now `aletheia>=1.0,<2`, so a plain
  `pip install aletheia` resolves it (a pre-release would not have).

### Fixed
- Authentication: actor derived from the bearer token, middleware placement, `/health` and tool routes
  authenticated, client-supplied actor removed.
- Packaging: `aletheia.providers` is declared in `pyproject.toml` (the first a1 wheel silently omitted it),
  with a test that fails whenever a package directory is undeclared. Built wheels are no longer tracked.

### Security
- Client-controlled actor removed from every write path.
- Remaining phase-14 auth surface gaps closed and regression-tested.

## [0.1.0a1] — 2026-11-06

The alpha that carries the hardening: 109 commits since a0, and the first release with a Hermes
memory-provider surface.

### Added
- **Hermes memory provider** (`aletheia/providers/hermes.py`, `hermes-plugin/`): Aletheia runs in-process as
  an external memory provider — turns are ingested per scope, recall is served from a worker thread so
  the turn's prefetch stays free, and recall reports a receipt. The provider imports the substrate
  only: an import-graph test fails if it ever pulls in the FastAPI surfaces, and a subprocess test
  asserts the class is a concrete `MemoryProvider` against the installed Hermes.
- **Turnkey deployment**: Dockerfile, compose file and HTTP tool routes.
- **Procedural memory** with a deny-by-default promotion gate.
- **Graph and temporal reasoning**: entity links stored in SQLite, date extraction from records, and
  temporal boosting for time-shaped queries.

### Changed
- **Schema v7 substrate hardening** with Round 2–9 integrity gates.
- **Retrieval quality** is part of the contract, not an option: the local embedder and the
  cross-encoder reranker are always on, and a missing model is reported rather than worked around with
  a keyword fallback.
- Test suite: 429 checks at a0 to **1011** at this commit.

### Fixed
- Authentication: the actor is derived from the bearer token, never from a client-supplied field;
  auth moved to middleware; `/health` and the tool routes require it.
- FastAPI `Request` typing and smoke coverage for the container image.

### Security
- Client-controlled actor removed from every write path.
- Remaining auth surface gaps from the phase-14 review closed and regression-tested.

## [0.1.0a0] — 2026-10-29

### Added
- Phase 1–6 implementation: substrate, scope engine, ingestion, retrieval, reasoning loop, server, shadow mode, logging.
- 429 regression tests covering scope, invariants, fault injection, deletion, export/import, retention, server endpoints, shadow mode, logging redaction, and evaluation.
- Evaluation set v1 with 6 gold cases for grounded synthesis, abstention, contradiction detection, cross-scope leak, content-conflict denial, and correction lineage.
- Adversarial review cycle with Codex; all Phase 1–6 findings repaired and regression-tested.

### Changed
- README updated to reflect Phase 7 gated cutover and current test count.
- Plan document updated to mark Phases 1–6 closed.

### Fixed
- Phase 6 adversarial review findings: scope allowlist via `ALETHEIA_KNOWN_SCOPES`, `import_scope` validation, `get_at` effective_at guard, logging regex redaction.

### Security
- Content-conflict detection prevents mis-filed records from being read across scope boundaries.
- Retrieved content is never re-ingested; recall output is assembled at call time.
- Fail-closed on unreachable policy engine, malformed extraction, and failed model calls.

### License
- AGPL-3.0. See `LICENSE`.
