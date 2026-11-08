# Phase 7 — Gated Cutover Checklist

**Plan:** `~/.hermes/plans/2026-10-24_aletheia-system-full-plan.md`
**Current HEAD:** `e88dff4` (Phase 7 checklist updated; provenance completed; build verification added)

## Gates

Each gate must be PASS before the next is attempted.

1. **Scoped recall demonstrated**
   - **PASS:** Phases 3–6 implemented tiered retrieval with scope engine, tiered loading, and RecallReceipt.
   - **Artifacts:** `aletheia/retrieval.py`, `aletheia/server.py`, `aletheia/shadow.py`, tests/test_server.py shadow tests.
   - **Evidence:** 429 passing tests, including shadow recall/reason/compare.

2. **Grounded reasoning measured**
   - **PASS:** Evaluation set v1 committed (`docs/evaluation/set-v1.md`) with Phase 7 runner (`tests/test_evaluation.py`). Runner invokes actual `synthesize` and `contradict` functions with fake model layer. Validation is per-case: E1/E4/E6 check gold-answer tokens and citations; E2/E5 verify Abstention with matching gold-answer reasons; E3 verifies conflict IDs. E4 directly asserts retrieval excludes out-of-scope records. E5 proves content_conflict denial via authorized_read reason. All 6 cases pass.
   - **Artifacts:** `docs/evaluation/set-v1.md`, `tests/test_evaluation.py`
   - **Evidence:** 7 evaluation tests passing.

3. **Crash recovery verified**
   - **PASS:** Added `test_crash_mid_write_leaves_no_partial_record_in_raw_tables` (kill-mid-write via SIGKILL, verify uncommitted record absent from raw records, journal, and FTS tables via direct SQL).
   - **Artifacts:** `tests/test_substrate.py`
   - **Evidence:** 429 tests passing.

4. **Deletion verified**
   - **PASS:** Added `test_deleted_records_absent_from_export` verifying `delete()` cascades to `export_scope()` output.
   - **Artifacts:** `tests/test_export_import.py`
   - **Evidence:** 429 tests passing.

5. **License inventory complete**
   - **PASS:** AGPL LICENSE file present. `docs/research/license-verification.md` exists and covers inspected components.
   - **Residual:** au-host and UI SDKs unresolved. Not in Aletheia distribution scope.
   - **Note:** Engineering-level verification only. Legal review before distribution.

6. **Resource limits documented**
   - **PASS:** `docs/operational-requirements.md` contains resource limits table (Disk 4/16 GB, RAM 4/8 GB, CPU 2/4+ cores).
   - **Residual:** None identified.

7. **Independent review passed**
   - **PASS:** Phase 6 adversarial review APPROVED (17th review, 0 findings). Phase 7 adversarial review APPROVED (11th review, 0 findings). Subsequent clean-room provenance fixes reviewed and cleared.
   - **Note:** All Phase 7 additions reviewed and approved.

## Next Actions

1. Tag `v0.1.0a0` and push to origin.
2. Create GitHub release with wheel and install instructions.
