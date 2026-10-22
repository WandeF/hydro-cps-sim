#!/usr/bin/env bash
# Execute the real, shared OpenPLC/ns-3 closed loop for all three scenarios.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c 'import pandapower, yaml, pymodbus'
[[ $EUID -ne 0 ]] || { echo 'Run as a normal user; run_all requests sudo.' >&2; exit 1; }
sudo -v
mkdir -p examples/power_feeder/output/experiment-logs
for scenario in config config_mitm_transparent config_mitm; do
  PYTHON_BIN="$PYTHON_BIN" bash scripts/run_all.sh \
    --config "examples/power_feeder/${scenario}.yaml" --logic-wait 0.3 \
    2>&1 | tee "examples/power_feeder/output/experiment-logs/${scenario}.log"
done
"$PYTHON_BIN" scripts/check_power_results.py --output examples/power_feeder/output
