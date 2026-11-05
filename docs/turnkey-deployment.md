# Weft HTTP deployment

The image and `weft-server` run `weft.server`, which serves the desktop API and
custom `/mcp/*` HTTP routes. Despite their names, these routes are **not an MCP
protocol server**: they do not implement MCP transport or JSON-RPC and cannot be
assumed to work with MCP clients.

## Docker Compose

```bash
export WEFT_API_KEY='replace-with-a-long-random-secret'
docker compose up --build
curl -H "Authorization: Bearer $WEFT_API_KEY" http://localhost:8765/health
```

Compose publishes port 8765 on host loopback (127.0.0.1), persists data in the `weft-data`
volume, and configures the scopes `default,benchmark`. Protect the key and the
volume. The service has no built-in TLS or per-client identities: anyone with the
key can access **all** configured scopes. Do not expose it to untrusted networks;
use appropriate network restrictions and TLS termination where needed.

| Variable | Meaning |
|----------|---------|
| `WEFT_API_KEY` | Required bearer secret; absent or incorrect credentials are rejected. Compose requires it at startup. |
| `WEFT_KNOWN_SCOPES` | Comma-separated allowed scopes; absent or empty denies all scopes. Compose sets `default,benchmark`; change the Compose configuration to change them. |
| `WEFT_DB_PATH` | Database path; defaults to `~/.weft/weft.sqlite3`. Compose sets `/home/weft/.weft/weft.sqlite3`. |

All endpoints, including `/health`, require the bearer header. Every scoped
request names a configured scope. This single shared key does not distinguish
users and does not establish that content is user-authored.

## HTTP endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Authenticated health check |
| POST | `/query` | Scope-limited lexical substring query |
| POST | `/export` | Export readable records in a scope |
| GET | `/record/{record_id}?scope=...` | Read one record |
| POST | `/shadow/recall`, `/shadow/reason`, `/shadow/compare` | Shadow endpoints |
| POST | `/mcp/ingest` | Write agent-attributed records |
| POST | `/mcp/recall` | Recall records |
| POST | `/mcp/status` | Count readable records |
| POST | `/mcp/call` | Custom JSON tool router (`ingest`, `recall`, `status`, `export`) |

For example:

```bash
curl -H "Authorization: Bearer $WEFT_API_KEY" -H 'Content-Type: application/json' \
  -d '{"scope":"default","records":[{"record_id":"note-1","payload":{"text":"example"}}]}' \
  http://localhost:8765/mcp/ingest
```

Ingest defaults to a `Source` with `stated` status and always uses `agent`
attribution. It cannot assert user or verifier attribution; knowledge and scope
rules still apply. Payload and provenance must be objects; payload `text`, when
present, must be a string. Record IDs must be unique; conflicting or invalid
writes are refused, and the whole batch rolls back. For the custom router, wrap
the same request in `{"tool":"ingest","arguments":{...}}`. HTTP recall uses
lexical mode: HTTP ingest does not create vector indexes. It still refuses recall
if the full-text index is incomplete or stale; it does not silently bypass that
integrity check. Hybrid/vector retrieval is not provided by these HTTP routes: the `server`
extra does not install sentence-transformers or any embedder, and deployed HTTP recall is
lexical-only by design.

HTTP recall errors: an incomplete or stale full-text index returns **503** with a
detail naming the recovery step (`weft --db <path> rebuild-fts --scope <scope>`, run by an
operator against the same database; the message names the requested scope). `limit` must be an integer from 1 to 100;
other values are rejected with a 4xx. Every request on a non-HTTP ASGI scope
(for example WebSocket) is denied.

## Operational notes

- **Credentials in examples.** Every `curl` example sends `$WEFT_API_KEY`;
  export it first and substitute your own secret. Never paste a real key into
  shared scripts.
- **Rule approval.** A rule is approved only by a user-attributed `Decision` or
  `Commitment` carrying a structured affirmative field: payload exactly
  `{"rule_id": "<id>", "approved": true}`. Free text, rejections, extra fields
  and agent-attributed records never approve. HTTP ingest cannot create these.
