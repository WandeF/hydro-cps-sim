#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CONFIG="$PROJECT_ROOT/examples/c_town/config.yaml"
ITERATIONS=""
LOGIC_WAIT="0.3"
POLL_INTERVAL="0.005"
SCADA_MODBUS_WORKERS="8"

usage() {
  cat <<'EOF'
Usage: bash scripts/run_all.sh [config.yaml] [options]

  --config PATH            Config file (default: examples/c_town/config.yaml)
  --iterations N           Override the configured simulation length
  --logic-wait SECONDS     Wait after SCADA downlink before reading PLC outputs (default: 0.3)
  --poll-interval SECONDS  File-marker polling interval (default: 0.005)
  --scada-modbus-workers N Concurrent SCADA workers (default: 8)
  -h, --help               Show this help

Set PYTHON_BIN to the Python executable containing the simulation dependencies.
EOF
}

fail() { echo "[ERROR] $*" >&2; exit 1; }
require_value() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail "Missing value for $1"; }
config_seen=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      require_value "$@"; CONFIG="$2"; config_seen=1; shift 2 ;;
    --iterations)
      require_value "$@"; ITERATIONS="$2"; shift 2 ;;
    --logic-wait)
      require_value "$@"; LOGIC_WAIT="$2"; shift 2 ;;
    --poll-interval)
      require_value "$@"; POLL_INTERVAL="$2"; shift 2 ;;
    --scada-modbus-workers)
      require_value "$@"; SCADA_MODBUS_WORKERS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) fail "Unknown option: $1" ;;
    *)
      [[ "$config_seen" == 0 ]] || fail "Only one config file may be specified"
      CONFIG="$1"; config_seen=1; shift ;;
  esac
done

cd "$PROJECT_ROOT"
[[ -f "$CONFIG" ]] || fail "Config file not found: $CONFIG"
PYTHON_BIN="$(command -v "$PYTHON_BIN")" || fail "Python executable not found"
CONFIG="$(realpath "$CONFIG")"
# Capture first: eval alone hides a failed configuration reader's exit code.
config_values="$("$PYTHON_BIN" -m src.run.config_info --config "$CONFIG")"
eval "$config_values"
ITERATIONS="${ITERATIONS:-$ITERATIONS_FROM_CONFIG}"
"$PYTHON_BIN" - "$ITERATIONS" "$LOGIC_WAIT" "$POLL_INTERVAL" "$SCADA_MODBUS_WORKERS" "$OUTPUT_DIR" "$PROJECT_ROOT" "$CONFIG" <<'PY'
import math
import sys
from pathlib import Path
try:
    for name, value in (("iterations", sys.argv[1]), ("scada-modbus-workers", sys.argv[4])):
        if int(value) < 1:
            raise ValueError(f"{name} must be a positive integer")
    for name, value, allow_zero in (("logic-wait", sys.argv[2], True), ("poll-interval", sys.argv[3], False)):
        v = float(value)
        if not math.isfinite(v) or v < 0 or (not allow_zero and v == 0):
            raise ValueError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}")
    output = Path(sys.argv[5]).resolve()
    project = Path(sys.argv[6]).resolve()
    config = Path(sys.argv[7]).resolve()
    if output == Path('/') or output == project or output in config.parents:
        raise ValueError("output_path must be a dedicated output directory")
    import yaml
    import pymodbus
    from epynet.epanet2 import EPANET2
except (ValueError, ImportError) as exc:
    raise SystemExit(f"[ERROR] {exc}")
PY
[[ $EUID -ne 0 ]] || fail "Run as a normal user; sudo is requested for privileged steps"
[[ -x "$NS3_PATH/ns3" ]] || fail "ns-3 launcher not found: $NS3_PATH/ns3"
[[ -f "$OPENPLC_PATH/webserver/scripts/compile_program.sh" ]] || fail "OpenPLC compiler not found"
for cmd in sudo ip flock setsid; do command -v "$cmd" >/dev/null || fail "Required command not found: $cmd"; done
# Both dependencies contain shared build products. Hold locks for the whole run.
exec 8>"$NS3_PATH/.hydro-cps-run.lock"
flock -n 8 || fail "Another Hydro-CPS run is using this ns-3 installation"
exec 9>"$OPENPLC_PATH/.hydro-cps-run.lock"
flock -n 9 || fail "Another Hydro-CPS run is using this OpenPLC installation"

