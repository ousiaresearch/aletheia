# Aletheia — Competitive Gap Closure Plan

**Created:** 2026-10-29  
**Goal:** Close the highest-impact gaps between Aletheia and comparable open-source memory providers.  
**Constraints:** clean-room only for external systems; AGPL-3.0; adversarial review before each phase is called closed.

---

## Gap inventory and priority

| Priority | Gap | Why it matters | Phase |
|---|---|---|---|
| P0 | No standardized benchmarks | Credibility gate; every serious provider publishes LoCoMo / LongMemEval / BEAM numbers. Without them, Aletheia is "unverified" in procurement and comparison. | 9 |
| P0 | No MCP server | MCP is the dominant integration surface for Claude Code, OpenClaw, OpenCode, and Hermes. No MCP = no agent adoption outside custom code. | 10 |
| P0 | **No comparable measurement** | Phase 9 published retrieval numbers while competitors publish answer-level ones, with no declared reader, no fixed judge and no coverage metric — so no claim about competitiveness could be made in either direction. See `parity-measurement-spec.md`. | 9B |
| P1 | No background extraction / consolidation | Pull-only `ingest` commands limit real-time agents. Honcho, Mem0, ByteRover all extract in the background. | 11 |
| P1 | No graph or temporal reasoning | Cognee, Hindsight, Zep/Graphiti all offer entity/temporal graphs. This is the #1 feature request in agent memory benchmarks. | 12 |
| P2 | No procedural memory / agent-curated state | LangMem and Letta let agents rewrite their own procedures. Differentiator for long-running, self-improving agents. | 13 |
| P2 | No managed or turnkey deployment | Mem0 Cloud, Honcho Cloud, Cognee Cloud lower the barrier to try. Aletheia currently requires CLI + Python. | 14 |
| P3 | Community and ecosystem | Stars, Discord, integrations, plugins. These are lagging indicators of product-market fit, not leading ones. Close after P0–P2. | 15 |

---

## Phase 9 — Benchmark suite (P0)

**Goal:** Publish reproducible benchmark results on the field's standard evals.

**Deliverables:**
1. `benchmarks/` directory with harness code.
2. LoCoMo, LongMemEval-S, and BEAM (1M / 10M if feasible) adapters.
3. Gold-token validation and abstention assertions (same pattern as Phase 7 evaluation runner).
4. Independent adversarial review of benchmark harness before first publish.

**Acceptance criteria:**
- Harness runs on a single command from `README.md`.
- Gold answers and reproduction steps are committed.
- Results are published in `docs/benchmarks/` and referenced in `README.md`.
- **Every published rate names its stage** (retrieval / reading / end-to-end), its N, and a 95%
  interval, per `parity-measurement-spec.md` §1–2. A number whose stage is unstated is not a result.

**Closed 2026-11-09** — all four met, including the fourth, which was added while closing it because
the original three could be satisfied by numbers that meant nothing to anyone else.

---

## Phase 9B — A comparable measurement path (P0)

**Goal:** Make it possible to say something defensible about how Aletheia compares to Mem0, Zep and
MemHQ. The full definition lives in [`parity-measurement-spec.md`](parity-measurement-spec.md); this
phase is the work it implies.

**The problem in one line:** competitors publish answer-level scores from a pipeline that includes
generation. Aletheia's `recall` returns receipted evidence and the host agent generates, so Aletheia
has no answer-level number at all — and no amount of retrieval work produces one.

**Deliverables:**
1. **Decision, recorded:** ship a reference reader used only for measurement (spec §4 Option A), or
   compete on retrieval explicitly and refuse the answer-level table (Option B). This is an operator
   decision, not an engineering default.
2. Coverage and full-recovery metrics in the published suite as first-class numbers, not extras.
   Already measured: 11.9% full recovery on multi-record questions.
3. Abstention scored as its own gate. Already measured: 0/444 — recall never declines.
4. If Option A: a pinned reader, a pinned judge with a repeatability check (n ≥ 50, agreement
   published), and per-question retrieval ids stored so reading can be scored against fixed evidence.
5. A comparison run whose verdict is stated as separated or overlapping on 95% intervals — never as
   "we win" on overlapping intervals.

**Acceptance criteria:**
- Any comparison document states the question set, the scoring path, the stage, the judge version and
  the interval for both sides, or makes no comparison.
- No numeric target appears anywhere without the committed artifact that produced it.

## Phase 10 — MCP server (P0)

---


**Goal:** Expose Aletheia recall and ingest as an MCP server so any MCP client can use it.

