# Power feeder example

A four-bus pandapower feeder controlled through two OpenPLC instances, SCADA and ns-3. See [modular physics documentation](../../docs/modular-physics.md) for configuration, setup, actuator semantics and validation.

| File | Purpose |
| --- | --- |
| `model.yaml` | Named 20 kV buses, lines, loads, utility and load breaker |
| `config.yaml` | Attack-free baseline |
| `config_mitm_transparent.yaml` | Same topology and existing Modbus proxy with zero bias |
| `config_mitm.yaml` | −0.15 pu bias on `PLC1_V_CONTROL` downlink to PLC2, iterations 3–6 |

From the repository root, after installing `requirements-power.txt` and the shared OpenPLC/ns-3 dependencies:

```bash
PYTHON_BIN="$(command -v python)" bash scripts/run_power_experiments.sh
```

Run as a normal user in an interactive terminal with sudo available. This executes the shared `scripts/run_all.sh` entry point for every case and then checks the results. The command does not substitute reference control or mock data for PLC execution.

The baseline keeps the 0.8 MW flexible load connected. A voltage below 0.95 pu opens its breaker; reconnection requires voltage above 1.03 pu. Output folders are separate per case. Re-running a case replaces its generated runtime and reports.

The adapter solves quasi-static AC power flow, not electrical transients. Full-loop results and reproduction commands are recorded in [the validation report](../../docs/upsi-validation.md).

`config_v2.yaml` runs the same baseline model using the explicit UPSI model, timing and signal schema. Its output is `output/schema_v2/`.

## Figures

From the repository root, after installing `requirements-power-plots.txt`:

```bash
output/modular-physics-venv/bin/python scripts/plot_example_results.py --example power_feeder
```

Figures are written to `examples/power_feeder/output/figures/` in PNG, SVG and PDF:

| Figure | Content |
| --- | --- |
| `electrical_topology` | Four buses, line lengths, original loads and controlled breaker |
| `state_comparison` | Baseline, transparent and MITM voltage, load power and line loading |
| `cross_layer_timeline` | Raw voltage, PLC2 register readback, actual breaker command and subsequent power snapshots |

The script validates the existing real-loop results before drawing. It aligns the PLC2 register readback at cycle k+1 with the corresponding write at k, leaving the final unavailable readback blank. Zero voltage at the disconnected load bus retains its `unsupplied` meaning. Curves show returned AC quasi-steady snapshots; command k first appears in physical snapshot k+1. The attack window covers cycles 3–6, the first breaker deviation is 3, and the first physical deviation is 4.

`control_trace.csv`, `state_comparison.csv` and `manifest.json` beside the figures preserve plotted values, timing semantics and SHA-256 hashes of the source files. Rendering does not rerun the simulation or modify its raw results.
