#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
CONFIG="$PROJECT_ROOT/examples/c_town/config.yaml"
ITERATIONS="${ITERATIONS:-}"
RUN_CHECK=0
SKIP_PREP=0
SKIP_COMPILE=0
SKIP_NS3=0
NS3_START_WAIT="${NS3_START_WAIT:-2}"
POLL_INTERVAL="${POLL_INTERVAL:-0.005}"
SYNC_BACKEND="${SYNC_BACKEND:-filesystem}"
HELICS_CORE_TYPE="${HELICS_CORE_TYPE:-ipc}"
HELICS_CORE_INIT="${HELICS_CORE_INIT:-}"
HELICS_BROKER_ADDRESS="${HELICS_BROKER_ADDRESS:-}"
HELICS_TIME_DELTA="${HELICS_TIME_DELTA:-0.001}"
HELICS_PREFIX="${HELICS_PREFIX:-hydro}"
HELICS_LOG_LEVEL="${HELICS_LOG_LEVEL:-1}"
HELICS_BROKER_NAME="${HELICS_BROKER_NAME:-hydro_cps_broker}"
HELICS_START_BROKER="${HELICS_START_BROKER:-1}"
HELICS_BROKER_PID=""
SCADA_MODBUS_WORKERS="${SCADA_MODBUS_WORKERS:-8}"
NO_BATCH_MODBUS=0
NO_PERSISTENT_SCADA_CONNECTIONS=0
CLEAN_RUNTIME="${CLEAN_RUNTIME:-1}"
NS3_PID=""
SUDO_KEEPALIVE_PID=""
STOP_PLC_ON_EXIT="${STOP_PLC_ON_EXIT:-1}"
STOP_ATTACKS_ON_EXIT="${STOP_ATTACKS_ON_EXIT:-1}"

usage() {
  cat <<EOF
Usage:
  bash scripts/run_all.sh [config.yaml] [options]

Options:
  --config PATH       Config file. Default: examples/c_town/config.yaml
  --iterations N      Override config iterations for this run
  --check             Run scripts/check.sh after closed-loop finishes
  --skip-prep         Skip ST/network/ns-3 source generation
  --skip-compile      Skip OpenPLC ST compilation
  --skip-ns3          Do not start ns-3; useful for local debugging only
  --poll-interval S   Filesystem marker polling interval. Default: 0.005
  --sync-backend NAME filesystem or helics. Default: filesystem
  --helics-core-type TYPE
                     HELICS core type. Default: ipc
  --helics-core-init STR
                     HELICS core init string, e.g. "--broker=hydro_cps"
  --helics-broker-address ADDR
                     Optional HELICS broker address, e.g. tcp://127.0.0.1:23405
  --helics-time-delta S
                     HELICS time delta while waiting for messages. Default: 0.001
  --scada-modbus-workers N
                     Concurrent PLC Modbus workers for SCADA. Default: 8
  --no-batch-modbus  Disable batched Modbus read/write optimization
  --no-persistent-scada-connections
                     Reconnect SCADA Modbus clients every cycle
  -h, --help          Show this help

Environment:
  PYTHON_BIN          Python executable visible from namespaces
  NS3_START_WAIT      Seconds to wait after starting ns-3. Default: 2
  POLL_INTERVAL       Filesystem marker polling interval. Default: 0.005
  SYNC_BACKEND        filesystem or helics. Default: filesystem
  HELICS_CORE_TYPE    HELICS core type. Default: ipc
  HELICS_CORE_INIT    HELICS core init string
  HELICS_BROKER_ADDRESS Optional HELICS broker address
  HELICS_TIME_DELTA   HELICS time delta while waiting for messages. Default: 0.001
  HELICS_BROKER_NAME  Broker name when run_all auto-starts helics_broker. Default: hydro_cps_broker
  HELICS_START_BROKER 1 to auto-start helics_broker for --sync-backend helics. Default: 1
  SCADA_MODBUS_WORKERS Concurrent PLC Modbus workers for SCADA. Default: 8
  CLEAN_RUNTIME       1 to delete output/runtime and output/check before run. Default: 1
  STOP_PLC_ON_EXIT    1 to stop PLC runtimes when run_all exits. Default: 1
  STOP_ATTACKS_ON_EXIT 1 to stop configured attack proxies/rules when run_all exits. Default: 1
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG="$2"; shift 2 ;;
    --iterations)
      ITERATIONS="$2"; shift 2 ;;
    --check)
      RUN_CHECK=1; shift ;;
    --skip-prep)
      SKIP_PREP=1; shift ;;
    --skip-compile)
      SKIP_COMPILE=1; shift ;;
    --skip-ns3)
      SKIP_NS3=1; shift ;;
    --poll-interval)
      POLL_INTERVAL="$2"; shift 2 ;;
    --sync-backend)
      SYNC_BACKEND="$2"; shift 2 ;;
    --helics-core-type)
      HELICS_CORE_TYPE="$2"; shift 2 ;;
    --helics-core-init)
      HELICS_CORE_INIT="$2"; shift 2 ;;
    --helics-broker-address)
      HELICS_BROKER_ADDRESS="$2"; shift 2 ;;
    --helics-time-delta)
      HELICS_TIME_DELTA="$2"; shift 2 ;;
    --helics-prefix)
      HELICS_PREFIX="$2"; shift 2 ;;
    --helics-log-level)
      HELICS_LOG_LEVEL="$2"; shift 2 ;;
    --scada-modbus-workers)
      SCADA_MODBUS_WORKERS="$2"; shift 2 ;;
    --no-batch-modbus)
      NO_BATCH_MODBUS=1; shift ;;
    --no-persistent-scada-connections)
      NO_PERSISTENT_SCADA_CONNECTIONS=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    --*)
      echo "[ERROR] Unknown option: $1" >&2; usage; exit 2 ;;
    *)
      CONFIG="$1"; shift ;;
  esac
