# Aletheia

![Aletheia — every memory carries a receipt](docs/images/hero-card.jpg)

Evidence-bound scoped memory for people and their agents.

Aletheia writes conversations, documents and media into a scoped, auditable store inside your own
profile. Recall is scoped first, ranked second, and explained in a receipt naming the scope
consulted and the ids selected. Deletions cascade, corrections preserve lineage, and nothing
retrieved is ever injected whole.

One repository, one name: this is both the substrate (the `aletheia` package) and the Hermes
plugin that register it as an external memory provider.

## Status

2.0.0. The substrate, scope engine, ingestion, retrieval, reasoning loop, HTTP server and the Hermes
memory provider are implemented and reviewed through phase 14. The major version marks the rename
from Weft to Aletheia: distribution, import path, CLI, environment variables, tool names and default
store path all changed together, and an existing store is moved into place on first run rather than
abandoned. `scripts/test.sh` runs the current suite: 1051 checks. AGPL-3.0.

## Install

### As a Hermes memory provider (the usual way)

```bash
hermes plugins install ousiaresearch/aletheia      # installs disabled
hermes plugins enable aletheia                      # then opt in
hermes config set memory.provider aletheia
```

Admission to the Hermes plugin catalog is a separate, human-reviewed PR; until that lands, install by
`owner/repo` as above or pin a commit with `--ref <sha>`.

### As a Python package

```bash
pip install ousia-aletheia
```

The import name is `aletheia`; the distribution is `ousia-aletheia` because `aletheia` and
`aletheia-memory` both belong to other people on PyPI. `pip install aletheia` would install
someone else's package, so don't.

