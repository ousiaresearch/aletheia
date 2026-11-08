# Phase 14 round 3: substrate semantics

- **Approval is structured.** A user Decision/Commitment approves a ProposedRule only when its
  payload is exactly `{"rule_id": "<rule>", "approved": true}`. Naming a rule (`source_ids`,
  `applies_to`, prose, `approved: false`, extra fields) creates no binding. Rewriting the record
  as a rejection removes the binding.
- **Revocation is durable.** Superseding a Decision/Commitment (`correct()` or an imported
  Correction) deletes its approval bindings. Deleting or overwriting the Correction later does not
  restore them; a new approval record is the only way back.
- **Lifecycle transitions** (reject, deprecate, activate) need a user-authored context. They may
  change `status`, `promoted_by`, `promoted_at`, `rejection_reason`, `deprecated_at` and nothing
  else, through `put()` and import alike. Activation additionally needs a valid bound approval.
- **Record validation** (put, `validate_schema=False`, import): `payload.text` is a string;
  `created_at` is a finite number; `effective_at`/`valid_from`/`valid_to` are finite numbers or
  null (no bool, no |int| >= 2^53); `valid_from <= valid_to`.
- **Citations** must resolve to a record in the citing record's own scope, for every record type.
  A missing id and a foreign id are the same refusal. Pre-existing foreign citations never block
  deletion (the delete scans only the deleted record's scope).
- **Import** orders by `source_ids` and `supersedes`, so export order does not matter. A refusal is
  atomic and reported: `last_import_refusal` holds the reason, `strict=True` raises
  `KnowledgeRefusal(reason)`. Malformed bundles raise `SchemaViolation`. Schema 6 and 7 bundles
  are accepted. **Approvals are never imported:** a restored proposed rule needs a fresh approval;
  a bundle containing an active rule is refused.
- **Delete** cascades transitively through derived Summary/Observation/Claim/Hypothesis/Reasoning
  records, removing FTS rows, vectors, entity links and supersedes links. A retained same-scope
  record that cites a removed one (e.g. a verified Claim) still refuses the delete.
- **Schema 7 migration** (stores written by schema 6/HEAD): every ProposedRule without an
  incarnation gets one; formerly active rules return to `proposed` (promotion fields removed),
  are journalled (`migrate_rule_reapproval`) and listed
  in `Substrate.migration_reapproval`. Old Decisions are never trusted. Failure rolls back and
  leaves `user_version` at 6.

- **Index cost.** `fts_rowmap(record_id, fts_rowid)` (derived, created at open, backfilled once per
  store, marker `meta.fts_rowmap_ready`) lets a write replace or remove a record's fts row by rowid;
  the delete re-checks `record_id`, so a stale hint cannot remove another record's row. The
  completeness check is linear (one pass over `fts`). `fts_candidates` keeps the last in-memory
  BM25 corpus, keyed on store version, `data_version`, an fts epoch, requested scopes, the caller's
  grants, known scopes, allowlist and markers; any rollback drops it. BM25 statistics still come
  only from the caller's readable rows of the requested scopes.
- **Vector completeness** requires a vector only for `EMBEDDABLE_TYPES` (Source, Observation,
  Claim). Other types (ProposedRule, Decision, Commitment, ...) are lexical-only but still need a
  valid fts projection.
- **Orphans** are attributed by the fts row's own scope column, never by `known_scopes` (an HTTP
  request's engine knows only its own scope): an orphan naming another scope does not affect this
  scope; an orphan naming the checked scope, or naming no scope (NULL), fails closed.


## Round 5 (R4 findings)

- **Migration order.** On a store already at schema >= 6, `fts_rowmap` is backfilled *before* the
  v7 migration reindexes anything, so the reindex replaces the old fts row instead of adding a
  second one. The migration re-reads `user_version` inside its write transaction, so a second opener
  that lost the race does not rerun it (which would re-token approvals made since). `meta.schema_version`
  is advanced. The regression test builds a real v6 layout by hand (no rowmap, no rule tables).
- **Migration re-approval.** Every pre-v7 `rule_approvals` row is dropped and every ProposedRule
  gets a fresh incarnation token, whether or not it already had binding rows; formerly active rules
  return to `proposed`. Those rows came from the unversioned implementation and are not trusted.
- **prune_derived** selects `epistemic_status='derived'` (never verified), skips a record a verified
  record or active rule relies on or whose removal a retained record would observe, and applies
  `delete()`'s transitive lineage cascade (shared `_lineage`): dependents, their fts rows, vectors,
  entity links and jobs go too. Returns records removed (pruned plus cascaded).
- **Vectors after a correction.** A record superseded by a Correction loses its vectors and is
  exempt from `vector_index_complete`. `aletheia extract` now embeds the Claims it writes.
- **Concurrent first open.** `PRAGMA journal_mode=WAL` and the schema script retry
  `database is locked` up to the busy timeout.
- **Import idempotence.** A bundle record identical (as serialized) to the stored one is skipped, so
  a bundle can be re-applied to a store holding some of its records. Anything that differs still goes
  through the unchanged policy: rules and Corrections refuse, verified/relied-on records are
  protected, approvals and active rules are never restored.


## Round 6 (R5 findings)

- **Temporal read gate.** `effective_at` requires a grant for the scope and returns only what
  `Substrate.query` (the read gate) lets the caller read; no context or no grant returns `[]`.
  `temporal_boost` scores only readable candidates. `close_window` already went through `get`/`put`.
  `set_validity`, `extract_dates`, `is_temporal_query` and `apply_temporal_metadata` take no store.
- **Denied correction.** `correct()` checks the replacement-id collision (scope grant, write and read
  gate) before the original rule is deprecated or any index row is touched; a denial commits only
  its journal entry.
- **Import and rules.** An existing rule is never transitioned by import: identical content is
  skipped, any difference (status included) refuses the whole bundle. A new id restores only as
  `proposed`, or as an inert `rejected`/`deprecated` rule exactly as exported. An `active` rule is
  refused with a message that says so. A Correction whose target is a live (proposed/active) local
  rule is refused, since it would deprecate that rule. Approvals are still never imported.
- **Vectors after a correction.** `correct()` (and import) keep the superseded record's vectors: its
  text is unchanged, and deleting the Correction makes it current again. The completeness exemption
  for superseded records remains for stores corrected by earlier code.
- **Quarantine.** The v7 migration moves rows that fail the record integrity rules (non-string
  payload text, non-finite timestamps, bad JSON shape) into `quarantined_records` (untyped columns,
  values preserved), drops their fts/vector/entity-link rows, journals each, logs a warning and lists
  them in `Substrate.migration_quarantined`. No read path sees them. Limit: only the migration
  scans; a store already at schema 7 is not re-checked.
- **Re-embedding.** `Substrate.reembed_missing(scope, ctx, embedder, limit=None)` embeds readable
  Source/Observation/Claim records lacking a vector for the embedder's model (write grant needed,
  text bounded to 2000 chars, idempotent). CLI: `aletheia --db PATH reembed --scope S [--limit N]`.

