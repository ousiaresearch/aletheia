# Phase 8 Security Audit

**Date:** 2026-10-29  
**Scope:** Entrypoints, temp files, egress surfaces, injection paths  
- **Auditor:** Arien (self-audit; independently reviewed and APPROVED 2026-10-29)

## Entrypoints

- `aletheia/cli.py:entrypoint()` is the single entrypoint.
- Subcommands: `ingest`, `recall`, `rebuild-fts`, `serve`, `export`, `delete`.
- No shell=True in subprocess calls.
- No `eval` or `exec` on user input.
- Paths passed to SQLite are file paths only; no URL handlers.

## Temp files

- Release/verification scripts create temp dirs via `mktemp -d` under the platform temp location (`/tmp` on Linux, `$TMPDIR` on macOS). `mktemp -d` creates private directories; umask 0077 additionally restricts venv contents. Scripts leave temp venv dirs in place after verification; cleanup is manual or deferred to OS tempdir policy. `/tmp/aletheia-install-test.db` is only passed to `aletheia ... serve --help` and is not created.
- `tests/test_install.py` uses `tempfile.mkdtemp` under the platform temp location with umask 0077; `/tmp/aletheia-install-pytest.db` is only passed to `aletheia ... serve --help` and is not created.
- `aletheia/adapters.py:67,95,100` uses `tempfile.mkstemp` for adapter scratch files in the Hermes scratch dir.
- `aletheia/substrate.py:1554` creates sibling `.tmp-*.part` files during writes; `aletheia/substrate.py:207-240` sets umask 0o077 and chmod 0o600 on the database and sidecars.
- Temp file permissions are enforced via umask 0077 in substrate.py and release/verification scripts.

## Egress surfaces

- External model inference is opt-in per query (user sees egress preview).
- Model fetch downloads from Hugging Face only; no other external network calls.
- FastAPI server binds to 127.0.0.1 by default; no external exposure unless user changes host.

## Injection paths

- Retrieved content is data, not instructions. CLI does not execute retrieved text.
- Scope engine denies by default; content-based only.
- No SQL injection vector: all queries use parameterized statements via `sqlite3` placeholders.
- No command injection: no shell=True, no user input in shell commands.

## Residual risks

- ML model adapters fetched from Hugging Face without integrity check (SHA-256 not pinned for all models).
- `aletheia serve` does not implement authentication; localhost only.

## Status

Beta-readiness self-audit complete. Independent adversarial review in progress (pending). Cleared items: entrypoints, egress, injection, temp-file handling.