The provider needs the embedder and the cross-encoder reranker on disk, downloaded as described
under [Models](#models) — there is no `aletheia models fetch` command. A missing model is reported,
never papered over with a keyword fallback.

### From source

```bash
git clone https://github.com/ousiaresearch/aletheia.git
cd aletheia
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[server]"
```

### Upgrading from Weft

Nothing to migrate by hand. On first run Aletheia moves `<profile>/weft/weft.sqlite3` (with its
WAL), `<profile>/weft/provider-config.json` and `~/.weft/weft.sqlite3` to the Aletheia paths. An
existing store at the new path always wins; a legacy file that loses the race is left where it is,
never deleted.

## Multiple profiles

One store serves every profile. Profiles are scopes inside it, not separate databases:

```
<hermes_root>/aletheia/aletheia.sqlite3
  profile:default     ← the root home
  profile:isildur
  profile:palantir
```

Each profile writes only to its own scope. Reading another profile's scope is decided by a
**host** policy file beside the store, not by the profile's own config — a file a profile can edit
cannot be the authority for reading someone else's memory:

`<hermes_root>/aletheia/policy.json` (mode `0600`, owner-only):

```
{
  "profiles": {
    "palantir": { "read_scopes": ["profile:isildur"], "shared": true, "shared_write": false }
  }
}
```

The profile asks for a grant in its own `provider-config.json`; the policy decides. A request the
policy does not name is ignored, `shared` is granted by the policy flag alone, and a missing or
loosely-permissioned policy grants nothing beyond the profile's own scope.

```
{
  "store_path": "/Users/you/.hermes/aletheia/aletheia.sqlite3",
  "read_scopes": ["profile:isildur"],
  "shared": true,
  "shared_write": false
}
```

`read_scopes` grants reads, never writes. Every recall's receipt names the scopes consulted, so a
grant is visible in the audit trail rather than implied by configuration.

A store written before 2.1 keeps its records under the old basename scope (`profile-.hermes`); the
provider does **not** move them on startup — a rename decided from a path is how one profile absorbs
another's memory. It reports the count and the exact command, and the rename moves records, journal,
entity links, jobs, the FTS index and the scope-keyed vector tables in one transaction:

```bash
aletheia --db ~/.hermes/aletheia/aletheia.sqlite3 rename-scope --from profile-.hermes --to profile:default
```

Stores from other profiles are copied in explicitly, without touching the source:

```bash
aletheia --db ~/.hermes/aletheia/aletheia.sqlite3 merge-store \
  --from-db ~/.hermes/profiles/isildur/aletheia/aletheia.sqlite3 \
  --from-scope profile-isildur --to-scope profile:isildur
```

Both commands exit non-zero when they moved nothing: a typo'd scope, a refused bundle and an empty
source are failures, not successes.

To check which copy is actually running — repository, plugin directory, plugin-source snapshots and
installed distributions drift independently:

```bash
python scripts/verify_deployed.py
```

## Boundaries

- Aletheia is not a brain, a consciousness, or a proof of either.
- It does not post, message, or commit on its own.
- Recall is data, not instructions. Retrieved text cannot authorize writes or alter scope.
- Scope is deterministic and deny-by-default. Identity grants nothing.

## Install

### From PyPI (alpha)

The distribution is `ousia-aletheia` — `aletheia` and `aletheia-memory` belong to other projects on
PyPI, so `pip install aletheia` gets you someone else's package.

**PyPI currently carries 2.0.1, which declares the model requirements as extras.** Installing it
gives a package whose provider comes up without an embedder. Until this tree is published, install
from source below, or add the extras explicitly:

```bash
pip install 'ousia-aletheia[local-embedding,local-recall]'   # the published 2.0.1 only
```

### From source

```bash
git clone https://github.com/ousiaresearch/aletheia.git
cd aletheia
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[server]"
```

Python >= 3.11 required. The `server` extra installs the HTTP server only. The provider's model
requirements — `mlx`, `mlx-embeddings` and `sentence-transformers` — are core dependencies marked
`darwin`/`arm64`: that is the only platform where the provider runs, so that is the platform an
environment built from the declaration is complete on. Elsewhere they are not installed, the
provider reports itself unavailable rather than falling back to keyword search, and the reranker is
reachable through `ousia-aletheia[local-recall]`. See [Models](#models).

## Optional external LLM features (explicit opt-in)

Fact extraction and query expansion can use an operator-selected LLM. Aletheia has no default
provider and does not silently choose or contact one. The client supports native OpenAI and
Anthropic APIs, plus arbitrary OpenAI-compatible endpoints (including compatible gateways).
Configure the provider, model, and credentials in the process environment:

```bash
# Native Anthropic example
export ALETHEIA_LLM_PROVIDER=anthropic
export ALETHEIA_LLM_MODEL='<model-id>'
export ANTHROPIC_API_KEY # load it from your secret manager before starting Aletheia

# Or native OpenAI: ALETHEIA_LLM_PROVIDER=openai, OPENAI_API_KEY=...
# For an OpenAI-compatible service, also set:
# ALETHEIA_LLM_PROVIDER=openai-compatible
# ALETHEIA_LLM_BASE_URL='<provider's API base URL>'
# ALETHEIA_LLM_MODEL='<provider's exact model id>'
# ALETHEIA_LLM_API_KEY_ENV='<name of the environment variable holding the key>'
```

Aletheia reads the key from the named environment variable; it does not read Hermes auth files
or copy credentials into project configuration. Make the secret available to the Aletheia process
through your existing secret manager. For fact extraction, external transmission remains disabled
unless you pass `--allow-external` (or set `ALETHEIA_EXTRACT_ALLOW_EXTERNAL=1`). The selected host
is printed to stderr before any request. Query expansion is separately disabled unless the caller
explicitly supplies it or sets `ALETHEIA_QUERY_EXPANSION=1`. The CLI and HTTP server make no other
external calls.

## Quick start

`ingest` and hybrid `recall` need the local embedder (darwin/arm64, see above).

```bash
aletheia --db ~/.aletheia/aletheia.sqlite3 ingest --scope coast --file note.md
aletheia --db ~/.aletheia/aletheia.sqlite3 recall --scope coast --query "tides"
aletheia --db ~/.aletheia/aletheia.sqlite3 rebuild-fts --scope coast
```

To serve over HTTP, `ALETHEIA_API_KEY` and `ALETHEIA_KNOWN_SCOPES` **must be set**; the server fails
closed, so without them every request returns 401 (no or wrong key) or 403 (scope not
allowed):

```bash
export ALETHEIA_API_KEY='replace-with-a-long-random-secret'
export ALETHEIA_KNOWN_SCOPES='coast'
aletheia --db ~/.aletheia/aletheia.sqlite3 serve --host 127.0.0.1 --port 8765
```

HTTP recall is lexical-only. The Docker image and Compose file are **unverified**: Docker was
not available to build or run them. See `docs/turnkey-deployment.md`.

Default database: `~/.aletheia/aletheia.sqlite3` (owner-only, 0600).

Deletion commits logical erasure immediately, but deleted text can remain in the live `-wal` file until a checkpoint completes, and backups, exports and snapshots are never erased. See "Deletion and erasure" in `docs/turnkey-deployment.md`.

## Models

Aletheia ships adapters, not weights. Aletheia requires Apple Silicon macOS, the MLX BGE-M3
embedder and the cross-encoder reranker; it does not fall back to keyword-only retrieval.

Those three requirements live in `[project.dependencies]`, each marked
`sys_platform == 'darwin' and platform_machine == 'arm64'`: `mlx`, `mlx-embeddings` and
`sentence-transformers`. They are dependencies rather than extras because Hermes installs a plugin
from its declaration — `pyproject.toml` when the plugin ships one, and `plugin.yaml`'s
`python_dependencies` only when it does not — so a requirement that lives only in an extra is never
installed when an environment is rebuilt. The gateway then starts, reports the provider unavailable,
and external memory is off with nothing failing. `plugin.yaml` mirrors the same three pins for
readers that only have the manifest; the `local-embedding` and `local-recall` extras are kept for
compatibility and are **not** the declaration anything installs from.

The `mlx>=0.32.4` pin publishes arm64 wheels for macOS 14 and later, so on macOS 13 the install
itself fails (`mlx==0.32.4` has no `macosx_13_0_arm64` wheel; older `mlx` releases do, this pin does
not). PEP 508 cannot express an operating-system version, so this is not expressible as a marker.

In the **same Python environment as Hermes**, install Aletheia and download the models while online
(review the model cards and licences before downloading):

```bash
python -m pip install ousia-aletheia
python -c "from huggingface_hub import snapshot_download; snapshot_download('mlx-community/bge-m3-mlx-4bit'); snapshot_download('cross-encoder/ms-marco-MiniLM-L-6-v2')"
```

Then verify the environment the gateway will actually run in, before restarting it:

```bash
python scripts/check_runtime_deps.py          # can the resolved environment import the modules?
python scripts/check_runtime_deps.py --deep   # do the embedder and the reranker actually load?
python scripts/verify_deployed.py             # which copy is deployed, and can it run?
```

These scripts ship with the repository, not the wheel, so run them from a source checkout. By
default they judge **the newest install facts** — the environment `installs/*/facts.json` names —
which is where Hermes installs plugin dependencies; it is not necessarily the interpreter holding the
gateway process. When the answer matters for a specific running gateway, pass its environment:
`--env ~/.hermes/installs/<id>/environments/<id>/venv`.

The presence check imports each module for real in the target interpreter — in isolated mode, from a
scratch directory — so neither `PYTHONPATH` nor a folder in your working directory can answer for an
install. (`--deep` is different: it deliberately puts the plugin source on `PYTHONPATH` and runs
there, because that is the copy the environment would import.) `check_runtime_deps.py` names what is
missing and prints the `uv pip install --python …` that repairs that environment: Hermes builds its
environments with uv and they carry no pip.

Exit codes for both: `0` healthy, `1` failed — a copy disagrees, a requirement is missing, the
declaration does not install it, or (`--deep`) the models do not load — and `2` unknown: the
environment could not be determined, the declaration could not be read, or the provider does not run
on this platform, so nothing was judged. **`2` is not a pass**: the failure this checks for is a
gateway that comes up cleanly with no embedder, so a check that cannot look must never print "no
drift".

Model cards: [BGE-M3 MLX](https://huggingface.co/mlx-community/bge-m3-mlx-4bit) and
[MiniLM reranker](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2). The embedder
currently requires the default `~/.cache/huggingface/hub` location, so do not override `HF_HOME`
or `HF_HUB_CACHE` for its download. The reranker uses the Hugging Face cache configuration.
Restart the gateway after setup. Provider readiness loads and probes both models locally; this can
take time and memory on startup. There is no `aletheia models fetch` command or built-in licence
consent flow. Optional media models are separate.

## Benchmarks

Measured on the public suites the field uses, with the numbers published in full — including where
they are bad.

| Dataset | Metric | Result | N |
|---|---|---|---|
| LoCoMo (whole benchmark, lexical) | hit@10 against gold evidence records | **64.4%** (95% CI 61.9–66.7) | 1,538 answerable questions of 1,986 |
| LoCoMo, questions needing >1 record | full evidence recovery | **11.9%** | 427 |
| LoCoMo, unanswerable questions | correct abstention | **0%** | 444 |
| LongMemEval-S | hit@10 against gold evidence sessions | 97.0% (95% CI 95.1–98.2) | 500 |

Three things that table is telling you honestly:

- **hit@10 overstates retrieval.** It counts a question as retrieved when any one gold record lands
  in the top 10. On the 427 LoCoMo questions that need more than one record, mean coverage is 0.317
  and full recovery is 11.9% — and five times the depth only raises that to 26.9%. Complete-evidence
  retrieval is the real deficit.
- **Recall never declines.** All 444 unanswerable LoCoMo questions return results instead of an
  abstention, so abstention accuracy is 0%.
- **Cross-paper comparison is not possible yet.** Mem0, Zep and MemHQ publish answer-level scores
  from an LLM judge; the numbers above are retrieval-level. There is no like-for-like comparison, and
  no parity claim is made either way.

Reproduce any of it with one command:

```bash
env -u PYTHONPATH .venv/bin/python benchmarks/run.py --dataset locomo
```

Full methodology, per-category breakdowns, the paired configuration diagnostics and the audit of the
harness itself are in [`docs/benchmarks/results.md`](docs/benchmarks/results.md); exact commands and
expected output are in [`docs/benchmarks/REPRODUCE.md`](docs/benchmarks/REPRODUCE.md).

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