## Round 7 (Sonnet R6-03..07)

- **Quarantine is erasable (R6-03).** `delete(id)` removes the quarantined copy of that id under the
  same `authorize_delete` rule as a live row (also when a live row of the same id exists);
  `prune_derived` removes quarantined `derived` rows past the TTL; `list_quarantined(ctx)` (id, scope,
  type, reason; no payload; readable scopes only) and `purge_quarantined(ctx, scope=, record_id=)` list
  and erase them. `PRAGMA secure_delete=ON` is set so freed pages do not keep the plaintext.
- **Text-less records (R6-04).** `vector_index_complete` requires a vector only for embeddable records
  with non-blank string payload text (`_embed_text`), the same predicate `reembed_missing` uses.
- **Correct, then erase the original (R6-05).** Superseded by round 8 below (the round-7 `erased_supersedes` tombstone is gone).
- **Migration/import (R6-06).** v7 also deprecates a `proposed` legacy rule that a Correction supersedes.
  Importing a bundle that restores a live rule together with a Correction of it now says so.
- **Stale vector (R6-07).** `reembed_missing` passes the text hash it embedded to `vector_put`, which
  re-checks it inside the write transaction; a changed/deleted record is skipped and counted as
  `skipped_changed`.

Residual limits: the quarantine
purge does not cascade through lineage; secure_delete does not scrub OS/backup copies; no Docker
or concurrent multi-process test, the race is simulated in-process.

