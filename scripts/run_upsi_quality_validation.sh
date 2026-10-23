#!/usr/bin/env bash
# Real policy experiments reuse the existing voltage MITM; model/attack unchanged.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
mkdir -p output/upsi-hardening
"$PYTHON_BIN" - <<'PY'
from pathlib import Path
import yaml
root=Path.cwd();source=root/'examples/power_feeder/config_mitm.yaml'
for name,action in [('hold','hold_last'),('fail','fail_fast')]:
    raw=yaml.safe_load(source.read_text())
    for key in ('openplc_path','ns3_path'):
        raw[key]=str((source.parent/raw[key]).resolve())
    raw['physics']['model_file']=str(source.parent/'model.yaml')
    raw['quality_policy']={'default':'pass_through','overrides':{'unsupplied':action}}
    raw['output_path']=str(root/'output/upsi-hardening'/f'power-{name}')
    (root/'output/upsi-hardening'/f'power-{name}.yaml').write_text(yaml.safe_dump(raw,sort_keys=False))
PY
sudo -v
PYTHON_BIN="$PYTHON_BIN" bash scripts/run_all.sh --config output/upsi-hardening/power-hold.yaml --logic-wait 0.3 \
  > output/upsi-hardening/power-hold-console.log 2>&1
if PYTHON_BIN="$PYTHON_BIN" bash scripts/run_all.sh --config output/upsi-hardening/power-fail.yaml --logic-wait 0.3 \
  > output/upsi-hardening/power-fail-console.log 2>&1; then
  echo 'Expected fail_fast run to stop, but it succeeded' >&2
  exit 1
fi
"$PYTHON_BIN" scripts/check_upsi_quality_results.py
