# Changelog

All notable changes to Weft are documented here.

## [0.1.0a1] — 2026-11-06

The alpha that carries the hardening: 109 commits since a0, and the first release with a Hermes
memory-provider surface.

### Added
- **Hermes memory provider** (`weft/providers/hermes.py`, `hermes-plugin/`): Weft runs in-process as
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
- Phase 6 adversarial review findings: scope allowlist via `WEFT_KNOWN_SCOPES`, `import_scope` validation, `get_at` effective_at guard, logging regex redaction.

### Security
- Content-conflict detection prevents mis-filed records from being read across scope boundaries.
- Retrieved content is never re-ingested; recall output is assembled at call time.
- Fail-closed on unreachable policy engine, malformed extraction, and failed model calls.

### License
- AGPL-3.0. See `LICENSE`.
