# Failure Mode: ACS Release History Injected Into Unrelated Conversations

**Phase:** 0 — Capability study
**Status:** Root cause identified from live configuration inspection. Not a hypothesis.

## Symptom

While discussing memory providers, memory-system architecture, and project naming, a ~10–14 kB
block summarizing the Agentic Commonwealth Society's RC-01 through RC-05 release stages was
injected into the conversation on every turn. It was irrelevant to the conversation and
recurred repeatedly.

**Placement (canonical statement):** the block is injected into the **turn at API-call time**,
wrapped in `<memory-context>` fences — not into the system prompt. Per the plugin's own
`README.md:44`, this placement preserves prompt caching; only a static mode header enters the system
prompt. The defect is the unconditional injection, not its placement; see "Verification of the
load-bearing claim" below for the code evidence.

*(Correction of a prior revision: this section and the Root cause section below previously stated
"injected into the system prompt". That was wrong — the correction was applied in the verification
section but not here, leaving the document contradicting itself on its central factual claim.)*

## Root cause

**Honcho `recallMode: hybrid` injects base context into the conversation automatically on every
turn, gated only by `contextCadence` (default 1 — every turn).** There is no relevance filter
between Honcho's stored context and the current conversation. Base context = session summary +
user representation + peer card, assembled server-side and injected wholesale.

Live configuration inspected (`~/.hermes/profiles/arien/honcho.json`), host `hermes_arien`:

```json
{
  "recallMode": "hybrid",
  "writeFrequency": "async",
  "sessionStrategy": "per-session",
  "dialecticReasoningLevel": "low",
  "saveMessages": true,
  "peerName": "Anduril",
  "aiPeer": "arien-heron",
  "workspace": "arien-heron-continuity-20260910",
  "enabled": true
}
```

Verbatim from `~/.hermes/profiles/arien/honcho.json`, host `hermes_arien`. Note what is **absent**:
`contextCadence` and `dialecticCadence` are both unset, so both take their defaults (1 and 1
respectively — see below).

