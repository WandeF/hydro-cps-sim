#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
CONFIG="$PROJECT_ROOT/examples/c_town/config_mitm_plc9.yaml"
START_ITERATION="${START_ITERATION:-}"
END_ITERATION="${END_ITERATION:-}"
PHYSICS_COLUMNS="${PHYSICS_COLUMNS:-}"
SCADA_COLUMNS="${SCADA_COLUMNS:-}"

usage() {
  cat <<EOF
Usage:
  bash scripts/compare_results.sh [config.yaml] [options]

Options:
  --config PATH       Config file. Default: examples/c_town/config_mitm_plc9.yaml
  --start N           Attack window start iteration. Default: inferred from config, fallback 20
  --end N             Attack window end iteration. Default: inferred from config, fallback 40
  --physics "COLS"    Space-separated physics columns. Default: inferred from config
  --scada "COLS"      Space-separated SCADA columns. Default: inferred from config
  --no-plots          Skip line plot generation
  -h, --help          Show this help

Environment:
  PYTHON_BIN          Python executable. Default: python3 in PATH
EOF
}

NO_PLOTS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG="$2"; shift 2 ;;
    --start)
      START_ITERATION="$2"; shift 2 ;;
    --end)
      END_ITERATION="$2"; shift 2 ;;
    --physics)
      PHYSICS_COLUMNS="$2"; shift 2 ;;
    --scada)
      SCADA_COLUMNS="$2"; shift 2 ;;
    --no-plots)
      NO_PLOTS=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    --*)
      echo "[ERROR] Unknown option: $1" >&2
      usage
      exit 2 ;;
    *)
      CONFIG="$1"; shift ;;
  esac
done

cd "$PROJECT_ROOT"
CONFIG="$(realpath "$CONFIG")"
CASE_DIR="$(dirname "$CONFIG")"
OUTPUT_DIR="$CASE_DIR/output"
BASELINE_DIR="$CASE_DIR/baseline"
RUNTIME_DIR="$OUTPUT_DIR/runtime"
REPORTS_DIR="$OUTPUT_DIR/reports"

if [[ ! -d "$BASELINE_DIR" ]]; then
  echo "[ERROR] baseline directory not found: $BASELINE_DIR" >&2
  exit 1
fi
if [[ ! -d "$RUNTIME_DIR" ]]; then
  echo "[ERROR] runtime directory not found: $RUNTIME_DIR" >&2
  exit 1
fi

sudo_maybe() {
  if sudo -n true 2>/dev/null; then
    sudo -n "$@"
  elif [[ -t 0 ]]; then
    sudo "$@"
  else
    "$@"
  fi
}

echo "[EXPORT] $REPORTS_DIR"
sudo_maybe "$PYTHON_BIN" "$PROJECT_ROOT/scripts/export_results.py" \
  --config "$CONFIG" \
  --runtime-dir "$RUNTIME_DIR" \
  --reports-dir "$REPORTS_DIR"
sudo_maybe chown -R "$(id -u):$(id -g)" "$REPORTS_DIR" 2>/dev/null || true

COMPARE_ARGS=()
COMPARE_ARGS+=(--config "$CONFIG")
if [[ -n "$START_ITERATION" ]]; then
  COMPARE_ARGS+=(--start "$START_ITERATION")
fi
if [[ -n "$END_ITERATION" ]]; then
  COMPARE_ARGS+=(--end "$END_ITERATION")
fi
if [[ -n "$PHYSICS_COLUMNS" ]]; then
  read -r -a PHYSICS_ARRAY <<< "$PHYSICS_COLUMNS"
  COMPARE_ARGS+=(--columns "${PHYSICS_ARRAY[@]}")
fi
if [[ -n "$SCADA_COLUMNS" ]]; then
  read -r -a SCADA_ARRAY <<< "$SCADA_COLUMNS"
  COMPARE_ARGS+=(--scada-columns "${SCADA_ARRAY[@]}")
fi
if [[ "$NO_PLOTS" == "1" ]]; then
  COMPARE_ARGS+=(--no-plots)
fi

echo "[COMPARE] baseline=$BASELINE_DIR attack=$OUTPUT_DIR"
"$PYTHON_BIN" "$PROJECT_ROOT/scripts/compare_attack_results.py" \
  "${COMPARE_ARGS[@]}" \
  --baseline "$BASELINE_DIR" \
  --attack "$OUTPUT_DIR"
