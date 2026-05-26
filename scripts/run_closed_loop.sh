#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-$PROJECT_ROOT/examples/c_town/config.yaml}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
ITERATIONS="${ITERATIONS:-100}"

cd "$PROJECT_ROOT"

sudo "$PYTHON_BIN" -m src.runtime.persistent_closed_loop \
  --config "$CONFIG" \
  --iterations "$ITERATIONS" \
  --python "$PYTHON_BIN" \
  --physics-mode dhalsim_epynet \
  --init-style dhalsim \
  --logic-wait 0.3
