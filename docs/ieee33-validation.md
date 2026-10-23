# IEEE 33-bus integration validation

The public Baran–Wu network now runs through the existing pandapower UPSI backend, OpenPLC, SCADA, Modbus and ns-3 loop. All three 100-cycle experiments passed: transparent forwarding preserves the baseline, and the voltage MITM propagates through a real PLC coil into the next physical snapshot.

## Model and environment

Validation date: 2026-10-23. Development branch: `feat/ieee33-paper-benchmark`, based on UPSI hardening commit `784f019b90aaad4fa560dac56e8ea13eae0ce0f8`. No remote push.

| Component | Version |
| --- | --- |
| Python | 3.10.20 |
| pandapower | 3.2.1 |
| NumPy | 2.2.6 |
| SciPy | 1.15.3 |
| pandas | 2.3.3 |
| matplotlib (optional plotting) | 3.10.7 |

The existing optional power environment is `output/modular-physics-venv`. Its dependency check passed. The complete installed environment is recorded at `output/ieee33_bw/native/requirements-frozen.txt`; plotting dependencies were added only to that environment. The original water environment remains unchanged.

The model is loaded by `pandapower.networks.case33bw()`. [MATPOWER's source](https://matpower.org/docs/ref/matpower6.0/case33bw.html) identifies Baran and Wu's 1989 paper and supplies the original bus, load and branch parameters. [pandapower's documentation](https://pandapower.readthedocs.io/en/v3.2.1/networks/power_system_test_cases.html#case-33-bw) describes its converted public case. See the example's [references and modeling boundary](../examples/ieee33_bw/references.md).

The imported network contains 33 buses at 12.66 kV, 37 lines, 32 loads, one external grid and no switch-table entries. Thirty-two lines are in service. The five open ties, using paper bus numbers, are 21–8, 9–15, 12–22, 18–33 and 25–29. Their state comes from `line.in_service`. Demand is 3.715 MW / 2.300 MVAr and the source voltage is 1.0 pu. This installed conversion uses `sn_mva=10` and `f_hz=60`; these are preserved and recorded with the source tables. The linked MATPOWER 6.0 page uses a 100 MVA base, so the two source versions are not asserted to have identical serialized parameters. The public pandapower case is the numerical reference used here.

Source verification:

```text
input_tables_sha256 = 3663958ff6aa0383bb9f0b48e6888cd36aa236070fc1d02b539fe826c9892758
source_file_sha256  = ca1fa6d9341f8f69cfee9c20c2fe0c31017b9c565bc435831ecdd4ab6ce2bd34
```

`benchmark_reference.json` contains the full input tables, not just these hashes. `bus_id_map.json` records source names, paper bus numbers and DataFrame indices. Source names are integer labels `0`–`32`, recorded as canonical text and checked as an explicit importer convention; mapping does not assume `index = paper_bus - 1`. The reindexed-model test verifies this distinction. More than one load on a controlled bus fails as ambiguous.

## G0: original power flow

Newton–Raphson, `numba=False`, `max_iteration=20`, `tolerance_mva=1e-10`:

| Metric | Computed result |
| --- | ---: |
| Converged | true |
| Minimum voltage | 0.9130904793610509 pu, Bus 18 |
| Total active line loss | 0.20267712645598454 MW |
| Grid active power | 3.917677126457022 MW |
| Served load | 3.715 MW |
| Unserved load | 0 MW |

The commonly reported comparison values are 0.9131 pu and 202.66 kW. The differences are approximately −0.00000952 pu and +0.01713 kW, within the declared absolute windows of 0.00005 pu and 0.05 kW. Source data and computed values are retained without adjusting parameters or results to force a match. This is a comparison against rounded published numbers, not a claim to reproduce another paper's solver bit for bit.

The native reference exports all 33 bus voltages and all 37 line power/current/loss results. The model's placeholder thermal ratings are not treated as meaningful line loading limits.

## G1: UPSI and controlled load

The loader adds a whitelisted builtin source inside the power adapter's model layer. Existing declarative four-bus models still use their original path. Common scheduling, PLC code generation, SCADA forwarding and attack processing contain no IEEE33 branches.

The existing Bus 24 load is controlled through `load.in_service`; its original 0.420 MW / 0.200 MVAr parameters remain unchanged. Real backend tests confirm opening and reclosing change its actual power between 0 and 0.420 MW while the other original loads remain unchanged. The bus remains energized after load shedding, so its voltage retains `good` quality. Topology-isolated bus voltages either fail the default strict boundary or use the explicitly configured `zero_if_unsupplied` behavior with `unsupplied` quality; they never become unmarked good zeros.

Aggregate MW signals are read-only `metrics.total.*` ports. Supply is the sum of `res_load.p_mw`; unserved power is requested `p_mw × scaling` minus served power, including load shedding. Convergence and complete bus voltages stay in diagnostics. All aggregate inputs are checked for finite values before summation so pandas cannot silently skip NaN. Solver failures invalidate the session rather than publishing a successful snapshot.

## G2–G3: real closed-loop evidence

Each configuration uses three compiled native OpenPLC binaries, Linux namespaces/TAPs, the generated ns-3 real-time topology, SCADA Modbus polling/downlink, and persistent real pandapower power flow. Every successful run has stage timing records and an ns-3 readiness log.

| Run | Control cycles | Physical snapshots | Result |
| --- | ---: | ---: | --- |
| Quick baseline | 10 | 11 | passed |
| Baseline | 100 | 101 | passed, load enabled throughout |
| Transparent proxy | 100 | 101 | passed, physics and actuator sequence equal baseline |
| MITM | 100 | 101 | passed, 21 modified FC16 requests in cycles 20–40 |

PLC1 owns Bus 2 voltage, PLC2 monitors buses 18 and 33, and PLC3 controls Bus 24. SCADA forwards `PLC1_V_BUS02` to PLC3. The experimental rule trips below 0.95 pu, restores above 1.03 pu, and retains the coil in between. Initial coils and dependency registers are preloaded by the existing shared bootstrap path.

Baseline Bus 2 voltage is 0.9970322597292005 pu. The −0.10 pu offset crosses the trip threshold; the rule does not act on the normal voltage. The three cases use identical communication topology, including the attacker endpoint. Transparent and MITM use the same route, listen port and request rule; only the increment differs.

Observed chain:

| Evidence | Cycle | Value |
| --- | ---: | ---: |
| Physical source voltage | 20 | 0.9970322597292005 pu |
| PLC1 voltage polled by SCADA / confirmed client write | 20 | 0.9970322847366333 pu |
| Proxy FC16 modified request | 20 | 0.997032 → 0.897032 pu (CSV rounds to six decimals) |
| Actual PLC3 coil | 20 | false |
| Actual PLC3 dependency register readback, before next downlink | 21 | 0.8970322608947754 pu |
| Next physical `P_LOAD24` | 21 | 0 MW |

The SCADA `plcs.PLC1.md` source and `plcs.PLC3.md` receiving register are checked separately. The flattened poll dictionary contains duplicate remote variable names and is not used as the owner measurement. Client `delivered_value` confirms a write acknowledgement; it is not asserted to be the post-MITM register value. PLC3 is polled on cycle k+1 before the next downlink, giving a one-cycle-later readback of k's write. The final cycle has no subsequent readback and is marked unobserved. Per-cycle polling, downlink and total-cycle wall-clock durations are copied from the original timing CSVs into `control_trace.csv`; they measure operation duration, not one-way packet latency. The configured ns-3 link delays are retained in the scene YAML. All accepted cycles have zero polling/downlink errors and timeouts.

Baseline and shed metrics:

| Metric | Baseline | Load24 shed |
| --- | ---: | ---: |
| Grid power (MW) | 3.917677126457022 | 3.4770060159601712 |
| Line loss (MW) | 0.20267712645598454 | 0.18200601595964758 |
| Served load (MW) | 3.715 | 3.295 |
| Unserved load (MW) | 0 | 0.420 |
| Bus 2 voltage (pu) | 0.9970322597292005 | 0.9973479713114992 |

First network modification and coil deviation occur in cycle 20; the first changed returned physical snapshot is 21. After cycle 40, voltage remains below the restore threshold and the coil stays off. Each returned AC solve gives the quasi-steady power for the command interval from `input_effective_time_s` to the granted time. Integrating those intervals from 0 to 100 s gives 0.00933333333333335 MWh (9.3333 kWh) unserved energy: 0.420 MW for the 80 one-second commands at cycles 20–99. The initial state occupies no interval. This integral uses applied-command timing; the plotted returned snapshots display the first difference at t=21 s.

No statistical repeats were performed, so no probability of physical deviation is estimated.

## Outputs and reproduction

Run with a Python environment containing `requirements-power.txt` and the project's configured OpenPLC/ns-3 installations:

```bash
python scripts/ieee33_reference.py
python -m src.physics.contract --config examples/ieee33_bw/config_baseline.yaml
PYTHON_BIN="$PWD/output/modular-physics-venv/bin/python" bash scripts/run_ieee33_experiments.sh
output/modular-physics-venv/bin/python scripts/check_ieee33_results.py
output/modular-physics-venv/bin/python -m pip install -r requirements-power-plots.txt
output/modular-physics-venv/bin/python scripts/plot_ieee33_results.py
```

The main runner archives previous IEEE33 output before rerunning. Freshly generated `output/ieee33_bw/` contains:

- `native/{benchmark_reference.json,bus_id_map.json,bus_voltage.csv,line_results.csv,requirements-frozen.txt}`.
- `quick/`, `baseline/`, `transparent/`, `mitm/`: raw runtime evidence, existing CSV exports, and derived `bus_voltage.csv`, `physical_metrics.csv`, `control_trace.csv`.
- `comparison.csv`: per-cycle voltage and loss changes, plus unserved power.
- `validation.json`: experiment status and key event metrics. Missing unstarted runs are `not_run`; incomplete or inconsistent existing runs are `failed`.
- `figures/{electrical_topology,voltage_profile,cross_layer_timeline}.{svg,png}`: generated from validated actual data. The topology marks five open ties and represents electrical, rather than software-network, connectivity.

All original large output files remain ignored by Git. Graphs were generated with matplotlib's noninteractive backend and visually inspected.

## Regression results

- Complete unittest suite: **90 passed**, including 13 new IEEE33 tests, with real optional power dependencies installed. No mocked solver or simulated PLC substitute is used for the new model or full-loop acceptance.
- The results checker was also run against isolated copies with an altered transparent raw voltage and a fabricated PLC3 register readback; both inconsistencies were rejected. Original experiment files were untouched.
- Original water Python environment: **63 passed, 27 optional-power tests skipped**. Missing optional dependencies are not counted as passed solver tests.
- C-Town 100-round reference/current comparison against hardening commit `784f019`: 101 physical CSV rows, 99 actuator rows and 99 key SCADA rows equal exactly. Raw physical times, applied commands and sensor values also match (absolute tolerance 1e-10).
- Existing C-Town PLC4 MITM: both hardening-reference and current code ran 100 rounds with 21 actual `PLC8_T7` FC16 modifications in cycles 20–40. The same 101 physics / 99 actuator / 99 key SCADA rows are identical, with matching raw time, command and sensor states. Summary: `output/ieee33-water-attack/validation.json`. The first current attempt failed the PLC5 Modbus startup readiness check before any control cycle; its directory/log are retained as `current-failed-startup` and `current-failed-startup.log`. The complete rerun passed; the failed attempt is not counted as a completed experiment.
- Four-bus baseline/transparent/MITM: passed. First coil difference at 3, physical difference at 4; flexible load 0.8 → 0 MW. Explicit UPSI v2 baseline matches the legacy configuration.
- Thermal box: 10 real PLC cycles, 11 physical states, heating and cooling both exercised; analytical ODE trajectory matches within 1e-10.
- Quality boundary: real `hold_last` preserves the original finite voltage for six deliveries with source time 3 s while the physical voltage remains zero; `fail_fast` rejects cycle 4 as expected and no cycle-5 physical snapshot is generated.

Regression commands used `UPSI_REFERENCE_REF=784f019 PYTHON_BIN="$PWD/output/modular-physics-venv/bin/python" bash scripts/run_upsi_validation.sh` and the same Python with `bash scripts/run_upsi_quality_validation.sh`. The extra water attack ran `scripts/run_all.sh --iterations 100 --logic-wait 0.3` with isolated copies of `examples/c_town/config_mitm_plc4.yaml` for the reference and current trees; configs and console logs are retained in `output/ieee33-water-attack/`.

The earlier results were copied to `output/ieee33-regression-before/` before regression scripts ran. The original C-Town example output is untouched. The user's pre-existing `examples/c_town/README_attack_mitm.md` deletion and untracked `hydro.zip` are not included in the commit.

DoS is an optional follow-up and was **NOT RUN** for IEEE33. Frequency/electromagnetic transients, relay dynamics, unbalanced power flow and simultaneous coupled physical backends are outside this experiment. Native/backend and end-to-end results are distinguished throughout.
