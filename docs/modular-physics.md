# Modular physical simulation

Hydro-CPS-Sim selects the physical simulator through `physics.backend`. Water and power examples use the same OpenPLC generation, SCADA, Modbus, ns-3, attack launcher and persistent closed-loop coordinator.

## Adapters and configuration

`src/physics/base.py` defines the common lifecycle:

```python
adapter = create_physics_adapter(runtime_config)
initial = adapter.initialize()
state = adapter.get_state()
next_snapshot = adapter.step(commands, iteration=1, dt=1.0)
adapter.close()
```

- `initialize()` returns the initial solved snapshot and is idempotent.
- `get_state()` returns a copy of sensor values under configured names.
- `step()` applies BOOL actuator commands, advances the physical sample clock and returns a snapshot. An explicit `iteration` must increase; `dt` is an optional keyword.
- `current_snapshot()` reads the current state without advancing it.
- `close()` releases resources and can be called repeatedly.

Snapshots include `iteration`, `sim_time`, `sim_time_start`, `sim_time_end`, `backend`, `values`, `actuators_applied` and `advanced`. Backend diagnostics can add fields. No solver failure returns mock or cached-success results.

Backend metadata lives in `src/physics/config.py:BACKEND_SPECS`; the factory imports only the selected adapter and solver. Add an adapter implementing the contract and register it there to introduce another domain.

### Water compatibility

Existing C-Town YAML needs no changes. If `physics.backend` is absent, the backend remains `dhalsim_epynet`. `epynet` and `epanet` are aliases. `EpanetAdapter` delegates to the original, unchanged `PhysicsEngine`, including its initial zero/configured rows, time behavior and actuator semantics.

The historical `hydraulic_timestep` field remains available; `physics.timestep` provides a generic sampling interval. EPANET intervals must be integer seconds. New non-water backends require an explicit positive, finite interval. `initial_state` provides domain-neutral initialization values; `initial_tank_values` remains supported. A solved power initialization comes from the actual load-flow result, not from placeholder values.

### Power configuration

```yaml
physics:
  backend: pandapower
  timestep: 1.0
  init_style: current
  model_file: model.yaml
  sensors:
    V_CONTROL: {table: res_bus, element: control, column: vm_pu}
  actuators:
    LOAD_SW: {table: switch, element: load_breaker, column: closed}
```

`model_file` is relative to the scenario YAML and defines named buses, external grids, lines, loads and switches. Sensor mappings select named result-table elements and columns. Supported result tables are `res_bus`, `res_line`, `res_load` and `res_ext_grid`. Unknown elements, unmapped PLC sensors, invalid BOOL commands and unsupported backends fail explicitly.

BOOL commands map to `switch.closed` or `load.in_service`; an optional `invert: true` reverses a mapping. In the feeder example, `LOAD_SW: true` connects the load, and false opens its line switch. Use `on` and `off` in ST control configuration to express true and false. Existing water `open`/`closed` action words keep their original BOOL semantics.

A disconnected bus has no solved voltage in pandapower. To report its voltage as zero, a bus `vm_pu` mapping must explicitly use `missing: zero_if_unsupplied`; the adapter checks that the bus is actually unsupplied and records its index in `unsupplied_buses`. Other nonfinite values are errors. Zero here represents an unenergized bus, not an invented converged voltage.

Optional model `load_profiles` maps a load name to a list of nonnegative scaling values. Sample zero is used for initialization, subsequent indices follow iterations, and the last value is held after the list ends. This is an external scenario input, independent of PLC decisions.

The backend uses real `pandapower.runpp()` AC power flow. It is a sequence of quasi-static operating points, not an electromagnetic or electromechanical transient model. `dt` advances the sampling clock; it does not integrate differential equations.

## Four-bus / two-PLC example

See `examples/power_feeder/`:

- A 20 kV radial feeder supplies 0.2 MW of essential load and 0.8 MW of flexible load.
- PLC1 exposes voltage, load power, external-grid power and line loading measurements.
- SCADA forwards `PLC1_V_CONTROL` to PLC2 over Modbus TCP.
- PLC2 opens `LOAD_SW` below 0.95 pu and reconnects it above 1.03 pu. Between thresholds, the output holds its previous state. At the configured source voltage, an opened load remains shed until a reset or a changed operating condition.
- The MITM configuration reuses the existing downlink proxy to add −0.15 pu to the FC16 voltage write during iterations 3–6. It never writes the physical state or directly changes the breaker.
- The transparent configuration uses the same proxy and topology with zero bias.

All three configurations retain the attacker endpoint so topology changes do not explain the comparison. Power namespaces have `ns-power-` names. OpenPLC and ns-3 build resources remain shared and use the existing runner locks; do not run scenarios concurrently.

## Setup and execution

Use the existing Ubuntu/OpenPLC/ns-3 installation, with Python 3.10. Install the optional power dependencies in the activated environment:

```bash
python -m pip install -r requirements-power.txt
python -m pip check
python -m unittest discover -s tests -v
```

The optional backend is pinned to pandapower 3.2.1. The core water requirements are unchanged. Power examples use relative dependency paths pointing to `OpenPLC_v3` and `ns-3-dev-git` alongside this repository; change them if your dependencies are elsewhere.

Run each case with the shared entry point:

```bash
PYTHON_BIN="$(command -v python)" bash scripts/run_all.sh --config examples/power_feeder/config.yaml
PYTHON_BIN="$(command -v python)" bash scripts/run_all.sh --config examples/power_feeder/config_mitm_transparent.yaml
PYTHON_BIN="$(command -v python)" bash scripts/run_all.sh --config examples/power_feeder/config_mitm.yaml
python scripts/check_power_results.py --output examples/power_feeder/output
```

Or run the same sequence and its checks from an interactive terminal as a normal user:

```bash
PYTHON_BIN="$(command -v python)" bash scripts/run_power_experiments.sh
```

The runner requests sudo for namespace and PLC networking setup. Without it, full-loop validation cannot run. The experiment script does not change sudo permissions or bypass authentication.

`config.yaml`, `config_mitm_transparent.yaml` and `config_mitm.yaml` write to separate `output/baseline`, `output/transparent` and `output/mitm` directories. Repeating a case clears that case's generated results, following existing runner behavior.

## Exports and acceptance

Historical `physics.csv` columns remain unchanged for C-Town. A new additive `physics_state.csv` includes physical times, solver backend/convergence, sensor values and actuator commands for any domain. Structured snapshots retain backend diagnostics. `reporting.key_variables` selects SCADA key variables without assuming tank-style names.

The full-run checker requires successful shared-runner stage logs, ns-3 readiness, solved samples 0–10, all controller cycles and actual MITM events. It checks:

1. Baseline and zero-bias proxy produce the same physical and actuator trajectories.
2. MITM changes `PLC1_V_CONTROL` only during the configured window.
3. PLC actuator deviation follows the data manipulation, and the load-flow deviation follows the actuator action.
4. The flexible load is served in the baseline and disconnected in the attacked trajectory.

It writes `examples/power_feeder/output/validation.json`, returns nonzero for failed or incomplete runs, and does not accept solver/component tests as a substitute for full OpenPLC/ns-3 execution.

For optional-backend API references, see [pandapower AC power flow](https://pandapower.readthedocs.io/en/v3.2.1/powerflow/ac.html) and [switch elements](https://pandapower.readthedocs.io/en/v3.2.1/elements/switch.html).
