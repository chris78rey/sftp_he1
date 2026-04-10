#!/usr/bin/env sh
set -eu

DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/run_local.py"
