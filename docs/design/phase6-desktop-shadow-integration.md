# Phase 6 — Desktop and Hermes Shadow Integration

## Electron Desktop

### 6.1 Required Views
1. **Records list** — browse all records in a scope, with type, status, and attribution badges.
2. **Record detail** — full record view with lineage (source_ids, supersedes chain).
3. **Scope selector** — switch between authorized scopes.
4. **Ingest** — drag-and-drop or file picker for documents, with ingestion progress.
5. **Query** — search interface with FTS results and scope filtering.
6. **Reasoning** — view reasoning chains (extract → hypothesize → contradict → conclude → synthesize).
7. **Admin** — retention settings, export/import, logs.

### 6.2 Tech Stack
- Electron + React (or plain HTML/JS for alpha).
- IPC bridge to a local Aletheia HTTP service.
- Service runs `aletheia serve` (see §6.4).

### 6.3 Security
- Desktop app authenticates via OS keychain.
- Local service binds to 127.0.0.1 only.
- No remote exposure without explicit config.

## Hermes Shadow Integration

### 6.4 Shadow Mode
- Aletheia runs as a background service in the same Hermes profile.
- Reads from the same SQLite store but never writes to the active session's journal.
- Provides `shadow_recall` and `shadow_reason` endpoints.
- Comparison output: diff of recalled records, reasoning chains, and citations.

### 6.5 Structured Logs
- Redaction: all external content passes through `_sanitize` before logging.
- Log levels: DEBUG (internal), INFO (user actions), WARN (boundary hits), ERROR (fail-closed events).
- Log format: JSON with timestamp, operation, record_id, scope, actor.

## Exit Criteria
- Desktop app launches and displays records.
- Shadow mode runs without interfering with active session.
- Logs are structured and redacted.
