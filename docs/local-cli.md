# Local CLI

The local CLI is the Aletheia composition root for one operator and one named scope per invocation. It uses the local BGE-M3 4-bit adapter for ingestion and hybrid recall; absent model weights are a visible error. No remote provider is called.

Run from the repository root:

```sh
.venv/bin/python -m aletheia.cli --help
.venv/bin/python -m aletheia.cli --db ~/.aletheia/aletheia.sqlite3 ingest --scope personal --file ./notes/example.md
.venv/bin/python -m aletheia.cli --db ~/.aletheia/aletheia.sqlite3 recall --scope personal --query "what did I decide about memory?"
```

The `--db` option precedes the subcommand. The database is owner-only (0600) when created by the substrate. `ingest` accepts formats supported by the current local ingestion parser: TXT, Markdown, text-layer PDF, and DOCX. It emits JSON with the record id, scope, and model id. Rejected files return a JSON rejection and exit status 2. Runtime and input errors go to stderr and return nonzero.

`recall` emits JSON results and uses hybrid lexical+vector retrieval. `--limit` is bounded to 1–100. The CLI only grants access to the explicitly named scope for that invocation. It does not implement user authentication, persistent scope configuration, multi-user service isolation, external-provider consent, or cross-scope access; do not expose it as a network service. Every invocation must name the scope it intends to access.

The default database path is `~/.aletheia/aletheia.sqlite3`. Keep backups and the database itself private: it contains plaintext memory.

## Rebuild an old text index

For a trusted-local, single-operator store whose hybrid recall refuses because its FTS entries are missing or use an older field order, back up the database first and run `aletheia --db PATH rebuild-fts --scope SCOPE`. This replaces that scope's full-text projection from its current stored records; it does not load a model, modify source files, rebuild vectors, establish vector provenance, or reconcile deletions in backups. A successful rebuild does not guarantee hybrid recall will succeed: vector or other integrity problems can still block it. Stop other Aletheia processes during maintenance. This command and its failure handling remain subject to independent release review.
