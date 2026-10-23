# Thermal box

A third-party UPSI adapter for a first-order thermal model. The adapter lives beside its model and scene configuration and is loaded through `physics.plugins`, with no entry in the platform's built-in registry.

```text
dT/dt = -0.1 × (T - 18) + 0.8 × HEATER
heater power = 800 × HEATER W
```

The initial temperature is 19 °C. The adapter integrates this ODE analytically. PLC1 exposes temperature and heater power. PLC2 enables heating below 20 °C, disables it above 22 °C and holds its output between these thresholds.

From the repository root:

```bash
PYTHON_BIN="$(command -v python)" bash scripts/run_all.sh \
  --config examples/thermal_box/config.yaml --logic-wait 0.3
```

This runs the same native OpenPLC, SCADA, Modbus, namespaces and ns-3 loop as the water and power scenarios. It requires the shared platform dependencies and PyYAML, with no EPANET or pandapower import for the physical adapter.

Results are written under `output/baseline/`. Re-running replaces that case's generated runtime and reports. Physical metadata, quality and timestamps appear in `reports/signal_schema.json` and `reports/csv/physics_signals.csv`.

See [UPSI](../../docs/upsi.md), the [adapter SDK](../../docs/adapter-sdk.md) and [validation results](../../docs/upsi-validation.md). `adapter.py` also supports analog control for standalone SDK use; the current PLC scenario uses BOOL commands.

## Figures

From the repository root, with matplotlib installed:

```bash
output/modular-physics-venv/bin/python scripts/plot_example_results.py --example thermal_box
```

Figures are written to `examples/thermal_box/output/baseline/figures/` in PNG, SVG and PDF:

| Figure | Content |
| --- | --- |
| `model_diagram` | Thermal box, heater, ambient heat loss, ODE and PLC hysteresis |
| `temperature_response` | Recorded temperatures, analytical interval solutions and applied heater power |
| `control_timeline` | Physical temperature, PLC2 register readback, actual command and returned heater power |

The current 10-cycle run heats from 19 °C to 22.1583 °C at 6 s. PLC2 disables heating in cycle 6; physical snapshot 7 is the first cooling result. The final temperature at 10 s is 20.7874 °C. Applied heater power is plotted on the command interval [k, k+1]; the returned power in snapshot k+1 describes that interval. The analytical curve uses the recorded PLC commands, and every interval endpoint is checked against the real physical result within 1e-10 °C.

`control_trace.csv`, `temperature_trace.csv` and `manifest.json` beside the figures preserve plotted values, source time, readback alignment and source-file SHA-256 hashes. The plotting command does not require pandapower or EPANET and does not rerun the experiment. `requirements-power-plots.txt` provides the shared optional plotting environment for all three examples.
