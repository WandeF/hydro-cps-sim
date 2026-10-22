# Initial adapter validation — 4dd6b2b

This record covers the initial adapter implementation. The current UPSI framework, C-Town 100-round regression and independent third adapter are documented in [UPSI validation](upsi-validation.md).

## Environment

Validation uses the existing Ubuntu installation and sibling `OpenPLC_v3` and `ns-3-dev-git` checkouts. The water environment is `/home/lzh/anaconda3/envs/hydro-cps/bin/python` (Python 3.10.20). Optional power dependencies are installed in an isolated environment:

```bash
/home/lzh/anaconda3/envs/hydro-cps/bin/python -m venv --system-site-packages output/modular-physics-venv
output/modular-physics-venv/bin/python -m pip install pandapower==3.2.1
output/modular-physics-venv/bin/python -m pip check
```

The existing water environment and `requirements.txt` are unchanged. The power environment uses pandapower 3.2.1, NumPy 2.2.6 and pandas 2.3.3.

## Automated checks

```bash
output/modular-physics-venv/bin/python -m unittest discover -s tests -v
/home/lzh/anaconda3/envs/hydro-cps/bin/python -m unittest discover -s tests -v
bash -n scripts/run_all.sh scripts/run_power_experiments.sh
git diff --check
```

- Power environment: 34 tests passed.
- Original water environment: 34 tests discovered; 22 passed and 12 optional power tests skipped.
- Dependency consistency, shell syntax and whitespace checks passed.
- Both generated PLC programs compiled with the real OpenPLC `iec2c` compiler. The generated ns-3 topology also compiled as C++.

The water regression compares the adapter with the unchanged EPANET engine, including initial states, multiple physical steps, actuator changes and historical CSV values. Power tests solve AC power flow, open and reconnect the actual feeder switch, check unmapped signals and nonfinite results, and exercise the existing binary Modbus frame processor. Component checks are separate from the complete network experiments below.

Local test logs are `output/modular-physics-validation-tests.log` and `output/modular-physics-legacy-tests.log`.

## Complete network experiments

```bash
PYTHON_BIN="$PWD/output/modular-physics-venv/bin/python" bash scripts/run_power_experiments.sh
output/modular-physics-venv/bin/python scripts/check_power_results.py --output examples/power_feeder/output
```

The three experiments use the shared runner, native OpenPLC processes, Linux namespaces, ns-3, SCADA and the existing Modbus MITM proxy. Each experiment runs ten controller cycles. Baseline, transparent proxy and biased proxy results are stored separately under `examples/power_feeder/output/`.

All three complete runs passed on 2026-10-22. Each produced eleven solved physical snapshots (0–10) and ten actuator samples (0–9). All shared-runner stages completed with status zero; ns-3 reported readiness in every case.

| Check | Observed result |
| --- | --- |
| Baseline | Flexible load remains connected at 0.8 MW |
| Transparent proxy | Identical physical values and actuator sequence to baseline; four zero-bias FC16 writes recorded |
| Voltage MITM | Four actual FC16 requests changed by −0.15 pu, only at cycles 3–6, targeting PLC2's `PLC1_V_CONTROL` |
| First controller deviation | Cycle 3: `LOAD_SW` changes from true to false |
| First physical deviation | Snapshot 4, time 4 s: flexible load changes from 0.8 MW to 0 MW |
| Power flow | Every snapshot converged using pandapower |

At cycle 3 the proxy changed the forwarded voltage from 1.010219 to 0.860219 pu. The real PLC opened the breaker. The following power-flow solution changed grid import from 1.010027 to 0.200387 MW and feeder loading from 14.573719% to 2.891698%. Control-bus voltage rose to 1.018059 pu, while the disconnected load bus was unsupplied and reported zero voltage through its explicit mapping.

The proxy returned to unmodified forwarding after cycle 6. The breaker remained open because 1.018059 pu is below the 1.03 pu reconnection threshold. This matches the configured PLC hysteresis.

`validation.json` reports `status: passed`, `transparent_equals_baseline: true`, `modified_modbus_frames: 4`, `first_control_mismatch: 3` and `first_physical_mismatch: 4`. The checker also verifies that the transparent proxy actually processed all four configured writes.

Full-run console logs are in `examples/power_feeder/output/experiment-logs/`. The first ns-3 build took about 303 seconds; later cases reused the compiled topology. No solver, PLC or network results were substituted with mocks. Water compatibility was checked against the actual EPANET engine; the complete C-Town network experiment was not repeated in this validation.

## Outputs

Each case exports `reports/csv/physics_state.csv`, `physics.csv`, `actuator_state.csv`, SCADA observations and attack events. Physical snapshots retain simulator diagnostics under `runtime/`. Runner stages and status are recorded in `timing/run_all_timing.csv`; network readiness is recorded in `logs/ns3_network.log`.

The experiment checker writes `examples/power_feeder/output/validation.json`. Missing or unsuccessful runs produce `failed_or_incomplete` and a nonzero exit status. Generated runtime data and the local Python environment remain outside Git.

## Model scope

The power backend solves quasi-static AC operating points. It does not model electrical transients. A disconnected load bus reports zero voltage only through an explicit mapping checked against pandapower's unsupplied-bus topology. The current breaker logic holds its output between 0.95 and 1.03 pu; load shedding therefore persists after the attack window until reset or a changed operating point.
