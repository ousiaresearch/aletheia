# Phase 8 — Beta-Readiness

**Prereq:** Alpha release `v0.1.0a0` published and approved.

**Goal:** Prepare Aletheia for beta distribution with hardened packaging, security review, and installation verification.

**Scope:**
- Artifact verification (wheel + metadata)
- Security audit of entrypoints, temp files, and egress surfaces
- Regression test for install/uninstall cycle
- PyPI publishing runbook (token creation, twine upload)

**Out of scope:** Wheel signing (deferred to Phase 9+), new memory features, model additions, server hardening beyond installer, hardened pipx/systemd install docs.

**Deliverables:**
1. `docs/design/phase8-beta-readiness.md` (this doc)
2. `docs/pypi-publishing.md` — step-by-step token creation and upload
3. `scripts/verify-install.sh` — fresh-venv install + uninstall + smoke test
4. `scripts/build-release.sh` — wheel build with interpreter selection and umask 0077
5. `tests/test_install.py` — install/uninstall regression with umask 0077
6. `docs/security-audit-alpha.md` — alpha security audit

**Gates:**
- G1: Wheel builds, metadata valid, and installs cleanly in fresh venv.
- G2: Security audit APPROVED (independent adversarial review, 2026-10-29, 0 findings).
- G3: PyPI runbook reviewed and executed; `aletheia==0.1.0a0` published to https://pypi.org/project/aletheia/0.1.0a0/.
- G4: Install/uninstall regression passing.

**Risks:**
- PyPI token not yet available; publishing blocked until token is provided
- Apple Silicon-only local-embedding extra may confuse pip on x86; document platform markers