done

cd "$PROJECT_ROOT"
CONFIG="$(realpath "$CONFIG")"

# Resolve paths from config.yaml once so every later step uses the same source of truth.
eval "$("$PYTHON_BIN" -m src.run.config_info --config "$CONFIG")"
ITERATIONS="${ITERATIONS:-$ITERATIONS_FROM_CONFIG}"
LOG_DIR="$OUTPUT_DIR/logs"
mkdir -p "$LOG_DIR"

stop_plc_runtimes() {
  # Previous one-click runs leave OpenPLC runtimes executing output/plcs/plcN.
  # Those processes keep the binary inode busy, so recompilation can fail with
  # ETXTBSY ("Text file busy") if we try to overwrite plcN. Stop them before
  # compilation and again when run_all exits.
  local pattern="$OUTPUT_DIR/plcs/"
  if [[ ! -d "$OUTPUT_DIR/plcs" ]]; then
    return 0
  fi
  if pgrep -af -- "$pattern" >/dev/null 2>&1; then
    echo "[CLEANUP] stopping OpenPLC runtime processes under $pattern"
    sudo -n pkill -TERM -f -- "$pattern" 2>/dev/null || sudo pkill -TERM -f -- "$pattern" 2>/dev/null || true
    sleep 1
    if pgrep -af -- "$pattern" >/dev/null 2>&1; then
      sudo -n pkill -KILL -f -- "$pattern" 2>/dev/null || sudo pkill -KILL -f -- "$pattern" 2>/dev/null || true
    fi
  fi
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ -n "${NS3_PID:-}" ]]; then
    echo "[CLEANUP] stopping ns-3 process group $NS3_PID"
    kill -TERM -- "-$NS3_PID" 2>/dev/null || sudo -n kill -TERM -- "-$NS3_PID" 2>/dev/null || sudo kill -TERM -- "-$NS3_PID" 2>/dev/null || true
    sleep 1
    kill -KILL -- "-$NS3_PID" 2>/dev/null || sudo -n kill -KILL -- "-$NS3_PID" 2>/dev/null || sudo kill -KILL -- "-$NS3_PID" 2>/dev/null || true
  fi
  if [[ "${STOP_PLC_ON_EXIT:-1}" == "1" ]]; then
    stop_plc_runtimes || true
  fi
  if [[ "${STOP_ATTACKS_ON_EXIT:-1}" == "1" ]]; then
    echo "[CLEANUP] stopping configured attack runtime"
    sudo -n "$PYTHON_BIN" -m src.attack.launch --config "$CONFIG" --action stop --runtime-dir "$OUTPUT_DIR/runtime" --python "$PYTHON_BIN" 2>/dev/null || \
      sudo "$PYTHON_BIN" -m src.attack.launch --config "$CONFIG" --action stop --runtime-dir "$OUTPUT_DIR/runtime" --python "$PYTHON_BIN" 2>/dev/null || true
  fi
  if [[ -n "${HELICS_BROKER_PID:-}" ]]; then
    echo "[CLEANUP] stopping HELICS broker $HELICS_BROKER_PID"
    kill -TERM "$HELICS_BROKER_PID" 2>/dev/null || sudo -n kill -TERM "$HELICS_BROKER_PID" 2>/dev/null || true
    sleep 1
    kill -KILL "$HELICS_BROKER_PID" 2>/dev/null || sudo -n kill -KILL "$HELICS_BROKER_PID" 2>/dev/null || true
  fi
  if [[ -n "${SUDO_KEEPALIVE_PID:-}" ]]; then
    kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

