#!/usr/bin/env bash
set -euo pipefail

# verify-install.sh — fresh-venv install, smoke test, and uninstall for Aletheia alpha.
# Requires: python3.11+, pip, wheel in dist/

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

if [ ! -d "dist" ] || [ -z "$(ls dist/*.whl 2>/dev/null)" ]; then
  echo "Error: no wheel found in dist/; run scripts/build-release.sh first" >&2
  exit 1
fi

WHEEL="$(ls dist/*.whl | head -n 1)"
VENV="$(mktemp -d -t aletheia-install-verify-XXXXXXXXXX)"
PYTHON_BIN="${ALETHEIA_PYTHON:-python3.11}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi
PY_VER="$("${PYTHON_BIN}" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
PY_MAJOR="$("${PYTHON_BIN}" -c 'import sys; print(sys.version_info.major)')"
PY_MINOR="$("${PYTHON_BIN}" -c 'import sys; print(sys.version_info.minor)')"
if [ "${PY_MAJOR}" -lt 3 ] || [ "${PY_MAJOR}" -eq 3 -a "${PY_MINOR}" -lt 11 ]; then
  echo "Error: ${PYTHON_BIN} is ${PY_VER}, but Aletheia requires Python >= 3.11" >&2
  exit 1
fi
umask 0077
"${PYTHON_BIN}" -m venv "${VENV}"
"${VENV}/bin/python" -m pip install --quiet "${WHEEL}"

echo "==> Checking entrypoint"
"${VENV}/bin/aletheia" --help >/dev/null
"${VENV}/bin/aletheia" --db /tmp/aletheia-install-test.db serve --help >/dev/null

echo "==> Uninstalling"
"${VENV}/bin/python" -m pip uninstall -y aletheia >/dev/null

echo "==> Verifying uninstall"
if [ -x "${VENV}/bin/aletheia" ]; then
  echo "Error: aletheia entrypoint still exists after uninstall" >&2
  exit 1
fi
if "${VENV}/bin/python" -c "import sys; sys.path = [p for p in sys.path if p]; import aletheia" 2>/dev/null; then
  echo "Error: aletheia package still importable after uninstall" >&2
  exit 1
fi

echo "==> Done. Install/uninstall cycle verified for ${WHEEL}"
