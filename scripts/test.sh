#!/bin/sh
# Run the Aletheia suite with an environment that cannot leak another Python into the venv.
# An inherited PYTHONPATH (set by agent runtimes) made numpy load from the wrong install,
# which broke the real-model tests intermittently. Always run the suite through this script.
cd "$(dirname "$0")/.." || exit 1
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV .venv/bin/python -m pytest -q -p no:cacheprovider "$@"
