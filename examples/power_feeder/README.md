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

The adapter solves quasi-static AC power flow, not electrical transients. Full-loop results and reproduction commands are recorded in [the validation report](../../docs/modular-physics-validation.md).
