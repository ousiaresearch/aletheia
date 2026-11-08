# Alpha Release Checklist

**Target:** Aletheia 0.1.0a0 — first alpha distribution
**Status:** Pre-release verification

## Gates

Each gate must be verified before the alpha tag is cut.

- [x] **G1: Tests passing** — Full test suite green on clean checkout. Verified: 429 passing.
- [x] **G2: Adversarial review current** — No open BLOCK findings on HEAD. Last review: APPROVED with 0 findings on `e22f64b`; subsequent clean-room fixes reviewed and cleared.
- [x] **G3: LICENSE present** — AGPL-3.0 `LICENSE` file at repo root.
- [x] **G4: pyproject.toml valid** — `python3 -m build --wheel` succeeds without error. Verified via `scripts/build-release.sh`.
- [x] **G5: README accurate** — Status, install, quick-start, and invariants match current code.
- [x] **G6: Docs present** — `docs/operational-requirements.md` includes resource limits and crash recovery.
- [x] **G7: Clean-room provenance** — `docs/clean-room-provenance.md` lists all inspected components and reuse decisions. All 5 systems named with dates, sources, access modes, recorded use, and Aletheia spec citations.
- [x] **G8: No secrets in repo** — `git secret` scan or equivalent returns clean.
- [x] **G9: Wheel buildable** — `dist/aletheia-0.1.0a0-py3-none-any.whl` exists and installs in a fresh venv. Verified via `scripts/build-release.sh`.
- [x] **G10: Entrypoint works** — `aletheia --help` and `aletheia --db /tmp/test.db serve --help` succeed.

## Artifacts

- Wheel: `dist/aletheia-0.1.0a0-py3-none-any.whl`
- LICENSE: `LICENSE`
- README: `README.md`
- Install docs: `docs/local-cli.md`
- Operational requirements: `docs/operational-requirements.md`
- Provenance: `docs/clean-room-provenance.md`
- Changelog: `CHANGELOG.md`

## Next actions

1. Verify G1–G10 on a clean checkout.
2. Tag `v0.1.0a0` and push to origin.
3. Create GitHub release with wheel and install instructions.