## Round 8 (Terra/Sonnet R7)

- **No id of an erased original is stored (R7-01/02/03, Sonnet R7-02/03).** The `erased_supersedes`
  table is replaced by `lineage_erased(record_id)`: when `delete()`/`prune_derived()`/quarantine erase
  repair a dependent, a Correction gets this boolean marker (its own id only), `supersedes` becomes
  NULL. Export emits `"lineage_erased": true` on it. Import accepts a Correction with no `supersedes`
  only if the marker is exactly `true` and the importing ctx is `user_authored`; any other case
  (non-bool marker, marker on a non-Correction or on a Correction that has a `supersedes`, non-user
  ctx, no marker) gets the same generic "Correction without a supersedes target" refusal. This path
  does no id lookup at all, so there is no cross-scope existence oracle and no collision when the
  erased id is later reused. Opening a dev store that still has `erased_supersedes` converts each row
  into a marker, drops the table and scrubs; the erased ids are not kept.
  Trust note: the marker is an assertion by a user-authored importer, not proof of a deletion.
- **Active legacy rule + Correction (R7-03/R7-05).** v7 now deprecates (promotion fields removed) an
  active rule that a Correction supersedes instead of demoting it to `proposed`; its export imports
  cleanly. Uncorrected active rules still return to `proposed` for re-approval.
- **Quarantine dependents (Sonnet R7-06).** Erasing a quarantined row (`delete`, `purge_quarantined`,
  TTL prune) unlinks dependents like `delete()` of a live record (refused if a dependent is in a scope
  the caller cannot delete in). The v7 migration does the same when it quarantines an original, so no
  Correction is left dangling.
- **Deletion residue (Terra R7-05, Sonnet R7-01).** `secure_delete=ON` on open. After the outermost
  `delete()`, `prune_derived()` and `purge_quarantined()` (once, not per cascaded record) the store runs
  FTS5 `optimize` (deleted terms leave `fts_data`) and `PRAGMA wal_checkpoint(TRUNCATE)` (plaintext
  leaves the WAL). Tests scan the raw DB and `-wal` bytes and the FTS shadow tables for a sentinel.

Residual limits (round 8, superseded where Round 9 says so): backups, filesystem snapshots, copies
made by other processes and storage-media remanence are out of scope. The migration path of the
dev-table conversion trusts the old table. No Docker or multi-process test.

## Round 9 (Terra/Sonnet R8)

- **The WAL scrub no longer fails open (Terra/Sonnet R8-01, Terra R9-01/03/04/05).** `delete()`,
  `prune_derived()` and `purge_quarantined()` persist `meta.scrub_pending='1'` inside the erase
  transaction before it commits (so a crash between commit and `_scrub()` still leaves a durable
  flag), then run FTS `optimize` (only if the erase touched an FTS row) and
  `PRAGMA wal_checkpoint(TRUNCATE)` in a loop with bounded backoff
  (`Substrate(scrub_retry_seconds=2.0)`; each attempt uses busy_timeout 0, so the loop owns the
  waiting). If a reader's snapshot still blocks it: `meta.scrub_pending='1'` stays persisted, a warning
  is logged, `last_scrub_complete` is False, `Substrate.scrub_pending()` is True. It is retried (one
  non-blocking attempt, checking both the local cache and `meta.scrub_pending` so any open Substrate
  connection discovers it) at the start of the next write, at open (bounded window), and by
  `Substrate.complete_scrub()` (returns True once nothing is pending). Re-indexing a record ID
  clears it from `_fts_erased_ids`/`_fts_residual_ids` so ID reuse inside `deferred_scrub()` never
  erases the replacement's FTS entry. The call still returns True
  for the erase, because the erase is committed; the WAL bytes are gone only when `scrub_pending()`
  is False.