LOG_DIR="$OUTPUT_DIR/logs"
mkdir -p "$LOG_DIR" "$OUTPUT_DIR/timing"
TIMING_CSV="$OUTPUT_DIR/timing/run_all_timing.csv"
printf 'stage,start_epoch_ns,end_epoch_ns,duration_sec,status\n' > "$TIMING_CSV"
RUN_START="$(date +%s%N)"
ACTIVE_STAGE=""
SUDO_READY=0
NETWORK_MANAGED=0
RUNTIME_STARTED=0
EXPORTED=0
NS3_PID=""
NS3_LAUNCHER_PID=""
SUDO_KEEPALIVE_PID=""

timing_record() {
  local stage="$1" start="$2" status="$3" end duration
  end="$(date +%s%N)"
  duration="$(awk -v s="$start" -v e="$end" 'BEGIN {printf "%.6f", (e-s)/1000000000}')"
  printf '%s,%s,%s,%s,%s\n' "${stage//,/;}" "$start" "$end" "$duration" "$status" >> "$TIMING_CSV"
}

stage() {
  ACTIVE_STAGE="$1"; shift
  STAGE_START="$(date +%s%N)"
  echo "[RUN-ALL] $ACTIVE_STAGE"
  # Keep errexit active inside shell functions, including multi-command stages.
  "$@"
  timing_record "$ACTIVE_STAGE" "$STAGE_START" 0
  ACTIVE_STAGE=""
}

ensure_sudo() {
  sudo -v || return $?
  SUDO_READY=1
  (
    sleeper=""
    trap 'if [[ -n "$sleeper" ]]; then kill "$sleeper" 2>/dev/null || true; fi; exit 0' TERM INT
    while sudo -n true; do
      sleep 60 & sleeper=$!
      wait "$sleeper" || break
    done
  ) >/dev/null 2>&1 &
  SUDO_KEEPALIVE_PID=$!
}

stop_attacks() {
  # A fresh installation has no previous runtime or attack processes to stop.
  [[ -d "$OUTPUT_DIR/runtime" ]] || return 0
  sudo -n "$PYTHON_BIN" -m src.attack.launch --config "$CONFIG" --action stop \
    --runtime-dir "$OUTPUT_DIR/runtime" --python "$PYTHON_BIN"
}

cleanup_network() {
  local ns dev pids
  for ns in "${NETWORK_NAMESPACES[@]}"; do
    pids="$(sudo -n ip netns pids "$ns" 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
      echo "$pids" | xargs -r sudo -n kill -TERM 2>/dev/null || true
      sleep 0.2
      sudo -n ip netns pids "$ns" 2>/dev/null | xargs -r sudo -n kill -KILL 2>/dev/null || true
    fi
    sudo -n ip netns del "$ns" 2>/dev/null || true
  done
  # Only remove interfaces belonging to this configuration.
  for dev in "${NETWORK_LINKS[@]}"; do
    sudo -n ip link del "$dev" 2>/dev/null || true
  done
}

stop_ns3() {
  if [[ -n "$NS3_PID" ]]; then
    kill -TERM -- "-$NS3_PID" 2>/dev/null || true
    for _ in {1..20}; do
      kill -0 -- "-$NS3_PID" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL -- "-$NS3_PID" 2>/dev/null || true
  fi
  if [[ -n "$NS3_LAUNCHER_PID" ]]; then wait "$NS3_LAUNCHER_PID" 2>/dev/null || true; fi
}

