# Modular physical simulation

Hydro-CPS-Sim uses the [Unified Physical Simulation Interface (UPSI)](upsi.md) for physical simulation. EPANET, pandapower and independently declared Python plugins share the same lifecycle, typed signals, time coordination and closed-loop scheduler.

- [UPSI configuration and data protocol](upsi.md)
- [Third-party adapter SDK](adapter-sdk.md)
- [Acceptance results and reproduction](upsi-validation.md)
- [C-Town and historical compatibility](upsi.md#existing-c-town-and-power-scenarios)
- [Power feeder example](../examples/power_feeder/README.md)
- [Independent thermal adapter](../examples/thermal_box/README.md)

The previous `PhysicsAdapter.step()` API and `src.physics.engine.PhysicsEngine` remain available to existing Python callers. New integrations implement `PhysicsBackend.describe()`, `initialize()`, `advance()` and `close()`. The shared runner now uses `PhysicsSession` and loads adapters through the plugin registry.

Install optional power dependencies with `python -m pip install -r requirements-power.txt`. Existing water dependencies are unchanged. In a normal-user terminal with sudo available, run all three power cases with:

```bash
PYTHON_BIN="$(command -v python)" bash scripts/run_power_experiments.sh
```

The power backend performs actual quasi-static AC power flow. A disconnected bus reports zero voltage only through an explicit `zero_if_unsupplied` policy checked against the network's unsupplied-bus topology. The existing MITM proxy changes Modbus traffic; it does not alter the physical solver directly.
