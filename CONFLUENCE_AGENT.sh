#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${LOCAL_EXPORT_PYTHON:-}
if [ -z "$PYTHON" ]; then
  BUNDLED="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
  if [ -x "$BUNDLED" ]; then
    PYTHON=$BUNDLED
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON=$(command -v python3)
  else
    echo 'Python 3 with Tkinter is required. See README.md.' >&2
    exit 1
  fi
fi
if [ ! -x "$PYTHON" ]; then
  echo 'LOCAL_EXPORT_PYTHON does not point to an executable Python.' >&2
  exit 1
fi
exec "$PYTHON" -X utf8 "$ROOT/agent_export.py" "$@"