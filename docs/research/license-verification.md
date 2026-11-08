# License Verification

**Phase:** 0 — Capability study
**Status:** Complete for components inspected; au-host and UI SDKs unresolved (no license file found at expected paths).
**Not legal advice.** This is engineering-level due diligence; obtain legal review before distribution.

## Summary

No AGPL component may be copied, linked, or bundled into Aletheia. Additionally, **Arsumbris components are under a non-commercial "Sustainable Use License" and may not be bundled into a distributable product.** Aletheia is therefore **Arsumbris-free by necessity and by prior user decision (Option C: our own code for everything).**

| Component | License | Distribution implication | Decision |
|---|---|---|---|
| Honcho (plastic-labs) | AGPL-3.0 *(verified via GitHub API: spdx_id AGPL-3.0, 7,576 stars)* | Derivative/combined works must be AGPL + source-available, network use counts as distribution | **Study only.** No copy, link, or bundle. May run externally as a separate benchmark baseline. |
| OpenViking core (volcengine) | AGPL-3.0 *(verified: root `LICENSE` is AGPLv3; GitHub API spdx_id AGPL-3.0)* | Same as above | **Study only.** |
| OpenViking `crates/` and `examples/` | **Apache-2.0** *(verified: `crates/LICENSE` and `examples/LICENSE` are Apache-2.0)* | Permissive; redistribution allowed with notice | Eligible for study/reference. Reuse still requires a per-file check. |
| OpenViking `examples/hermes-plugin/` | **MIT** *(verified: `Copyright (c) 2025 Nous Research`)* | Permissive | **Genuinely reusable reference** for the Phase 6 adapter design — it is a Hermes memory-provider plugin published under MIT. Study it for adapter shape; verify each file's license individually before copying. |
| Hindsight (vectorize-io) | MIT *(verified via GitHub API spdx_id MIT)* | Permissive, redistribution allowed with notice | Lowest reuse friction of the external systems. Still to be evaluated for fit; reuse decision deferred. |
| Mem0 (mem0ai/mem0) | Apache-2.0 *(verified via GitHub API spdx_id Apache-2.0, 67,009 stars)* | Permissive with patent grant and NOTICE requirement | Eligible for study; reuse deferred pending fit assessment. |
| Supermemory (`supermemoryai/supermemory`) | MIT *(verified via GitHub API spdx_id MIT; ~31,190 stars)* | Permissive | Eligible for study; reuse deferred. |
| RetainDB | Apache-2.0 *(verified via GitHub API spdx_id Apache-2.0, 57 stars)* | Permissive | Eligible for study; low adoption, verify independently. |
| ByteRover CLI (campfirein/byterover-cli) | **Elastic License 2.0 (ELv2)** *(verified: `LICENSE` file is ELv2; 4,955 stars)* | **Not open source.** Permits internal use but prohibits providing the software as a managed/hosted service and restricts circumvention of license keys | **Study only. Not reusable.** ELv2 is not a permissive license — do not treat as MIT despite community forks carrying MIT. |
| Holographic memory | **Unresolved / likely not applicable.** No canonical upstream repo identified. Search returns `writerslogic/holographic-memory` (AGPL-3.0, 21 stars) and unrelated projects. The Hermes provider appears to be a bundled plugin, not a distinct open-source project. | Unknown | **Treat as unusable until a canonical source is identified.** Do not assume MIT. |
| Arsumbris au-engine | Sustainable Use License (Volvelle Labs GmbH), v1.0 | **Non-commercial / internal-use only.** May not be distributed in a product. | **Study only. Cannot be a build dependency.** |
| Arsumbris au-mcp | Sustainable Use License, v1.0 | Same as above | **Study only.** |
| Arsumbris au-type-system | Sustainable Use License, v1.0 | Same as above | **Study only.** |
| Arsumbris au-host | Unresolved — no LICENSE at repo root (404) | Unknown | **Treat as unusable until resolved.** Do not bundle. |
| Arsumbris au-host-sdk, au-mcp-sdk, au-mcp-core, au-mcp-adapter-cc | Unresolved — no LICENSE file; `package.json` has no `license` field | Unknown; npm SPDX absent means "all rights reserved" by default | **Treat as unusable until resolved.** Do not bundle. |

**Note on the reuse picture:** five systems are now confirmed permissive (Hindsight MIT, Mem0
Apache-2.0, Supermemory MIT, RetainDB Apache-2.0, OpenViking's crates/examples/hermes-plugin
Apache-2.0/MIT). This *loosens* the constraint relative to the first revision, which had asserted
"every reference is study-only except Hindsight" without having screened the rest. It does not
change the Option C decision: the user chose independently-written code, and Aletheia will not bundle any
of these. They are study and reference material, and the MIT Hermes plugin is specifically useful as
an adapter-design reference.

**ByteRover is the notable new exclusion:** ELv2 is source-available but not open source, and it
prohibits offering the software as a hosted service — directly relevant to a self-hostable
competitor. Previously unscreened; now excluded.

## Arsumbris Sustainable Use License — key limitation (verbatim)

> "You may use or modify the software only for your own internal business purposes or for
> non-commercial or personal use. You may distribute the software or provide it to others only
> if you do so free of charge for non-commercial purposes."

Other relevant terms: copyright/copyright notices must be preserved; modified copies must carry a
prominent "you have modified this" notice; patent license terminates if you assert the software
infringes a patent; license terminates on violation with a 30-day cure window; non-transferable,
non-sublicensable.

**Implication:** Aletheia — a self-hostable memory provider intended for distribution and market
competition — may not ship any of these components. This is stricter than AGPL: it also blocks
non-commercial redistribution of a product containing them.

## Rules adopted for Aletheia

1. **Aletheia's own code:** independently written. Permissive license (MIT or Apache-2.0, final choice pending).
2. **No AGPL source copied, linked, or bundled.** Honcho and OpenViking are study references and external benchmark baselines only.
3. **No Arsumbris component bundled, linked, or shipped** in any form. Study their documented architecture and behavior; implement our own typed substrate.
4. **Dependency screening at intake:** every new third-party dependency must have its license read and recorded before it enters the tree. Absence of a license declaration means "not usable" until resolved.
5. **Model licenses are separate** from code licenses and must be checked independently for any bundled or default-configured model.
6. **Publish a component inventory** before alpha distribution, listing every dependency, its license, and its obligation.

## Open items

- [ ] Resolve au-host / UI SDK licenses (no license file located; owner contact may be required).
- [ ] Confirm final Aletheia license (MIT vs Apache-2.0) — user decision, not yet made.
- [ ] Verify Hindsight's suitability independently of its permissive license.
- [ ] Full transitive dependency audit once the codebase exists.