ensure_sudo() {
  if sudo -n true 2>/dev/null; then
    echo "[SUDO] cached credentials available"
  else
    echo "[SUDO] root permission is required for namespace, TAP, OpenPLC/ns-3, and closed-loop execution."
    sudo -v
  fi

  # Keep the sudo timestamp alive because preparation/compilation can take longer
  # than sudo's default timeout. Background commands must never prompt for a password.
  (
    while true; do
      sudo -n true 2>/dev/null || exit 0
      sleep 60
    done
  ) &
  SUDO_KEEPALIVE_PID=$!
}

step() {
  echo
  echo "================================================================================"
  echo "[RUN-ALL] $*"
  echo "================================================================================"
}

now_ns() {
  date +%s%N
}

fmt_duration_sec() {
  local start_ns="$1"
  local end_ns="$2"
  awk -v s="$start_ns" -v e="$end_ns" 'BEGIN { printf "%.6f", (e - s) / 1000000000 }'
}

timing_init() {
  TIMING_DIR="$OUTPUT_DIR/timing"
  mkdir -p "$TIMING_DIR"
  RUN_ALL_TIMING_CSV="$TIMING_DIR/run_all_timing.csv"
  printf 'stage,start_epoch_ns,end_epoch_ns,duration_sec,status\n' > "$RUN_ALL_TIMING_CSV"
  RUN_ALL_START_NS="$(now_ns)"
}

timing_record() {
  local stage="$1"
  local start_ns="$2"
  local end_ns="$3"
  local status="$4"
  local duration
  duration="$(fmt_duration_sec "$start_ns" "$end_ns")"
  printf '%s,%s,%s,%s,%s\n' "${stage//,/;}" "$start_ns" "$end_ns" "$duration" "$status" >> "$RUN_ALL_TIMING_CSV"
}

time_stage() {
  local stage_name="$1"
  shift
  step "$stage_name"
  local start_ns end_ns rc
  start_ns="$(now_ns)"
  set +e
  "$@"
  rc=$?
  set -e
  end_ns="$(now_ns)"
  timing_record "$stage_name" "$start_ns" "$end_ns" "$rc"
  if [[ "$rc" -ne 0 ]]; then
    echo "[ERROR] stage failed: $stage_name (rc=$rc)" >&2
    exit "$rc"
  fi
}

start_helics_broker() {
  if [[ "$SYNC_BACKEND" != "helics" || "$HELICS_START_BROKER" != "1" ]]; then
    return 0
  fi
  if ! command -v helics_broker >/dev/null 2>&1; then
    echo "[ERROR] --sync-backend helics requires helics_broker in PATH, or set HELICS_START_BROKER=0 and start a broker manually." >&2
    exit 1
  fi

  local fed_count
  fed_count="$($PYTHON_BIN - <<PYCOUNT
from pathlib import Path
from src.core.config import load_runtime_config
rt = load_runtime_config(Path("$CONFIG"))
print(len(rt.plcs) + 2)  # coordinator + scada + PLC adapters
PYCOUNT
)"
  local log_file="$LOG_DIR/helics_broker.log"
  : > "$log_file"

  if [[ -z "$HELICS_CORE_INIT" && -z "$HELICS_BROKER_ADDRESS" ]]; then
    HELICS_CORE_INIT="--broker=$HELICS_BROKER_NAME"
  fi

  echo "[HELICS] broker cmd: helics_broker -t $HELICS_CORE_TYPE -f $fed_count -n $HELICS_BROKER_NAME"
  helics_broker -t "$HELICS_CORE_TYPE" -f "$fed_count" -n "$HELICS_BROKER_NAME" --loglevel="$HELICS_LOG_LEVEL" >"$log_file" 2>&1 &
  HELICS_BROKER_PID=$!
  sleep 1
  if ! kill -0 "$HELICS_BROKER_PID" 2>/dev/null; then
    echo "[ERROR] HELICS broker exited early. Tail of log:" >&2
    tail -n 80 "$log_file" >&2 || true
    exit 1
  fi
  echo "[HELICS] broker started pid=$HELICS_BROKER_PID federates=$fed_count log=$log_file"
}

