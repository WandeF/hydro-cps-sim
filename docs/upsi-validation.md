# UPSI implementation and validation

P0–P4 are implemented and validated on 2026-10-22. The platform now loads an independent third adapter through scene configuration and runs it through the same native OpenPLC/SCADA/ns-3 loop as water and power models.

## Implementation

Compared with `4dd6b2b`, the framework adds the following capabilities:

| Stage | Implementation | Acceptance |
| --- | --- | --- |
| P0 | `base.PhysicsBackend`, `types.SignalSpec`, `types.StepResult`, `registry.register_backend/create_backend` | One lifecycle and typed scalar protocol; lazy configured factories and installed entry points |
| P1 | `adapters.epanet.EpanetBackend`, `adapters.pandapower.PandapowerBackend`, `session.PhysicsSession` | Both real simulators run through UPSI; no simulator-specific branch in the scheduler |
| P2 | `signal_mapper.SignalMapper`, `normalization.normalize_physics` | Port/direction/type/unit/range validation, affine conversions and BOOL inversion; arbitrary logical names |
| P3 | `time_coordinator.TimeCoordinator`, `io.physics_export`, `session._fail` | Actual time grants, timestamp/quality/validity tracking, schema and signal tables, explicit failure records |
| P4 | `examples/thermal_box/adapter.py`, [adapter SDK](adapter-sdk.md) | An independent ODE adapter loads through configuration without changing network/control/attack code or the built-in registry |

Historical naming, bootstrap rows, physical-engine internals and CSV conversion are isolated under `physics/legacy/` and `io/legacy_export.py`. Compatibility imports retain the existing `PhysicsEngine`, step adapters and `io.dhalsim` entry points. The historical engine was relocated without changing its implementation.

## Automated tests

```bash
output/modular-physics-venv/bin/python -m unittest discover -s tests -v
/home/lzh/anaconda3/envs/hydro-cps/bin/python -m unittest discover -s tests -v
```

- Power environment: **54 tests passed**.
- Original water environment: **40 passed, 14 optional power tests skipped**.
- Dependency consistency, shell syntax, local documentation links and Git whitespace checks passed.

Tests cover actual EPANET/pandapower calculations, the independent thermal ODE, explicit installed entry-point metadata, fresh-process imports without either built-in solver, signal transforms/ranges/types, BOOL inversion, unknown ports, duplicate registrations, invalid status, stale timestamps, time stalls and solver exceptions. Failure tests check that the session becomes unavailable and records the last valid physical time.

Water metadata tests distinguish the INP `PRESSURE EXPONENT` option from pressure units and preserve model-path fallback beside copied legacy scenes. C-Town pressure is described as `mH2O`, tank levels as `m` and native flow as `LPS`.

## Full C-Town regression

A clean archive of reference commit `4dd6b2be27899fb55ee9973cb81a5d58120f1a17` and the current implementation each ran the original C-Town configuration with `--iterations 100 --logic-wait 0.3`. Each used native OpenPLC, Linux namespaces, ns-3 and SCADA. Output directories were isolated from `examples/c_town/output/`.

| Compared result | Rows per run | Differences |
| --- | ---: | ---: |
| Historical `physics.csv` | 101 | 0 |
| `actuator_state.csv` | 99 | 0 |
| `scada_observed_key.csv` | 99 | 0 |

The historical iteration convention is retained: rows 0 and 1 are bootstrap/configured initialization, followed by 99 control cycles to physical row 100. The checker also compares raw physical times, commands and all exposed sensor values at full precision (absolute tolerance `1e-10`).

The hydraulic engine granted 104 s, 196 s and 300 s intervals during this run, ending at physical time 29,400 s. These actual event times are preserved through UPSI. The coordinator does not calculate them from cycle number × 300 s. Initialization observations are marked `initial` with `valid: false`; solved measurements carry their actual timestamps and valid quality.

## Power regression

All three original power scenarios and the explicit v2 baseline completed ten PLC cycles and eleven solved pandapower snapshots:

| Scenario | Result |
| --- | --- |
| Baseline | Flexible load remains connected at 0.8 MW |
| Transparent proxy | Physical and actuator trajectories equal baseline; four zero-bias FC16 writes recorded |
| MITM | Four FC16 voltage writes to PLC2 changed by −0.15 pu, only at cycles 3–6 |
| Control consequence | PLC opens `LOAD_SW` at cycle 3 |
| Physical consequence | Snapshot 4 at 4 s reports 0 MW flexible load |
| v2 baseline | Same physical trajectory as the original-format baseline |

All power-flow samples converged. The attacked load remains shed after the attack window because the 1.018059 pu control-bus voltage is below the configured 1.03 pu reconnection threshold. Disconnected-bus zero voltage uses the explicit unsupplied-bus policy and is marked `quality: unsupplied`.

## Third adapter

`examples/thermal_box/adapter.py` is outside the platform core and is selected only by the scenario's `physics.plugins` declaration. Its fresh-process SDK test imports neither EPANET nor pandapower.

The full scenario compiled and ran two native OpenPLC programs and used the shared SCADA, Modbus, namespace and ns-3 paths. It produced eleven physical samples and ten controller cycles. The actual PLC command sequence included heating and cooling; the measured temperature trajectory matched the analytical solution of the configured first-order ODE to `1e-10`. At 10 s the temperature was 20.787404280085017 °C and the heater was off.

No solver, PLC or network substitutes were used for these complete experiments. Pure SDK/component tests are reported separately above.

## Reproduce

Use the shared platform installation and the optional power environment. No new runtime dependency is required beyond the previously installed PyYAML, pymodbus, OpenPLC, ns-3, epynet and optional pandapower 3.2.1.

From the repository root, in a normal-user terminal with sudo available:

```bash
PYTHON_BIN="$PWD/output/modular-physics-venv/bin/python" bash scripts/run_upsi_validation.sh
```

The script extracts a clean local reference archive, runs both water cases and all power/thermal cases, then checks their actual artifacts. `UPSI_REFERENCE_REF` may select a different local reference commit. It does not push Git changes. Re-running replaces the generated validation and example-case outputs; the original C-Town output directory is not used.

To check existing results without rerunning:

```bash
output/modular-physics-venv/bin/python scripts/check_upsi_results.py --output output/upsi-regression
```

The aggregate result is `output/upsi-regression/validation.json` with `status: passed`. Missing or unsuccessful full runs fail with a nonzero status. Stage timings and ns-3 readiness are required, so component tests cannot stand in for complete execution.

Console logs are `output/upsi-regression/*-console.log`. Each run retains its own `timing/run_all_timing.csv`, `reports/`, `runtime/` and network/PLC logs. Unit-test logs are `output/upsi-tests.log` and `output/upsi-legacy-tests.log`. Generated results, clean reference archives and the optional Python environment remain outside Git.

## Current scope

The interface supports bool, int, float and string values, including analog inputs in standalone adapters. The current PLC transport supports float observations and BOOL commands; other bindings are rejected explicitly. Pandapower solves quasi-static AC operating points, and the thermal model uses analytical integration. The inherited EPANET native-handle cleanup safeguard is retained for compatibility with the installed library build.

P5—FMI/FMU execution and simultaneous multi-backend synchronization—remains future work. This release provides the single-backend interface and time boundary for those extensions.