- **HTTP recall is lexical-only.** It never loads embeddings or vector indexes.
  Scaling: it verifies full-text index completeness before answering, so cost
  grows with the number of records. Measured on the development host (SQLite
  3.53): lexical recall takes 0.03 s at 2,000 records, 0.17 s at 10,000 and
  0.51 s at 30,000; ingesting 30,000 records takes 3.1 s. The `python:3.12-slim`
  image ships SQLite 3.40, which was not tested, and Docker was not run for
  these numbers. If the check fails, an operator rebuilds the index with
  `weft --db <path> rebuild-fts --scope <scope>`. Orphaned records (records
  whose scope is not the one being served) need a rebuild of the orphan's own
  scope, not of the requested one. `limit` is capped at 100.
- **`/mcp/*` is not the MCP protocol.** These are custom JSON-over-HTTP routes
  (including `/mcp/call`); they do not implement JSON-RPC, tool discovery or
  MCP transports, so standard MCP clients cannot use them directly.

## Backup and restore

`/export` output is a read-only snapshot, not a complete backup. Restore is
**Python API only**: `Substrate.import_scope` has no CLI command and no HTTP route,
so a restore means running Python code against the target store. Preconditions
(a bundle failing any of them is refused whole):

- **User-attributed records need a user-authored context.** Restoring a bundle that
  holds user-attributed records (for example a user Decision) with a context that
  is not user-authored is refused ("user attribution requires a user-authored turn").
- **Verification records and verified Claims need a registered verifier.** The
  importing actor must be a registered verifier, otherwise the bundle is refused
  ("actor is not a registered verifier").

Schema and migration: a legacy Correction whose original was deleted before this version
is marked `lineage_erased` by the migration, so its export stays restorable (by a
user-authored import, as below). A database with a newer schema version than the code
supports is refused rather than opened.

Deleting a record that a Correction supersedes does not leave a forgeable stand-in. The
Correction instead carries a **lineage-erased** marker: it records that its original was
deleted, without naming or recreating it. A lineage-erased Correction is restorable only
by a user-authored import (a user-authored context); other contexts are refused.

Import handles rules by state:

- **Active rules refuse.** An export containing an active rule is deliberately
  **not fully restorable**: import will not recreate it, so the whole bundle is
  rejected atomically. Rules must be re-proposed and re-activated.
- **Rejected and deprecated rules import as inert records.** They are kept for
  history and never become active.
- **Existing rules never transition on import.** Importing cannot promote,
  reject or deprecate a rule that already exists in the target store.
- **Imported proposed rules need fresh approval.** The Decisions that named them
  do not carry approval over; a restored proposal needs a new user-authored
  Decision or Commitment before it can be promoted.

## Deletion and erasure

`delete` is a Python-API operation (`Substrate.delete`); there is no CLI command and no
HTTP route for it. What it does, and what it does not:

- **Logical erasure is immediate.** `delete` commits logical erasure immediately and
  removes the record from the live database and the full-text (FTS) index. The call
  compacts the FTS index (FTS optimize) and then asks SQLite for a WAL checkpoint.
- **Physical removal from the write-ahead log is conditional.** A TRUNCATE checkpoint
  truncates the WAL, but only when no other connection blocks it. A concurrent reader
  holding a snapshot can delay that checkpoint. The system retries with bounded backoff
  (`Substrate(scrub_retry_seconds=2.0)` by default) and persists a scrub-pending flag
  (`meta.scrub_pending`, visible as `Substrate.scrub_pending()`) inside the erase transaction
  before it commits, so an interruption after commit never loses the pending state; the scrub
  completes at the next open of the database, at the next write on any Substrate connection, or
  when you call `Substrate.complete_scrub()`. The guarantee is that the WAL is clean once
  `scrub_pending()` is False, not at the moment `delete` returns. Until then, **deleted plaintext may remain in the
  live `-wal` file**, and `delete` still returns success because the logical erasure has
  committed.
- **The journal keeps audit rows, with the id and reason redacted.** For a deleted
  record the journal retains audit rows (type, time, actor, scope), but its record id
  and free-text reason are redacted to `[erased]` after delete. This is intended
  behaviour from this round of the code. Journal rows written before this round of the
  code keep their ids and reasons, and are not erased.
