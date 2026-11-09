#!/bin/sh
# Stage Aletheia 2.1 into the local Hermes plugin directory, clear the stale plugin-source
# snapshots, and rename the live store's pre-2.1 scope.
#
# The gateway must be stopped first: it holds the store open, and the rename takes a write lock.
# This script never restarts the gateway and never deletes a store.
#
#   ./scripts/deploy_local.sh              # dry run: print every action
#   ./scripts/deploy_local.sh --apply      # perform them
#
# After --apply, start the gateway and run `python scripts/verify_deployed.py`.
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
HERMES_HOME=${HERMES_HOME:-$HOME/.hermes}
PLUGIN_DIR="$HERMES_HOME/plugins/aletheia"
STORE="$HERMES_HOME/aletheia/aletheia.sqlite3"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

say() { printf '%s\n' "$*"; }
run() {
    if [ "$APPLY" = "1" ]; then
        say "  + $*"
        "$@"
    else
        say "  would run: $*"
    fi
}

say "repository : $REPO"
say "hermes home: $HERMES_HOME"
say "store      : $STORE"
say ""

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/aletheia/__init__.py")
say "staging version $VERSION into $PLUGIN_DIR"
for path in aletheia plugin.yaml pyproject.toml README.md CHANGELOG.md; do
    if [ -e "$REPO/$path" ]; then
        if [ "$APPLY" = "1" ]; then
            mkdir -p "$PLUGIN_DIR"
            # No --delete: the destination is the user's plugin checkout, not a mirror.
            rsync -a --exclude '__pycache__' --exclude '*.pyc' "$REPO/$path" "$PLUGIN_DIR/"
        fi
        say "  staged $path"
    fi
done
say ""

say "clearing plugin-source snapshots (they are keyed by path, not content, so a stale copy survives an update)"
for snapshot in "$HERMES_HOME"/installs/*/environments/*/workspace/plugin-sources/aletheia-*; do
    [ -e "$snapshot" ] || continue
    say "  ${snapshot}"
    [ "$APPLY" = "1" ] && rm -rf "$snapshot"
done
say ""

if [ -f "$STORE" ]; then
    say "renaming the live store's pre-2.1 scope"
    run env ALETHEIA_REPO="$REPO" "$REPO/.venv/bin/python" - "$STORE" <<'PY'
import os
import sys
sys.path.insert(0, os.environ["ALETHEIA_REPO"])
from aletheia.merge import scope_counts, upgrade_scope
store = sys.argv[1]
print("  before:", scope_counts(store))
result = upgrade_scope(store, "profile-.hermes", "profile:default")
print("  changed:", result.get("changed"), "vectors:", result.get("vectors"))
print("  refused:", result.get("refused") or "(none)")
print("  after:", scope_counts(store))
PY
else
    say "no store at $STORE — nothing to rename"
fi

say ""
say "next:"
say "  1. start the gateway (or restart the launchd service)"
say "  2. python $REPO/scripts/verify_deployed.py"
say "  3. confirm the ready line names version=$VERSION scope=profile:default"