- **Journal redaction (Terra R8-03, Sonnet R8-05).** Erasing a record rewrites its journal rows
  (`put`, earlier `delete_denied`, `quarantine`, ...): `record_id` becomes `'[erased]'`, and the
  `delete`/`delete_quarantined`/`quarantine` free text becomes `reason=[erased]`. Type, time, actor
  and scope stay for audit. Cascaded members and pruned records are redacted the same way.
- **Scope before relevance (Terra R8-02).** `correct()` and `import_scope` decide
  missing/foreign/unreadable first and only then ask whether the target is an active rule's promoter.
  A missing original in `correct()` now also journals `correct_denied`, so the refusal (and its
  `audit_recorded`) is identical to an unreadable one.
- **Quarantined originals cascade (Terra R8-04, R9-05).** Erasing a quarantined row applies `delete()`'s
  `source_ids` lineage cascade (shared `_plan_cascade`): derived records citing it are removed, a
  retained non-derived citer vetoes the erase. The v7 migration cascades the same way
  (`migration_cascaded` lists the removed ids, a warning is logged, the open-time scrub finishes it),
  and strips dangling citations to quarantined records from any retained non-derived records (or
  cascades a record that loses its only required source) so the migrated scope exports and imports cleanly.
- **Active-rule bundles (Terra R8-05).** Any ProposedRule with status `active` anywhere in the bundle
  refuses the whole bundle before any idempotent skip or insertion.
- **Future schema (Terra R8-06).** `PRAGMA user_version` greater than the supported version raises
  `SubstrateError` before WAL switch, schema creation or any write.
- **Legacy target-less Corrections (Sonnet R8-02).** Open-time repair (also covering the v7
  migration) marks every Correction whose `supersedes` is NULL as `lineage_erased`, since only an
  erased original can produce one; the scope then exports and restores.
- **Missing FTS rowid hint (Sonnet R8-03).** Erasure falls back to deleting by `record_id` (scan) when
  the hint is absent or stale. At scrub time every erased id is verified in one pass (chunked `IN`);
  any id still indexed is erased again and keeps `last_scrub_complete` False and the flag set.
- **Visible veto (Sonnet R8-04).** A delete refused by a retaining citation, a verified claim / active
  rule, or an out-of-scope dependent writes `delete_denied` WITH the scope and the id, so the
  user-authored caller sees it in `journal_entries`; `Substrate.last_refusal` holds the reason
  (`delete refused: a retained record in this scope cites this record`). A missing id, a foreign id
  and a caller without a context all give `last_refusal == 'delete not permitted'` and a scope-less
  journal row. The veto itself is unchanged.
- **Bounded erase cost (Sonnet R8-06).** `with substrate.deferred_scrub():` postpones optimize +
  checkpoint to one run at outermost exit (the pending flag is persisted at the first deferred erase,
  so a crash is caught at the next open). A single erase skips `optimize` when it touched no FTS row
  (quarantine erase). `optimize` is kept for FTS erases: `merge` does not guarantee old segments
  holding deleted terms are rewritten.

Residual limits (round 9): the scrub guarantee is "WAL clean once `scrub_pending()` is False", not
"clean on return"; a process that dies between the commit and the first scrub attempt (outside
`deferred_scrub`) leaves no flag, though SQLite checkpoints the WAL when the last connection closes.
An erase costs O(index) (optimize + one verification scan) unless deferred. A pending flag set by one
process is retried by another only at open. `write_projection` files and backups are not touched by
delete. IDs that are content-derived (`doc-<hash>`) are redacted from the journal but remain
guessable from the source file. Docker and multi-process behaviour untested.
