# Changelog

All notable changes to Aletheia are documented here.

## [Unreleased]

### Added
- **`aletheia/graph.py` — typed relations, scoped and time-bounded (phase 12 / W2.1).** A
  `relations` table (schema version 8) stores subject → predicate → object where both endpoints are
  `record_id` values, so an edge is authorised the same way a record is. `entity_links` answers
  "which record mentions this string"; this answers "which records are related, and how".
  - Writing requires the scope to be granted and readable *and* both endpoints to exist in it, so a
    cross-scope edge is not expressible: declaring the edge in `shared` does not join an `alpha`
    record to a `beta` one.
  - A predicate must come from the closed `PREDICATES` set; confidence is bounded 0–1; a window
    whose `valid_to` precedes `valid_from` is refused at construction.
  - Reading is filtered by the edge's scope at every hop, and each far endpoint must be readable in
    its own scope — an edge is not a permission. Measured: a caller granted only `alpha` cannot walk
    `alpha → shared → beta`.
  - `neighbours()` is bounded breadth-first: shortest distance, hop and node limits, closed windows
    invisible without being deleted.
  - `expand_candidates()` reaches neighbours of the top matches in a stable order (seed, then
    distance, then id) and recall uses it as a **context** layer: graph-reached records never
    displace a matching record, are capped at ten, and are marked `neighbor` like adjacency
    context. The receipt names the layer `graph_expand`, so "why is this here" has an answer.
  - `extract_relations()` is rule-based and deliberately small: a candidate predicate comes from a
    fixed pattern list and an edge is drawn only for a **citation the substrate can verify** — a
    record id present in the citing record's text. Nothing is inferred from a word alone, and every
    edge names its `source_ids`.
  - **Communities** are a projection, not authority: `rebuild_communities()` runs label propagation
    inside one scope, visiting nodes in sorted order and breaking ties toward the smaller label, so
    two runs over the same edges produce the same partition — a partition nobody can reproduce is
    not auditable. Only that scope's edges are read and only its records are labelled; the rebuild
    replaces the previous projection; membership is derived state that can be thrown away.
  - Schema version 9 adds `communities` (8 added `relations`). Migration from v6 or v7 lands on 9 and
    both tables are rebuilt from records rather than trusted from an export bundle
    (`_IMPORTABLE_SCHEMA_VERSIONS` lists 6–9, and a future version is still refused).
  - Operator surface: `aletheia relate --scope <s>` draws relations from verified citations,
    `aletheia graph --scope <s> --rebuild-communities` recomputes the projection, and
    `aletheia graph --scope <s> --record-id <id>` shows that record's community, neighbours and
    relations. Without these the graph would be a table nothing fills.
- **`aletheia/representation.py` — a cited card of what a scope holds (phase 12 / W2.2).** The plan
  asked for a stored `Representation` record type. This deliberately stores nothing: a snapshot goes
  stale without anything failing, cannot cite the records it was built from once they change, and
  reading it is indistinguishable from reading an asserted fact. The card is a projection assembled
  on demand from one scope — architecture rule 4 applied literally — with every line carrying the
  `record_id` and `source_ids` it came from (`Card.record_ids()` returns the set a caller can check
  it against).
  - Contents are rule-based and boring: stated `Commitment` / `Decision` lines with their status,
    `Claim`s above a confidence floor, relation counts by predicate, community sizes, and the most
    recent record ids. Nothing infers a personality, a mood or a preference — that would be
    interpretation dressed as data, and the first reader of a wrong interpretation would be a model
    that then acted on it.
  - A record superseded by a `Correction` is history, not a fact: excluded from the card and counted
    under `counts.superseded`, so a corrected claim never appears twice as equally current.
  - An ungranted scope yields an empty card, and building twice stores nothing (asserted by
    comparing the record count before and after).
  - `aletheia representation --scope <s> [--limit N]` prints the card as JSON.