*(Correction of a prior revision: an earlier version of this document quoted `dialecticCadence: 2` in
this block. That key does not exist here; the value 2 belongs to the `hermes` host in the root
config file. The block was misquoted. Live `hermes honcho status` reports "Dialectic cad: every 1
turn" for this profile, confirming the default.)*


**Contributing factor:** the ACS summary is large and structurally prominent (session summary with
five numbered sections), so it dominates the injected payload regardless of relevance.

**Not the cause:** Honcho's peer-modeling or dialectic reasoning. Those are the valuable parts.
The defect is specifically the *unconditional injection* of accumulated context, not the reasoning
about peers.

## Why pruning local memory did not fix it

Local `MEMORY.md` / `USER.md` and Honcho are two separate layers. Pruning local notes (1,194/3,500
chars remaining) reduced the built-in layer only. Honcho's injection continued unchanged, which is
why the ACS block reappeared after each prune. **Any fix that touches only one layer will not hold.**

## Mitigation available today (not applied)

`hermes honcho mode tools` sets `recallMode: tools` — no auto-injection; the agent calls
`honcho_search` / `honcho_reasoning` explicitly. This stops unconditional injection immediately.
Trade-off: recall becomes the agent's responsibility rather than automatic, so relevant context may
be missed when the agent does not think to ask.

`hermes honcho tokens 1200` caps the injected budget instead, trading completeness for size — but
stale material still enters. Weaker fix.

## Requirements this imposes on Aletheia

1. **Relevance filtering must precede injection.** Context is selected against the present query,
   not appended unconditionally. A memory system that injects its entire accumulated state every
   turn is reproducing the defect by design.
2. **Project scoping must gate retrieval**, not merely rank it. ACS material must be excluded from a
   memory-provider discussion by a permission/scope decision that runs *before* any relevance model.
3. **Layered memory must not be confused with one memory.** If a system has both local notes and a
   service, "reducing memory" must address every layer, and the system should report which layers
   were affected.
4. **Injection must be explainable and auditable.** Every injected item should be attributable —
   which scope, which source, why included. Aletheia's RecallReceipt requirement covers this.
5. **Automatic and on-demand recall are both legitimate, but must be scoped.** Default to a
   small, always-eligible set (identity, standing preferences, current active project) rather than
   the whole archive.

## Verification of the load-bearing claim

The `contextCadence` default of `1` and the every-turn injection behavior are **code-verified**, not
merely documented. The installed plugin at `~/.hermes/plugins/honcho/` contains:

- `client.py:327` — `` `"context_cadence": look.parsed("contextCadence", int, 1)` `` — the config
  lookup supplies `1` as the default when the key is absent.
- `__init__.py:155` — `` self._context_cadence = 1  # minimum turns between context API calls ``
- `__init__.py:662` — `` context_due = self._context_cadence <= 1 or (self._turn_count - self._last_context_turn) >= self._context_cadence ``
  — with the default of 1, `context_due` is true on every turn.

The live `honcho.json` sets no `contextCadence`, so `1` applies. Hermes's shipped documentation
agrees (`website/docs/user-guide/features/honcho.md:119`, `` `contextCadence` | `1` `` = "Minimum
turns between `context()` API calls (base layer refresh)").

*(Correction of a prior revision: this section previously claimed the plugin implementation "was not
located in this source tree" and that the default was only documentation-verified. That was wrong —
the plugin is installed at `~/.hermes/plugins/honcho` and the default is code-confirmed at the
lines above. The earlier search looked only under `~/.hermes/hermes-agent/`, which is the Hermes
source tree, not the plugin directory.)*

**Remaining limitation:** the code confirms the default and the gate. Behavior with `contextCadence`
set greater than 1 has not been exercised against a live server, so cadence-based throttling is
untested here. This does not affect the finding: at the default, injection is unconditional.

**Injection placement (corrected):** the context is injected into the **turn at API-call time**,
wrapped in `<memory-context>` fences — not into the system prompt. Per the plugin's own
`README.md:44`, this placement preserves prompt caching; only a static mode header enters the system
prompt. The mechanism (unconditional per-turn injection) is unaffected; only the placement is
corrected. This is why the block reappears verbatim in each turn's context rather than being
absorbed into the system prompt.

## Ingestion side (added after adversarial review)

The analysis above covers retrieval only. **The ingestion side is where the contamination entered.**

`saveMessages: true` persists every turn — *including the injected `<memory-context>` block* — back
into Honcho. So the ACS summary was:

1. Injected into a turn (retrieval-side defect).
2. Persisted verbatim as part of the next stored message (ingestion-side defect).
3. Modeled upward into the peer card by peer modeling.

The live Arien peer card proves step 3: it contains `ATTRIBUTE: Owner of the ACS Constitutional
Convention Letters`. ACS material is not merely recalled — it has been **written into the derived
identity layer**.

**Consequence:** a scope gate at retrieval time does not remediate contamination already persisted
in derived artifacts. Aletheia must address both:
- Gate at injection/recall (prevent new contamination).
- Provenance and scope on derived artifacts, plus an explicit remediation path for existing
  contaminated state (tagging, re-derivation, or deletion — with lineage retained where policy
  permits).

## Refinement: peer modeling is the carrier, not the cause

*(Corrected after adversarial review.)* The earlier statement "Not the cause: Honcho's peer-modeling
or dialectic reasoning" was too categorical. Precisely:

- **The defect** is the unconditional injection policy.
- **The carrier** is peer modeling — it is what persists and amplifies the contamination across
  sessions by writing it into the peer card, which is itself always-eligible context.

Both must be addressed: gate at injection, and apply provenance + scope to derived artifacts. A fix
that only stops injection leaves the already-contaminated peer card in place.

## Pruning figure provenance

*(Corrected after adversarial review.)* The earlier claim "Pruning local notes (1,194/3,500 chars
remaining)" cited no source. The figure comes from the `memory` tool's own usage report after the
prune operation in this conversation (`{"usage": "34% — 1,194/3,500 chars", "entry_count": 4}`),
against the configured `memory_char_limit: 3500` in `config.yaml`. It is reproducible from the
tool's return value, not from an independent measurement. The substantive point stands: local notes
were pruned substantially, and the ACS block reappeared regardless — proving the two layers are
independent.

## Scope of the defect (expanded)

The original analysis examined only the `arien` profile. A full sweep of every configured Honcho
host shows the defect is **systemic, not profile-specific**:

| Config file | Host | recallMode | contextCadence | sessionStrategy |
|---|---|---|---|---|
| `~/.hermes/honcho.json` | hermes | hybrid | UNSET → default 1 | per-session |
| `~/.hermes/honcho.json` | hermes_arien | hybrid | UNSET → default 1 | per-session |
| `~/.hermes/honcho.json` | hermes_councilregistrar | hybrid | UNSET → default 1 | per-session |
| `~/.hermes/honcho.json` | hermes_councilreviewer | hybrid | UNSET → default 1 | per-session |
| `~/.hermes/honcho.json` | hermes_isildur | hybrid | UNSET → default 1 | per-session |
| `profiles/arien/honcho.json` | hermes | hybrid | UNSET → default 1 | per-session |
| `profiles/arien/honcho.json` | hermes_arien | hybrid | UNSET → default 1 | per-session |

**Every host on this machine runs `hybrid` recall with the default cadence.** No host has an
explicit `contextCadence`. This means the unconditional-injection policy affects all five agent
profiles, and each accumulates its own contamination independently.

**Additional finding — config precedence is a live hazard.** Two config files both define the
`hermes` and `hermes_arien` hosts with conflicting `aiPeer` and `workspace` values
(`~/.hermes/honcho.json` and `~/.hermes/profiles/arien/honcho.json`). Resolution order is
`$HERMES_HOME/honcho.json` > `~/.hermes/honcho.json` > `~/.honcho/config.json`, so the profile file
wins for the Arien profile — but which file a given run reads depends on `$HERMES_HOME`, which is a
runtime property, not a static one. A run with an unset `HERMES_HOME` reads the root file and
resolves to a *different peer and workspace* than the same run under the profile.

**Consequence for Arien specifically:** the hazard is **cross-workspace contamination, not inherited
Anastasia representation**. Resolution order is `$HERMES_HOME/honcho.json` > `~/.hermes/honcho.json` >
`~/.honcho/config.json`, so the profile file normally wins — but which file a run reads depends on
`$HERMES_HOME`, a runtime property. Host selection is by profile key (`profile_host_key`,
`client.py:60-65`): a profile-`arien` run resolves to host key `hermes_arien`, not `hermes`.

The actual divergence is between the two `hermes_arien` blocks:

| Source | `aiPeer` | `workspace` |
|---|---|---|
| `~/.hermes/profiles/arien/honcho.json` | `arien-heron` | `arien-heron-continuity-20260910` |
| `~/.hermes/honcho.json` (fallback) | `arien` | `hermes` (shared) |

A run with an unset `$HERMES_HOME` reads the root file and resolves to peer `arien` in the **shared
`hermes` workspace** — a different, broadly-shared memory space — instead of Arien's dedicated
`arien-heron-continuity-20260910` workspace. So the hazard is Arien's continuity being drawn from, or
written into, a workspace shared with other agents, rather than reading Anastasia's representation.

*(Correction of a prior revision: this section previously claimed such a run "would read Anastasia's
accumulated representation as Arien's base context" because "the aiPeer in the root file is
anastasia". That misattributed the block — `anastasia` is the `aiPeer` of the root file's `hermes`
host, which is selected only for the *default* profile, not for `arien`. The real hazard is the
workspace divergence shown above. The underlying concern — config resolution being runtime-dependent —
stands and is unchanged.)*

**Requirement added to Aletheia:** configuration resolution must be deterministic and reported. The
system must state which config, which peer, and which workspace served a given recall — not leave it
to `$HERMES_HOME` resolution at runtime. Ambiguous or conflicting host definitions must fail closed,
not silently pick a winner.



> A query about memory providers must return zero ACS release history unless the query explicitly
> concerns ACS.
