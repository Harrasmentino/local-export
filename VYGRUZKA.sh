#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${LOCAL_EXPORT_PYTHON:-}
NODE=${LOCAL_EXPORT_NODE:-}
MODULES=${LOCAL_EXPORT_MODULES:-"$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"}
if [ -z "$PYTHON" ]; then
  BUNDLED_PYTHON="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
  if [ -x "$BUNDLED_PYTHON" ]; then PYTHON=$BUNDLED_PYTHON
  elif command -v python3 >/dev/null 2>&1; then PYTHON=$(command -v python3)
  else echo 'Python 3 with python-docx is required.' >&2; exit 1
  fi
fi
if [ -z "$NODE" ]; then
  BUNDLED_NODE="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
  if [ -x "$BUNDLED_NODE" ]; then NODE=$BUNDLED_NODE
  elif command -v node >/dev/null 2>&1; then NODE=$(command -v node)
  else echo 'Node.js is required for fixed Excel reports.' >&2; exit 1
  fi
fi
if [ ! -f "$MODULES/@oai/artifact-tool/package.json" ]; then
  echo '@oai/artifact-tool is missing. See docs/FIXED_REPORTS.md.' >&2
  exit 1
fi
if [ ! -e "$ROOT/_local/node_modules" ] && [ ! -L "$ROOT/_local/node_modules" ]; then
  ln -s "$MODULES" "$ROOT/_local/node_modules"
fi
export LOCAL_EXPORT_NODE="$NODE"
export LOCAL_EXPORT_MODULES="$MODULES"
exec "$PYTHON" -X utf8 "$ROOT/local_export.py" "$@"