- **`aletheia/consolidation.py` — near-duplicates found and marked, never deleted (phase 12 / W2.3).**
  `consolidate_scope` used to report exact-text duplicates in a comment that admitted "production
  would use embeddings". This replaces it with clustering that is lexical by default and
  embedding-based when an embedder is passed, and it *marks* what it finds with a `duplicate_of`
  edge instead of counting it.
  - Nothing is deleted, and nothing is superseded automatically: a `Correction` requires user
    attribution, so an agent may mark a duplicate but may not rewrite what a person asserted. A test
    asserts consolidation writes no `Correction` and leaves `supersedes` untouched — that failure
    would be an agent laundering away a stated fact.
  - The keeper is chosen deterministically — strongest confidence, then newest, then smallest id —
    and grouping does not depend on row order. Two runs give the same answer, including the last
    tiebreak.
  - Clustering is per scope in both directions: an ungranted scope yields nothing, and two scopes
    with the same sentence are not duplicates of each other.
  - `aletheia consolidate --scope <s> [--threshold T] [--mark] [--dry-run]`; the default is to
    report, because a threshold that is wrong for a corpus is not obvious until you have seen what
    it would have marked.
  - Recall uses the marking: a marked duplicate yields its match slot to its keeper and is returned
    as context, so both are visible and only one is treated as a match.
- **`aletheia/retention.py` — windows as policy, purging through the erasure path (W2.6, slice 1).**
  Schema version 10 adds `retention_policy` (one window per scope) and `retention_marks` (per
  record). Deliberately side tables: an `expires_at` column on the record would make a policy change
  a rewrite of history, and the record is the thing this substrate promises not to rewrite.
  - Expiry is measured from a record's creation, not from when the policy was set, so a window
    applied today does not silently extend the life of everything already stored.
  - An existing mark is not moved by a later, shorter window unless `--overwrite` is passed.
  - Expired records leave recall **before** they are deleted, and the receipt names the exclusion.
    Hiding is reversible and free; deleting is neither.
  - The sweep deletes through `Substrate.delete()`, the one path that cascades derived artifacts,
    scrubs the index and WAL, and journals itself — asserted with a `Summary` citing the purged
    record. `aletheia retention --sweep` reports unless `--confirm` is given.
  - `aletheia retention --scope <s> [--set-days N] [--apply] [--sweep] [--confirm] [--overwrite]`.

### Fixed
- **The provider's runtime requirements are declared where they are installed from.** `mlx`,
  `mlx-embeddings` and `sentence-transformers` moved from `[project.optional-dependencies]` into
  `[project.dependencies]`, each marked `sys_platform == 'darwin' and platform_machine == 'arm64'`.
  Hermes installs a plugin from its declaration — `pyproject.toml` when one ships, `plugin.yaml`'s
  `python_dependencies` only when it does not — so a requirement left in an extra is never installed
  when the environment is rebuilt. The gateway then started normally, reported the memory provider
  unavailable, and external memory was off for every session with nothing failing.
  Verified by resolution, not by assertion, and the result is platform-dependent: with
  `uv pip install --dry-run --python-platform linux`, `… --python-platform windows` and
  `… --python-platform x86_64-apple-darwin`, the package resolves 7 packages with no torch, no mlx
  and no CUDA wheels. The Apple-Silicon set is the opposite — 91 packages including torch,
  sentence-transformers and mlx-vlm — because that is the platform the provider actually runs on.
  uv cannot dry-run `aarch64-apple-darwin` for this project at all: it assumes a macOS 13.0
  deployment target and `mlx==0.32.4` ships 14.0+ wheels. What is **not** verified is a fresh
  environment built from this declaration: none has been rebuilt since the edit, and the checker
  below exists so the next rebuild is judged rather than trusted.
- The `local-embedding` and `local-recall` extras are no longer described as aliases. `local-recall`
  now carries the same `>=3.0,<6` pin as core (it used to pin `sentence-transformers` unbounded plus
  a separate unbounded `torch`), and it is the documented route to the reranker off Apple Silicon.
  Asking for the reranker without it now raises an error naming the extra instead of a bare
  `ModuleNotFoundError` from inside a recall transaction (`aletheia/reranking.py`).
- `plugin.yaml` mirrors the three pins and says why it is a mirror rather than the install source.
  It also gained the `mlx` requirement it was missing.

