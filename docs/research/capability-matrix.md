# Capability Matrix

**Phase:** 0 — Capability study
**Purpose:** Establish what each reference system does, so Aletheia's implementation is informed rather than naive.
**Method:** Documented behavior and configuration inspection. No source copied. Observations, inferences, and unknowns are separated.

## Reference systems

| System | What it is | Core mechanism | Key strength | Key limitation (observed) |
|---|---|---|---|---|
| **Honcho** | Peer-centric memory service (AGPL-3.0) | Workspace → peers → sessions → messages; background reasoning builds per-peer representations | Evolving models of *who someone is* over time; representation and chat endpoints for grounded answers | Unconditional context injection under `hybrid` recall (see failure-modes.md); no relevance gate before injection |
| **OpenViking** | Context database / typed filesystem (AGPL-3.0) | `viking://` virtual filesystem over knowledge, memory, skills | **Tiered loading** (L0 ~100 tok abstract → L1 ~2k overview → L2 full) and **scoped search** to a project subtree, not a flat pool | Requires a running self-hosted server; generated summaries need verification |
| **Hindsight** | Learning-oriented agent memory (MIT) | Entity resolution + knowledge graph + retain/recall/reflect; mental models and knowledge pages | Cross-memory **reflect** synthesis; temporal/entity-aware recall; strong published LongMemEval results | Newer/less battle-tested in our context; synthesis can blur evidence with interpretation |
| **Mem0** | LLM fact extraction + dedup | Server-side extraction into facts, update/delete by ID | Hands-off extraction; automatic dedup | Cloud dependency in platform mode; extraction quality is opaque |
| **Supermemory** | Managed recall with containers | Named containers, auto_recall/auto_capture, context fencing | **Context fencing** — strips recalled memories from captured turns, preventing recursive memory pollution | Cloud-first; self-hosted requires a local server |
| **ByteRover** | Local/cloud knowledge store (**ELv2 — not open source**) | `brv` CLI; pre-compression extraction | Captures learning *before* context compression destroys it | ELv2 prohibits offering it as a hosted service; less documented retrieval semantics |
| **Holographic** | Local memory w/ HRR algebra | Holographic reduced representations + trust scoring | Fully local; explicit trust scoring | Retrieval quality unverified for our use; trust scores must not be mistaken for verified truth |
| **RetainDB** | Cloud memory w/ delta compression | Incremental delta encoding | Compact storage; 10 tools | Cloud, paid; fidelity of compression unverified |
| **Arsumbris** | Typed knowledge workspace (Sustainable Use — **not bundleable**) | Typed markdown/YAML graph; engine + host UI + MCP agent layer with plugins and hooks | **Type system** makes records inspectable and structured; **hook shapes** (mediator/stamper/session-start) for governance; typed tool inputs | Non-commercial license; no sandboxing; diagnostics advisory only; mediation fails open if daemon unreachable |

## Capability → Aletheia requirement map