export_results() {
  [[ "$EXPORTED" == 0 && "$RUNTIME_STARTED" == 1 ]] || return 0
  sudo -n "$PYTHON_BIN" -m src.io.export_results --config "$CONFIG" \
    --runtime-dir "$OUTPUT_DIR/runtime" --reports-dir "$OUTPUT_DIR/reports" || return $?
  sudo -n chown -R "$(id -u):$(id -g)" "$OUTPUT_DIR/reports" || return $?
  EXPORTED=1
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  set +e
  if [[ -n "$ACTIVE_STAGE" ]]; then timing_record "$ACTIVE_STAGE" "$STAGE_START" "$rc"; fi
  if [[ "$SUDO_READY" == 1 ]]; then
    stop_ns3
    if [[ "$NETWORK_MANAGED" == 1 ]]; then
      stop_attacks || true
      cleanup_network
    fi
    export_results || { echo "[ERROR] Could not export run results" >&2; [[ "$rc" != 0 ]] || rc=1; }
  fi
  if [[ -n "$SUDO_KEEPALIVE_PID" ]]; then
    kill "$SUDO_KEEPALIVE_PID" 2>/dev/null
    wait "$SUDO_KEEPALIVE_PID" 2>/dev/null
  fi
  timing_record "run_all total" "$RUN_START" "$rc"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

prepare_workspace() {
  NETWORK_MANAGED=1
  stop_attacks || true
  cleanup_network
  # Remove stale generated PLCs when a configuration reduces or renumbers nodes.
  sudo -n rm -rf -- "$OUTPUT_DIR/runtime" "$OUTPUT_DIR/reports" "$OUTPUT_DIR/st" "$OUTPUT_DIR/plcs"
}

build_ns3() {
  local scratch="$NS3_PATH/scratch/ns3_network.cc"
  mkdir -p "$NS3_PATH/scratch"
  if [[ -e "$scratch" && ! -w "$scratch" ]]; then sudo -n chown "$(id -u):$(id -g)" "$scratch"; fi
  # Avoid recompilation when the generated network source is unchanged.
  if ! cmp -s "$OUTPUT_DIR/ns3_network.cc" "$scratch"; then cp "$OUTPUT_DIR/ns3_network.cc" "$scratch"; fi
  echo "[NS3] build log: $LOG_DIR/ns3_build.log"
  if ! (cd "$NS3_PATH" && ./ns3 build ns3_network) >"$LOG_DIR/ns3_build.log" 2>&1; then
    tail -n 50 "$LOG_DIR/ns3_build.log" >&2
    return 1
  fi
}

start_ns3() {
  local log="$LOG_DIR/ns3_network.log" pid_file="$LOG_DIR/ns3_network.pid" deadline
  rm -f "$pid_file"
  setsid bash -c 'echo "$$" > "$2"; cd "$1"; exec ./ns3 run --no-build ns3_network' \
    _ "$NS3_PATH" "$pid_file" >"$log" 2>&1 &
  NS3_LAUNCHER_PID=$!
  deadline=$((SECONDS + 30))
  while (( SECONDS < deadline )); do
    if [[ -s "$pid_file" ]]; then NS3_PID="$(cat "$pid_file")"; fi
    if ! kill -0 "${NS3_PID:-$NS3_LAUNCHER_PID}" 2>/dev/null; then
      tail -n 50 "$log" >&2; return 1
    fi
    if grep -q 'ns3 network started\.' "$log"; then
      echo "[NS3] network ready (pid=$NS3_PID)"
      return 0
    fi
    sleep 0.1
  done
  echo "[ERROR] ns-3 did not become ready within 30 seconds" >&2
  tail -n 50 "$log" >&2
  return 1
}

run_closed_loop() {
  RUNTIME_STARTED=1
  sudo -n "$PYTHON_BIN" -m src.runtime.persistent_closed_loop \
    --config "$CONFIG" --iterations "$ITERATIONS" --python "$PYTHON_BIN" \
    --physics-mode dhalsim_epynet --init-style dhalsim \
    --poll-interval "$POLL_INTERVAL" --logic-wait "$LOGIC_WAIT" \
    --scada-modbus-workers "$SCADA_MODBUS_WORKERS"
}

echo "[CONFIG] $CONFIG"
echo "[OUTPUT] $OUTPUT_DIR"
echo "[RUN] iterations=$ITERATIONS logic_wait=${LOGIC_WAIT}s python=$PYTHON_BIN"
stage "Authenticate sudo" ensure_sudo
stage "Clean previous runtime and configured network" prepare_workspace
stage "Generate PLC programs" "$PYTHON_BIN" -m src.control.st_generation --config "$CONFIG"
stage "Generate namespace setup" "$PYTHON_BIN" -m src.network.network_sh_generation --config "$CONFIG"
stage "Generate ns-3 topology" "$PYTHON_BIN" -m src.network.ns3_generation "$CONFIG"
stage "Compile PLC programs" "$PYTHON_BIN" -m src.control.plc_precompile --config "$CONFIG"
stage "Compile ns-3 topology" build_ns3
stage "Create network namespaces" bash "$OUTPUT_DIR/network.sh"
stage "Launch OpenPLC" "$PYTHON_BIN" -m src.control.plc_run --config "$CONFIG"
stage "Start ns-3 network" start_ns3
stage "Run closed loop" run_closed_loop
stage "Export results" export_results
echo "[DONE] Results: $OUTPUT_DIR/reports/csv"
