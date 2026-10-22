#!/usr/bin/env bash
# Compare a real 100-round water reference, then exercise every UPSI backend.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
UPSI_REFERENCE_REF="${UPSI_REFERENCE_REF:-4dd6b2b}"
UPSI_REFERENCE_COMMIT="$(git rev-parse --verify "${UPSI_REFERENCE_REF}^{commit}")"
mkdir -p output
UPSI_REFERENCE_DIR="$(mktemp -d "$PROJECT_ROOT/output/upsi-reference.XXXXXX")"
git archive "$UPSI_REFERENCE_COMMIT" | tar -x -C "$UPSI_REFERENCE_DIR"
mkdir -p output/upsi-regression
printf '%s\n' "$UPSI_REFERENCE_COMMIT" > output/upsi-regression/reference_commit.txt
"$PYTHON_BIN" - <<'PY'
from pathlib import Path
import yaml
root=Path.cwd()
config=yaml.safe_load((root/'examples/c_town/config.yaml').read_text())
for case in ('reference','current'):
    config['output_path']=str(root/'output/upsi-regression'/case)
    (root/'output/upsi-regression'/f'ctown-{case}.yaml').write_text(yaml.safe_dump(config,sort_keys=False))
PY
sudo -v
PYTHON_BIN="$PYTHON_BIN" bash "$UPSI_REFERENCE_DIR/scripts/run_all.sh" \
  --config "$PROJECT_ROOT/output/upsi-regression/ctown-reference.yaml" --iterations 100 --logic-wait 0.3 \
  > output/upsi-regression/reference-console.log 2>&1
PYTHON_BIN="$PYTHON_BIN" bash scripts/run_all.sh \
  --config output/upsi-regression/ctown-current.yaml --iterations 100 --logic-wait 0.3 \
  > output/upsi-regression/current-console.log 2>&1
PYTHON_BIN="$PYTHON_BIN" bash scripts/run_power_experiments.sh > output/upsi-regression/power-console.log 2>&1
PYTHON_BIN="$PYTHON_BIN" bash scripts/run_all.sh --config examples/power_feeder/config_v2.yaml --logic-wait 0.3 \
  > output/upsi-regression/power-v2-console.log 2>&1
PYTHON_BIN="$PYTHON_BIN" bash scripts/run_all.sh --config examples/thermal_box/config.yaml --logic-wait 0.3 \
  > output/upsi-regression/thermal-console.log 2>&1
"$PYTHON_BIN" scripts/check_upsi_results.py --output output/upsi-regression