stop_stale_attacks() {
  sudo "$PYTHON_BIN" -m src.attack.launch \
    --config "$CONFIG" \
    --action stop \
    --runtime-dir "$OUTPUT_DIR/runtime" \
    --python "$PYTHON_BIN"
}

run_ns3() {
  if [[ "$SKIP_NS3" == "1" ]]; then
    echo "[NS3] skipped by --skip-ns3"
    return 0
  fi

  local src_cc="$OUTPUT_DIR/ns3_network.cc"
  local scratch_cc="$NS3_PATH/scratch/ns3_network.cc"
  local log_file="$LOG_DIR/ns3_network.log"

  if [[ ! -f "$src_cc" ]]; then
    echo "[ERROR] ns-3 source not found: $src_cc" >&2
    exit 1
  fi
  if [[ ! -d "$NS3_PATH" ]]; then
    echo "[ERROR] ns3_path not found: $NS3_PATH" >&2
    exit 1
  fi

  # The ns-3 front-end refuses to run as root. Use sudo only to repair
  # permissions that may have been left behind by older runs, then execute
  # ./ns3 as the normal user. TAP devices are created as the current user in
  # network.sh, so TapBridge can open them without running the ns-3 launcher as root.
  mkdir -p "$NS3_PATH/scratch" 2>/dev/null || sudo mkdir -p "$NS3_PATH/scratch"
  if [[ ! -w "$NS3_PATH/scratch" ]]; then
    sudo chown "$(id -u):$(id -g)" "$NS3_PATH/scratch"
  fi
  if [[ -e "$scratch_cc" && ! -w "$scratch_cc" ]]; then
    sudo rm -f "$scratch_cc"
  fi
  cp "$src_cc" "$scratch_cc"

  local ns3_cmd=""
  if [[ -x "$NS3_PATH/ns3" ]]; then
    ns3_cmd="./ns3 run ns3_network"
  elif [[ -x "$NS3_PATH/waf" ]]; then
    ns3_cmd="./waf --run scratch/ns3_network"
  else
    echo "[ERROR] Neither ./ns3 nor ./waf is executable under ns3_path: $NS3_PATH" >&2
    exit 1
  fi

  echo "[NS3] source : $src_cc"
  echo "[NS3] scratch: $scratch_cc"
  echo "[NS3] log    : $log_file"
  echo "[NS3] cmd    : cd $NS3_PATH && setsid $ns3_cmd"

  : > "$log_file"
  local pid_file="$LOG_DIR/ns3_network.pid"
  rm -f "$pid_file"

  setsid bash -c 'echo "$$" > "$3"; cd "$1" && exec bash -lc "$2"' \
    _ "$NS3_PATH" "$ns3_cmd" "$pid_file" >"$log_file" 2>&1 &
  local launcher_pid=$!

  for _ in $(seq 1 50); do
    [[ -s "$pid_file" ]] && break
    if ! kill -0 "$launcher_pid" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done

  if [[ -s "$pid_file" ]]; then
    NS3_PID="$(cat "$pid_file")"
  else
    NS3_PID="$launcher_pid"
  fi

  sleep "$NS3_START_WAIT"

  if ! kill -0 "$NS3_PID" 2>/dev/null; then
    echo "[ERROR] ns-3 exited early. Tail of log:" >&2
    tail -n 80 "$log_file" >&2 || true
    exit 1
  fi
  echo "[NS3] started pid=$NS3_PID"
}

step "Config"
echo "[CONFIG]      $CONFIG_ABS"
echo "[OUTPUT]      $OUTPUT_DIR"
echo "[OPENPLC]     $OPENPLC_PATH"
echo "[NS3]         $NS3_PATH"
echo "[ITERATIONS]  $ITERATIONS"
echo "[PYTHON]      $PYTHON_BIN"
echo "[SYNC]        $SYNC_BACKEND"
if [[ "$SYNC_BACKEND" == "helics" ]]; then
  echo "[HELICS]      core_type=$HELICS_CORE_TYPE core_init=$HELICS_CORE_INIT broker=$HELICS_BROKER_ADDRESS"