- **Why ids are redacted.** Ingest ids are content-derived (`doc-`, `media-` or
  `extracted-` plus a sha256 prefix), so an id can itself reveal content: someone holding
  the database could confirm that a candidate file was ingested.
- **A delete can be refused.** If another retained record cites the target, the delete is
  refused and the target stays. The refusal is recorded in the scope's journal
  (`delete_denied`, visible to a user-authored caller) and in `Substrate.last_refusal`; a
  foreign id, a missing id and a missing context all give the same generic
  `"delete not permitted"`.
- **Cost.** Each delete is O(index) because of the FTS optimize and the checkpoint. A
  reviewer measured about 0.18 s per delete at 20k records and 0.85 s at 100k records.
  Bulk deletes should use the deferred scrub (`with substrate.deferred_scrub():`), which performs one FTS
  optimize and one checkpoint at the outermost exit instead of paying that cost per
  record. Files written by `write_projection` are not touched and must be removed
  separately.

This does **not** erase: backups, exports already written (including `/export` output
you saved), snapshots (volume or filesystem), projection files written earlier, or
storage-media remanence (SSD wear levelling, journals, swap). Those copies must be
handled by the operator.

A database whose `PRAGMA user_version` is newer than the code supports is refused before
any write, and an import bundle that contains any active rule is refused whole, even if
that rule already exists identically.

## HTTP-ingested records and CLI hybrid recall

Sources and Claims ingested over HTTP have **no vectors**: HTTP ingest never computes
embeddings, and deployed HTTP recall is lexical-only by design. Hybrid recall is a
desktop/CLI feature. `weft ingest`, `weft recall` (hybrid) and `weft reembed` all need the
local embedder, and `default_text_embedder` loads an MLX BGE-M3 snapshot from the local
Hugging Face cache; its dependencies (`mlx`, `mlx-embeddings`, the `local-embedding` extra)
are available on darwin/arm64 only. The `local-recall` extra (sentence-transformers) backs
only the optional cross-encoder reranker and query expansion, not an embedder. In the Linux
Docker image there is therefore no embedder: these commands fail there with "embedding
model not present", and HTTP-ingested records stay without vectors. Do not rely on
`reembed` inside the container.

To get hybrid recall over such records, copy or export the data to a darwin/arm64 machine
with the model cached and run, against that database:

```bash
weft --db <path> reembed --scope <scope>
```

It embeds records that lack a vector; records with no text cannot be embedded and are
reported as skipped. CLI hybrid recall refuses a scope until its vector index is complete.
HTTP lexical recall is unaffected.

## `weft serve`

`weft --db <path> serve` exports `--db` as `WEFT_DB_PATH` for the server process
when `WEFT_DB_PATH` is not already set; if both are set and differ, the
environment variable wins and a warning goes to stderr. `serve` also warns on
stderr when `WEFT_API_KEY` or `WEFT_KNOWN_SCOPES` is unset (the server fails
closed); secret values are never printed.

The Compose health check is defined in the Compose file, not the image. The
image retains build tools and curl; source changes invalidate the package install
layer. Neither image build nor runtime has been verified here with Docker.

## Local install

From a checkout, install `pip install '.[server]'`, set `WEFT_API_KEY` and
`WEFT_KNOWN_SCOPES`, then run `weft-server --host 127.0.0.1 --port 8765`.
Choose a writable `WEFT_DB_PATH` if the default location is unsuitable. The
Docker build and runtime are unverified and need to be verified on a machine with Docker; this guide
is not evidence of a successful deployment.

## `weft extract` sends scope text to an external service

`weft extract` (and `weft.extraction.extract_facts`) sends record text to the
third-party endpoint `https://tokenharbor.ai/v1` (TokenHarbor). It is **disabled
by default**. It runs only when you pass `--allow-external` or set
`WEFT_EXTRACT_ALLOW_EXTERNAL=1`; the destination host is printed to stderr before
anything is sent. The credential is read from `TOKENHARBOR_API_KEY` only (no
credential file is read). Only records the caller's scope grant can read are sent.
The HTTP server never calls this service. Not exercised against the real endpoint
in this repository's tests (the transport is faked).