**Deliverables:**
1. `aletheia/mcp_server.py` — stdio MCP server wrapping `recall`, `ingest`, `rebuild-fts`, `status`.
2. `aletheia/mcp_tools.py` — tool definitions with scope validation and deny-by-default enforcement.
3. Entrypoint `aletheia-mcp` in `pyproject.toml` scripts.
4. Tests: tool schema validation, scope denial, recall receipt formatting.
5. Adversarial review.

**Acceptance criteria:**
- `aletheia-mcp --help` works from a fresh venv install.
- Claude Desktop / OpenCode / Hermes config examples in README.
- All scope invariants enforced at the MCP boundary.

---

## Phase 11 — Background extraction / consolidation (P1)

**Goal:** Add an optional background extractor that consolidates raw records into scoped summaries without breaking the single-file, no-dependency model.

**Deliverables:**
1. `aletheia/extraction.py` — optional background extraction module (not imported unless requested).
2. CLI commands: `aletheia extract --scope <scope>`, `aletheia consolidate --scope <scope>`.
3. Config: `extraction.enabled`, `extraction.model`, `extraction.cadence`.
4. Tests: extraction produces Correction records when facts change; consolidation reduces record count; fail-closed on model errors.
5. Adversarial review.

**Acceptance criteria:**
- Extraction runs synchronously or via a background worker; substrate never blocks on it.
- Default install is unchanged; extraction requires `pip install aletheia[extraction]`.

---

## Phase 12 — Graph / temporal reasoning (P1)

**Goal:** Add optional entity linking and temporal validity windows without requiring an external graph database.

**Deliverables:**
1. `aletheia/temporal.py` — bi-temporal validity windows on records.
2. `aletheia/entity_link.py` — entity extraction and linking using the local embedder (no external graph DB).
3. SQLite schema extension: `entity_links` table, `valid_from` / `valid_to` columns.
4. Retrieval boost: linked entities increase relevance score.
5. Adversarial review.

**Acceptance criteria:**
- Entity links are stored in the same SQLite file; no new infrastructure.
- Queries can filter by `effective_at` for temporal grounding.
- Default install unchanged; graph features require `pip install aletheia[graph]`.

---

## Phase 13 — Procedural memory / agent-curated state (P2)

**Goal:** Allow agents to propose procedural rules and prompt refinements, gated by user attribution.

**Deliverables:**
1. `aletheia/procedural.py` — `ProposedRule` status; only `Decision`/`Commitment` records with user attribution promote rules to `active`.
2. CLI: `aletheia propose-rule`, `aletheia active-rules --scope <scope>`.
3. Tests: rules are deny-by-default; no rule activates without user-attributable Decision.
4. Adversarial review.

**Acceptance criteria:**
- Proposed rules never enter active retrieval without explicit user attribution.
- Rules are scoped; identity grants nothing.

---

## Phase 14 — Turnkey deployment (P2)

**Goal:** Lower the barrier to evaluation with a Docker image and optional managed signup flow.

**Deliverables:**
1. `Dockerfile` — builds wheel, installs with `[server]`, exposes `8765`.
2. `docker-compose.yml` — single command: `docker compose up`.
3. `docs/turnkey-deployment.md` — Docker and FastAPI server guide.
4. Managed signup page placeholder on ousiaresearch.xyz pointing to Docker for self-hosters.

**Acceptance criteria:**
- `docker compose up` produces a running server in under 60 seconds on a machine with Docker.
- No external dependencies beyond the container image.

---

## Phase 15 — Community and ecosystem (P3)

**Goal:** Build visibility and integration surface after P0–P2 are shipped and reviewed.

**Deliverables:**
1. GitHub Discussions enabled.
2. Discord server for users and contributors.
3. Integration docs for Hermes, OpenCode, Claude Code.
4. Good first issue labels and contributing guide.

**Acceptance criteria:**
- 10 GitHub stars within 30 days of Phase 10 MCP announcement.
- 3 external integrations (Hermes plugin, OpenCode memory adapter, Claude Code MCP config) documented.

---

## Sequence and dependencies

```
Phase 9 (benchmarks) ──► Phase 9B (comparable measurement) ──┐
                                                              ├── README credibility update ── Phase 15
Phase 10 (MCP) ──────────────────────────────────────────────┤
                                                              ├── Phase 11 (background extraction)
Phase 12 (graph) ────────────────────────────────────────────┤
                                                              ├── Phase 13 (procedural)
Phase 14 (Docker) ───────────────────────────────────────────┘
```

