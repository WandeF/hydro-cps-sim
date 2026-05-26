#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
CONFIG="$PROJECT_ROOT/examples/c_town/config.yaml"

if [[ $# -gt 0 && "$1" != --* ]]; then
  CONFIG="$1"
  shift
fi

cd "$PROJECT_ROOT"

"$PYTHON_BIN" -m src.check.run \
  --config "$CONFIG" \
  "$@"
