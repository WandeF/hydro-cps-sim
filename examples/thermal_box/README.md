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
