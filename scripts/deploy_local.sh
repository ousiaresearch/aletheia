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

# The environment imports the plugin from its own copied workspace member, and its editable install
# points there — not at this checkout and not at the plugins directory. So the member has to be
# replaced, and its dist metadata refreshed, or the gateway keeps importing the old copy while
# `plugins_dir` and the repository both read as current. That is how a 2.0.0 snapshot served a 2.0.1
# repository, and how an environment came to report "no drift" while holding 2.0.1 metadata.
ACTIVE_ENV=$(python3 - "$HERMES_HOME" <<'PY'
import glob, json, os, sys
home = sys.argv[1]
facts = sorted(glob.glob(os.path.join(home, "installs", "*", "facts.json")),
               key=os.path.getmtime, reverse=True)
for path in facts:
    try:
        document = json.load(open(path, encoding="utf-8"))
        environment = document["packages"]["venv"]["environment"]
    except Exception:
        continue
    if os.path.isdir(environment):
        print(environment)
        break
PY
)

# facts.json names the venv; the workspace it manages sits beside it, in the environment directory.
ACTIVE_ROOT=""
if [ -n "$ACTIVE_ENV" ]; then
    case "$ACTIVE_ENV" in
        */venv) ACTIVE_ROOT=$(dirname "$ACTIVE_ENV") ;;
        *) ACTIVE_ROOT="$ACTIVE_ENV" ;;
    esac
fi

say "refreshing the plugin-source member for the resolved environment"
for existing in "$HERMES_HOME"/installs/*/environments/*/workspace/plugin-sources/aletheia-*; do
    [ -e "$existing" ] || continue
    case "$existing" in
        "$ACTIVE_ROOT"/workspace/plugin-sources/*) say "  keeping ${existing} (this environment is the one that runs)" ;;
        *) say "  removing ${existing} (a retired environment's copy)"; [ "$APPLY" = "1" ] && rm -rf "$existing" ;;
    esac
done
if [ -n "$ACTIVE_ROOT" ]; then
    MEMBER=$(ls -d "$ACTIVE_ROOT"/workspace/plugin-sources/aletheia-* 2>/dev/null | head -1)
    if [ -z "$MEMBER" ]; then
        # Same key shape Hermes uses: <plugin dir name>-<sha256(plugin dir path)[:16]>.
        KEY="aletheia-$(printf '%s' "$PLUGIN_DIR" | shasum -a 256 | cut -c1-16)"
        MEMBER="$ACTIVE_ROOT/workspace/plugin-sources/$KEY"
    fi
    say "  member: $MEMBER"
    if [ "$APPLY" = "1" ]; then
        mkdir -p "$MEMBER"
        # --delete here: the member is a generated copy, unlike the plugin checkout.
        rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' --exclude '.venv' \
            "$PLUGIN_DIR"/ "$MEMBER"/
    fi
    UV=${UV:-$(command -v uv || printf '%s' "$HERMES_HOME/bin/uv")}
    if [ -x "$UV" ] && [ -x "$ACTIVE_ENV/bin/python" ]; then
        say "  installed distribution metadata is refreshed against that member:"
        run "$UV" pip install --python "$ACTIVE_ENV/bin/python" --no-deps --reinstall -e "$MEMBER"
    else
        say "  no uv or no interpreter at $ACTIVE_ENV/bin/python — metadata not refreshed"
    fi
else
    say "  no resolved environment in facts.json — nothing to refresh"
fi
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