#!/usr/bin/env bash
# Real CPS experiments, serialized through the existing shared runner.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c 'import pandapower, yaml, pymodbus'
[[ $EUID -ne 0 ]] || { echo 'Run as a normal user; run_all requests sudo.' >&2; exit 1; }
sudo -v
# Keep previous evidence, including incomplete runs, instead of overwriting it.
if [[ -d output/ieee33_bw ]]; then
  mkdir -p output/ieee33-archives
  mv output/ieee33_bw "output/ieee33-archives/$(date +%Y%m%d-%H%M%S)-$$"
fi
mkdir -p output/ieee33_bw/logs
"$PYTHON_BIN" scripts/ieee33_reference.py
"$PYTHON_BIN" -m pip freeze > output/ieee33_bw/native/requirements-frozen.txt
"$PYTHON_BIN" - <<'PY'
from pathlib import Path
import yaml
p=Path('examples/ieee33_bw/config_baseline.yaml').resolve()
c=yaml.safe_load(p.read_text())
for k in ('openplc_path','ns3_path'):c[k]=str((p.parent/c[k]).resolve())
c['physics']['model']['path']=str(p.parent/'model.yaml')
c['output_path']=str(Path('output/ieee33_bw/quick').resolve());c['iterations']=10
Path('output/ieee33_bw/quick.yaml').write_text(yaml.safe_dump(c,sort_keys=False))
PY
PYTHON_BIN="$PYTHON_BIN" bash scripts/run_all.sh --config output/ieee33_bw/quick.yaml --logic-wait 0.3 \
  > output/ieee33_bw/logs/quick.log 2>&1
for scenario in baseline transparent mitm; do
  echo "IEEE33: starting $scenario (100 cycles)"
  PYTHON_BIN="$PYTHON_BIN" bash scripts/run_all.sh \
    --config "examples/ieee33_bw/config_${scenario}.yaml" --logic-wait 0.3 \
    > "output/ieee33_bw/logs/${scenario}.log" 2>&1
done
"$PYTHON_BIN" scripts/check_ieee33_results.py --output output/ieee33_bw