Phases 9 and 10 can start in parallel. **Phase 9B gates every competitive claim**: until the stage a
number belongs to, its scoring path and its judge are declared, nothing may be said about where
Aletheia sits relative to Mem0, Zep or MemHQ — the failure that produced both the phantom "93.0 floor"
and two days of a proxy figure quoted as retrieval quality. Phase 11 depends on Phase 10 being
reviewed so extraction results can be exposed via MCP. Phase 12 can proceed independently. Phase 14
depends on Phase 10 (server). Phase 13 and 15 depend on 9–12 being closed.

Each phase stops for adversarial review before it is marked complete. No phase is considered finished until the reviewer returns APPROVED.

---

## Where the plan actually stands — reconciliation 2026-11-09

Checked against each phase's own acceptance criteria, by opening the artifacts rather than trusting
the phase list. The short version: **the two P0 items are the weakest, and the P1/P2 items behind
them are largely done.** The order this plan intends is not the order the work happened in.

| Phase | Priority | Status | Evidence |
|---|---|---|---|
| 9 — Benchmark suite | P0 | **Closed 2026-11-09** | One entrypoint (`benchmarks/run.py`), documented from `README.md` and from `benchmarks/README.md`; gold answers and reproduction steps committed in `docs/benchmarks/REPRODUCE.md`; results published in `docs/benchmarks/results.md` and referenced from `README.md`; abstention now measured rather than assumed — and it reads 0/444, so the assertion exists and fails, which is the point of having one. Two independent adversarial audits of the harness ran before publication (see `results.md`). |
| 10 — MCP server | P0 | **Not shipped** | `aletheia/mcp_server.py` exists but `aletheia/mcp_tools.py` does not, and `pyproject.toml` declares only `aletheia` and `aletheia-server` — there is no `aletheia-mcp` entrypoint, so `aletheia-mcp --help` fails. `README.md` mentions MCP zero times. This is the plan's stated adoption gate and it is open. |
| 11 — Background extraction | P1 | **Shipped** | `aletheia/extraction.py`; `extract` and `consolidate` are live CLI commands. |
| 12 — Graph / temporal reasoning | P1 | **Shipped** | `graph.py`, `temporal.py`, `entity_link.py`; `relate`, `graph`, `temporal`, `link-entities` CLI; recall reaches the graph as a bounded context layer (W2.1). |
| 13 — Procedural memory | P2 | **Shipped** | `aletheia/procedural.py`; `propose-rule` and `active-rules` CLI. |
| 14 — Turnkey deployment | P2 | **Present, unverified** | `Dockerfile` and `docker-compose.yml` are in the repo; the "<60 s on a machine with Docker" criterion has not been measured here. |
| 15 — Community | P3 | **Not started** | GitHub reports 0 stars and Discussions disabled; README has no integration docs for Hermes, OpenCode or Claude Code. |

### The measurement that Phase 9 was a gate for

Phase 9 asks for credible published numbers, and now has them for LoCoMo — with the caveat that they
are retrieval numbers, not answer numbers:

- whole benchmark, lexical, N=1,982 gradable of 1,986: hit@10 **64.6%** (CI 62.5–66.7), MRR 0.394,
  mean evidence coverage 0.591, full recovery 54.8%
- the questions that need more than one record (n=427) are the real gap: hit@10 57.6%, coverage
  0.317, full recovery **11.9%**, and depth 50 only lifts that to 26.9%

**There is no numeric parity target in this plan, and there never was.** The "93.0 floor" that
appeared in `results.md` and in `recall-bolstering-plan.md` on 2026-11-09 was introduced by the
benchmark commit itself and has been withdrawn. The competitor numbers that do exist (Mem0 67.1,
Zep/Graphiti 75.1, MemHQ 83.2 on LoCoMo) are answer-level and LLM-judged; ours are retrieval-level.
A like-for-like comparison needs one scoring path across all of them, and that does not exist yet.
So the honest position is: **credibility gate partially cleared, parity unknown, and unknown is not
a synonym for behind** — but neither is it a synonym for close.

### What this implies for sequencing

0. **Phase 9B is the gap this reconciliation exposed.** The plan asked for credibility and never
   defined a comparable number, so a whole week of measurement could not answer the only question a
   credibility gate exists to answer. That definition now exists in `parity-measurement-spec.md`, and
   the operator decision it needs — reference reader, or compete on retrieval and say so — is the
   first thing that unblocks every competitive statement.
1. **Finish Phase 10 before more retrieval work.** It is a P0, it is the adoption surface, and it is
   one entrypoint plus one module away from its acceptance criteria. Nothing in the recall roadmap
   unlocks distribution the way this does.
2. ~~Close Phase 9's README criterion.~~ Done 2026-11-09 with the suite entrypoint.
3. **Then the coverage work**, judged on full-recovery rate for multi-evidence questions, which is
   where the measurable deficit is largest and where hit@10 has been hiding it.