fi

timing_init

time_stage "Ensure sudo credentials" ensure_sudo

if [[ "$CLEAN_RUNTIME" == "1" ]]; then
  time_stage "Clean previous runtime/check outputs" bash -c 'sudo rm -rf "$1/runtime" "$1/check"; mkdir -p "$2"' _ "$OUTPUT_DIR" "$LOG_DIR"
fi

time_stage "Stop stale OpenPLC runtimes from previous runs" stop_plc_runtimes

if [[ "$SKIP_PREP" != "1" ]]; then
  time_stage "Generate and validate OpenPLC ST files" "$PYTHON_BIN" -m src.control.st_generation --config "$CONFIG"
  time_stage "Generate network.sh" "$PYTHON_BIN" -m src.network.network_sh_generation --config "$CONFIG"
  time_stage "Generate ns3_network.cc" "$PYTHON_BIN" -m src.network.ns3_generation "$CONFIG"
else
  echo "[PREP] skipped by --skip-prep"
  timing_record "Generate preparation artifacts" "$(now_ns)" "$(now_ns)" "skipped"
fi

if [[ "$SKIP_COMPILE" != "1" ]]; then
  time_stage "Compile ST files into OpenPLC executable runtimes" "$PYTHON_BIN" -m src.control.plc_precompile --config "$CONFIG"
else
  echo "[COMPILE] skipped by --skip-compile"
  timing_record "Compile ST files into OpenPLC executable runtimes" "$(now_ns)" "$(now_ns)" "skipped"
fi

time_stage "Create Linux namespaces, bridges, veth pairs, and TAP devices" bash "$OUTPUT_DIR/network.sh"
time_stage "Launch OpenPLC runtimes and start Modbus/TCP" "$PYTHON_BIN" -m src.control.plc_run --config "$CONFIG"
time_stage "Start ns-3 network" run_ns3
time_stage "Start HELICS broker if requested" start_helics_broker
time_stage "Stop stale configured attack runtime/rules" stop_stale_attacks
SCADA_EXTRA_ARGS=(--scada-modbus-workers "$SCADA_MODBUS_WORKERS")
if [[ "$NO_BATCH_MODBUS" == "1" ]]; then
  SCADA_EXTRA_ARGS+=(--no-batch-modbus)
fi
if [[ "$NO_PERSISTENT_SCADA_CONNECTIONS" == "1" ]]; then
  SCADA_EXTRA_ARGS+=(--no-persistent-scada-connections)
fi
SYNC_EXTRA_ARGS=(
  --sync-backend "$SYNC_BACKEND"
  --helics-core-type "$HELICS_CORE_TYPE"
  --helics-core-init "$HELICS_CORE_INIT"
  --helics-broker-address "$HELICS_BROKER_ADDRESS"
  --helics-time-delta "$HELICS_TIME_DELTA"
  --helics-prefix "$HELICS_PREFIX"
  --helics-log-level "$HELICS_LOG_LEVEL"
)

time_stage "Run persistent closed-loop control" sudo "$PYTHON_BIN" -m src.runtime.persistent_closed_loop \
  --config "$CONFIG" \
  --iterations "$ITERATIONS" \
  --python "$PYTHON_BIN" \
  --physics-mode dhalsim_epynet \
  --init-style dhalsim \
  --poll-interval "$POLL_INTERVAL" \
  --logic-wait 0.3 \
  "${SYNC_EXTRA_ARGS[@]}" \
  "${SCADA_EXTRA_ARGS[@]}"

if [[ "$RUN_CHECK" == "1" ]]; then
  time_stage "Run offline check" bash "$PROJECT_ROOT/scripts/check.sh" "$CONFIG"
fi

timing_record "run_all total" "$RUN_ALL_START_NS" "$(now_ns)" "0"

step "Finished"
echo "[RUNTIME] $OUTPUT_DIR/runtime"
echo "[CSV]     $OUTPUT_DIR/runtime/csv"
echo "[JSON]    $OUTPUT_DIR/runtime/json"
echo "[TIMING]  $OUTPUT_DIR/timing"
if [[ "$RUN_CHECK" == "1" ]]; then
  echo "[CHECK]   $OUTPUT_DIR/check"
else
  echo "[CHECK]   run: bash scripts/check.sh $CONFIG"
fi