### Added
- `scripts/check_runtime_deps.py` — imports the required modules in the interpreter of the
  environment the newest `installs/*/facts.json` names (isolated mode, scratch cwd, so neither
  `PYTHONPATH` nor a directory in the caller's cwd can answer for an install), names what is missing
  with its import error, and prints a `uv pip install --python …` repair that works in an
  environment without pip. `--deep` is a different question and a different environment: it puts the
  plugin source that environment would import on `PYTHONPATH` and loads the embedder and reranker
  there, naming which copy it used. Exit codes: 0 healthy, 1 missing or not ready, 2 unknown —
  including "the provider does not run on this platform", where nothing was checked.
- `aletheia/runtime_deps.py` — the requirement set as data, judged as three separate questions:
  the declaration (parsed with `packaging`, markers evaluated for the *target* platform, so a
  `win32`-marked requirement counts as undeclared on arm64; a marker reading `os_name` or
  `platform_release` is reported unknown rather than answered from the checker's own interpreter),
  presence (`import`, not `find_spec`, which cannot see a wrong-arch wheel, a broken `__init__` or a
  namespace directory — all three now report absent), and the resolved environment (`Environment`
  carries a note when `facts.json` names one venv and resolves the lock in another — they have
  disagreed on this machine, and only one of them had the models). A probe that answers about fewer
  modules than it was asked about counts the rest missing, and an empty requirement set (any
  non-Apple-Silicon platform) is reported as not judged rather than as satisfied.
- `packaging>=23` as a core dependency, for reading the declaration correctly rather than by hand.

### Changed
- `scripts/deploy_local.sh` replaces the *resolved* environment's plugin-source member from the
  staged plugin directory and re-installs its editable distribution (`uv pip install --reinstall -e`),
  instead of only clearing snapshots. Clearing them left the gateway importing a copy that had been
  deleted, and left the environment's dist metadata at whatever version it was last installed at —
  which is why an environment running 2.1.0 source reported `ousia-aletheia 2.0.1`. Retired
  environments' members are still removed. Dry run unchanged: the default prints every action.
- `scripts/verify_deployed.py` has three verdicts instead of two. `drift` (exit 1) covers disagreeing
  copies, a missing requirement, and a declaration that does not install one; `unknown` (exit 2)
  covers an environment or interpreter it could not inspect, a declaration it could not read, and a
  platform the provider cannot run on; `no drift` (exit 0) requires all of it to be answerable and
  answered. The earlier version printed "no drift" for a venv with no interpreter in it, which is the
  same silence as the bug. It also loads `runtime_deps` by path, so a report about one checkout
  cannot be answered by another.
- **The installed distribution was invisible to that check.** `_dist_versions` globbed
  `aletheia-*.dist-info`, but this project installs as `ousia_aletheia-*.dist-info`, so an
  environment holding 2.0.1 against a 2.1.0 checkout reported "no drift" with the dist copy simply
  absent from the report. It now matches either name and filters on the `Name:` in METADATA.
- Two copy classes are printed but not counted as drift: a copy in an environment `facts.json` does
  not resolve to (a retired environment left on disk), and the dist-info of an **editable** install,
  whose version is the one it was installed at while the code comes from the source tree its finder
  maps to — which the `plugin_sources` entry already judges. Both are marked `-` in the report. A
  check that fails on stale metadata forever is a check nobody reads.
- A namespace directory no longer counts as an install, but a namespace package that is really
  installed still does. `mlx` legitimately imports as a namespace directory holding extension
  modules (`mlx/core*.so`), so "no `__file__`" alone would have reported the live environment as
  missing its embedder; the probe now imports a canary submodule (`mlx.core`,
  `mlx_embeddings.utils`) before calling a namespace directory empty.

### Tests
- `tests/test_runtime_deps.py` — 42 tests, including the cases two rounds of adversarial review
  found missing: a requirement marked for another platform counts as undeclared; a URL-pinned
  requirement is parsed rather than misread as absent; the probe reads an import error instead of a
  spec lookup, survives non-UTF8 output, ignores banner noise, and cannot be answered by a folder in
  the caller's cwd; the repair command works in an environment without pip; the checker's exit codes
  are asserted through `main()` with a stand-in interpreter (0 present, 1 missing, 2 unprobeable);
  `verify_deployed` is exercised end to end rather than monkeypatched, uses the repository it was
  given, and treats an unloadable checker or an unresolvable environment as unknown rather than
  clean.

### Documentation
- README named the wrong distribution (`pip install aletheia` is someone else's package) and pointed
  at extras that no longer carry the requirement. It now names `ousia-aletheia`, says why the
  requirements are dependencies, records that `mlx` needs macOS 14+, states that the checker scripts
  ship with the repository rather than the wheel, and documents three exit codes instead of two.

## [2.1.0] — 2026-11-08

### Added
- **One host store, many scopes.** The provider now opens
  `<hermes_root>/aletheia/aletheia.sqlite3` for every profile and writes under `profile:<name>`.
  Profiles are scopes inside one substrate, so a multiplexed gateway no longer runs N databases
  that can never see each other (`aletheia/profile_identity.py`).
- **Host-policy read grants.** `<hermes_root>/aletheia/policy.json` (0600, owner-checked) names which
  profiles may read which scopes and who may use `shared` / write to it. A profile's own
  `provider-config.json` can only *request* a grant; the policy decides. Reads run under a read
  context, writes keep their own, so a read grant never confers write authority, and a configured
  `scope` is ignored — identity is derived from the profile, never configured.
- **`aletheia merge-store`** — copy one scope from another store into this one under a new scope.
  The source is opened read-only and never migrated; the copy is additive (an existing id with
  different content is refused by name, never overwritten); records the target's rules reject are
  reported by id instead of zeroing the bundle; the result asserts
  `exported = imported + present + skipped`.
- **`aletheia rename-scope`** — rename a scope in place, moving records, journal, entity links, jobs,
  the FTS index and the scope-keyed vector tables in one transaction. Receipts are not rewritten:
  they record what was consulted at the time. Refuses to union two live scopes.
- **`scripts/verify_deployed.py`** — reports the version in the repository, the profile's plugin
  directory, every plugin-source snapshot and every installed distribution, and exits non-zero on
  drift. This exists because the gateway was importing a 2.0.0 snapshot while the repo said 2.0.1.

### Changed
- **Identity comes from `agent_identity`.** Hermes passes the profile's own name
  (`get_active_profile_name()`); the provider now uses it instead of the home directory's basename.
  The default profile's scope was `profile-.hermes` and is now `profile:default`.
- **`ScopeEngine.known_scopes` is the store's scope set**, not just the instance's own scope. A
  shared store must register every scope or a grant is defeated by the unknown-scope rule.
- **`hermes memory status` explains itself.** When the CLI interpreter cannot load the provider's
  models, the status line names the missing module and says it may exist only in the gateway
  environment, instead of reading as an outage.
- Invalid `provider-config.json` values are normalised on save; `db_path` is still accepted as the
  pre-2.1 name for `store_path`.

### Migration
- Nothing moves on startup. Records still carrying the old basename scope (`profile-.hermes`) are
  reported with their count and the exact command; the operator runs
  `aletheia rename-scope --from profile-.hermes --to profile:default`. A rename decided from a path,
  applied to a shared store, is how one profile silently absorbs another's memory.
- Per-profile stores written before this release are copied in explicitly with `aletheia merge-store
  --from-db <profile store> --from-scope <old scope> --to-scope profile:<name>`.
- Both commands exit non-zero when they moved nothing, so a typo'd scope cannot read as success.

### Tests
- `tests/test_profile_identity.py`, `tests/test_shared_store.py`, `tests/test_merge_store.py`,
  `tests/test_hermes_multiprofile.py`, `tests/test_deploy_truth.py`.

## [2.0.1] — 2026-11-08

### Added
- Provider-agnostic LLM transport for the package's optional extraction and query-expansion paths:
  native OpenAI, native Anthropic, and OpenAI-compatible endpoints with user-selected provider,
  model, base URL, and API-key environment-variable name.
- Fail-closed handling for missing or invalid configuration, with API keys excluded from config reprs.

### Changed
- Optional extraction and query expansion no longer select TokenHarbor or any other provider by
  default. Extraction still requires explicit egress opt-in; query expansion remains separately
  opt-in. The Hermes plugin does not make background LLM calls as part of this release.
- Documented provider setup and the fact that Aletheia does not read Hermes auth files directly.

### Verified
- Mocked transport tests exercise OpenAI, Anthropic, compatible endpoints, disabled integrations,
  and secret redaction. No live provider endpoint was called.

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
- **A hero card** (`docs/images/hero-card.jpg`, `hero.jpg`) in the house imprint standard: the scene
  is generated with Codex image generation from the Nous branding lane (analog newspaper direct
  response — halftone, paper tooth, one red signal ink), and the copy is typeset deterministically
  over it as two plates multiplied together, so the product name is never model-improvised. Prompt
  and typeset source live in `docs/images/source/`.

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