| Capability | Source system(s) | Aletheia requirement | Priority |
|---|---|---|---|
| Peer/identity modeling that evolves | Honcho | Models of people and agents that change over time, distinguishing their statements from our inferences | Alpha |
| Relevance-gated context injection | *(the gap Honcho left)* | Nothing enters a conversation without a scope + relevance decision | **Alpha — core** |
| Project scoping before ranking | OpenViking (scoped search) | Permissions/project membership resolved before any relevance model or LLM call | **Alpha — core** |
| Tiered loading (abstract → overview → source) | OpenViking | Short abstract, then summary, then source — agent/UI reads summary before opening full content | Alpha |
| Temporal / entity-aware recall | Hindsight | "What did we believe then" vs "what do we believe now"; entity resolution across time | Full release |
| Cross-memory reflection / contradiction surfacing | Hindsight | Surface contradictions with evidence; never silently merge | Full release |
| Fact extraction + dedup | Mem0 | Extract observations with source links; detect duplicates/corrections | Alpha |
| Context fencing (don't re-capture recalled material) | Supermemory | Recalled context is not re-ingested as new source unless independently introduced | Alpha |
| Pre-compression capture | ByteRover | Capture learning before context is discarded | Full release |
| Typed, inspectable records | Arsumbris | Our **own** typed substrate (Arsumbris unbundleable): structured records with schemas, not free text | **Alpha — core** |
| Governance hooks (mediate/act/observe) | Arsumbris au-mcp | Write path guarded by deterministic rules *before* model involvement; fail closed | **Alpha — core** |
| Evidence-linked reasoning | Honcho (grounded chat) | Conclusions cite source IDs; abstain when evidence absent/contradictory | Alpha |
| Trust / confidence handling | Holographic | Confidence recorded per observation; never upgrade inference to fact via retrieval | Alpha |

## What Aletheia must build itself (no reuse — license)

Per `license-verification.md`, Aletheia bundles nothing. Honcho and OpenViking core are AGPL (study
only); ByteRover is ELv2 (not reusable); Arsumbris is non-commercial (unbundleable); Holographic has
no identified canonical source. Five systems *are* permissively licensed (Hindsight MIT, Mem0
Apache-2.0, Supermemory MIT, RetainDB Apache-2.0, OpenViking crates/examples Apache-2.0 + the
`examples/hermes-plugin` MIT), but per the user's Option C decision Aletheia implements everything
independently and treats these as study and reference material only.

Aletheia therefore needs original implementations of:

1. **Typed file substrate** — Arsumbris's role. Schemas over structured files, with validation.
2. **Peer/identity model** — Honcho's role. Evolving representations with provenance.
3. **Hierarchical retrieval + tiered loading** — OpenViking's role.
4. **Scope/permission engine** — the gap none of them fully close; runs first, deterministically.
5. **Reasoning loop** — extract → retrieve → hypothesize → contradiction-check → conclude → cite.
6. **Ingestion pipeline** — conversations, documents, media (transcription/description), idempotent.
7. **Correction & temporal model** — supersession with lineage, effective-time queries.
8. **Deletion propagation** — remove source + derived content from all stores and exports.
9. **Hermes memory-provider adapter** — external provider interface.
10. **Electron desktop** — inspect evidence, correct memories, test recall, configure models/privacy.

## Observations, inferences, unknowns

**Observed (verified by inspection):**
- Honcho `hybrid` recall injects base context every turn with no relevance gate. **Code-verified** at
  `~/.hermes/plugins/honcho/client.py:327`, `__init__.py:155` and `:662`; `contextCadence` defaults to 1.
- Injection is placed in the **turn** (wrapped in `<memory-context>` fences), not the system prompt
  (plugin `README.md:44`) — placement preserves prompt caching.
- `saveMessages: true` persists turns including injected context back into storage — the ingestion-side
  carrier of contamination.
- The live Arien peer card contains `ATTRIBUTE: Owner of the ACS Constitutional Convention Letters`,
  proving ACS material was modeled into the derived identity layer.
- All five configured agent hosts run `hybrid` with default cadence — the defect is systemic.
- Arsumbris components are non-commercially licensed; cannot be bundled.
- OpenViking exposes a documented tiered-loading model (L0/L1/L2), and its `examples/hermes-plugin/`
  is MIT.
- Licenses verified via GitHub API and LICENSE file reads: Honcho AGPL-3.0, OpenViking AGPLv3 core /
  Apache-2.0 crates+examples / MIT hermes-plugin, Hindsight MIT, Mem0 Apache-2.0, Supermemory MIT,
  RetainDB Apache-2.0, ByteRover CLI **ELv2**, Arsumbris Sustainable Use.

**Inferred (reasoned, not directly verified):**
- A scope-before-ranking architecture will fix the ACS-class contamination more reliably than tuning
  injection size.
- Honcho's peer model and its unconditional injection are separable defects — the reasoning is sound,
  the injection policy is not.
- Identity-layer facts are a leak vector: content determines scope, not storage location.

**Unknown / not yet verified:**
- Hindsight's actual retrieval quality on our data (only published benchmarks seen).
- Holographic's canonical source — no identified upstream repo; the Hermes provider appears bundled.
- RetainDB retrieval quality for our use (Apache-2.0, 57 stars, low adoption).
- Arsumbris au-host license (unresolved).
- Whether our own typed substrate can reach Arsumbris's ergonomics without its code — Phase 1 will test this.
- Honcho behavior with `contextCadence > 1` (untested against a live server).
