# Hydro-CPS-Sim

**A co-simulation platform for water cyber-physical system security research.**

[Documentation](https://wandef.github.io/en/latest/) · [中文文档](https://wandef.github.io/zh/latest/) · [中文 README](README.zh-CN.md)

Hydro-CPS-Sim connects EPANET hydraulics, OpenPLC control programs, Modbus TCP communication and ns-3 networking in one closed loop. You can configure water networks, PLC rules and communication paths, run attack experiments, and observe changes in sensor data, actuator actions and hydraulic states.

## Features

- **Closed-loop simulation:** Pump and valve commands feed back into the hydraulic model to update levels, pressures and flows.
- **PLC execution:** Generate Structured Text (ST) programs from configuration and run separate OpenPLC instances.
- **Configurable networking:** Connect PLC, SCADA and attack endpoints through Linux network namespaces and ns-3.
- **Attack scenarios:** Run Modbus MITM, UDP DoS and PLC logic injection with configurable targets and active windows.
- **Results and logs:** Export hydraulic states, SCADA observations, actuator states, attack events and stage timings.

See [Architecture](https://wandef.github.io/en/latest/architecture.html) for the components and closed-loop workflow.

## Requirements

- Ubuntu 22.04 or 24.04 on x86_64.
- Conda installed and available in your shell.
- Internet access for packages and dependency repositories.
- A normal user account with `sudo` access for installation and network setup.

The installer creates or reuses a Python 3.10 environment named `hydro-cps` and installs the OpenPLC command-line runtime, ns-3 and DHALSIM-epynet. It does not install the OpenPLC web management service.

## Installation

```bash
git clone https://github.com/WandeF/hydro-cps-sim.git
cd hydro-cps-sim

# Preview the installation plan without changing the system.
bash install.sh --dry-run

# Install dependencies and build the runtime.
bash install.sh
conda activate hydro-cps
```

Dependencies are placed in the project parent directory by default. To choose a separate dependency directory or change the environment name and compiler parallelism, use `--parent`, `--env` and `--jobs`. Run `bash install.sh --help` for all options.

The installer updates path fields in the C-Town YAML files and saves changed originals under `output/install-backups/`. If you move the project or its dependencies, update the configuration paths. See [Installation](https://wandef.github.io/en/latest/installation.html) for details.

## Quick start

Run the C-Town baseline after installation:

```bash
bash scripts/run_all.sh --config examples/c_town/config.yaml --logic-wait 0.3
```

For a shorter run:

```bash
bash scripts/run_all.sh --iterations 10 --logic-wait 0.3
```

Start the runner as a normal user in the activated environment; it requests `sudo` for privileged steps. Use `bash scripts/run_all.sh --help` for configuration, iteration, polling and SCADA worker options.

**Save results before another run:** The runner clears `runtime/`, `reports/`, `st/` and `plcs/` under the configured output directory. Copy results you want to keep to a separate directory before starting the next experiment.

## Attack experiments

The C-Town examples include:

| Configuration | Scenario |
| --- | --- |
| [`config.yaml`](examples/c_town/config.yaml) | Attack-free baseline |
| [`config_mitm_plc4.yaml`](examples/c_town/config_mitm_plc4.yaml) | Modify T7 data sent from SCADA to PLC4 |
| [`config_mitm_plc9.yaml`](examples/c_town/config_mitm_plc9.yaml) | Modify the T7 response from PLC8 |
| [`config_mitm_composite_plc7_plc9.yaml`](examples/c_town/config_mitm_composite_plc7_plc9.yaml) | Combined T5 and T7 response manipulation |
| [`config_dos_plc2_single.yaml`](examples/c_town/config_dos_plc2_single.yaml) | UDP DoS targeting PLC2 from one attack endpoint |
| [`config_dos_plc2_three_bots.yaml`](examples/c_town/config_dos_plc2_three_bots.yaml) | UDP DoS targeting PLC2 from three attack endpoints |
| [`config_openplc_logic_plc4.yaml`](examples/c_town/config_openplc_logic_plc4.yaml) | Change the PU10 control threshold at PLC4 |

Select a configuration with `--config`:

```bash
bash scripts/run_all.sh --config examples/c_town/config_mitm_plc4.yaml --logic-wait 0.3
```

Each YAML contains the scenario's network, control and attack settings. See [Security Experiments](https://wandef.github.io/en/latest/experiments.html) and [Configuration](https://wandef.github.io/en/latest/configuration.html) to adjust targets, parameters and active windows.

## Results

The runner prints the result location when it finishes. Paths are relative to `output_path` in the selected YAML:

```text
<output_path>/
├── reports/csv/   # Hydraulic, actuator, SCADA and attack CSV files
├── logs/          # Runtime and network logs
└── timing/        # Runner stage timings
```

Start with `physics.csv`, `actuator_state.csv` and `scada_observed_*.csv` to compare the baseline and attack runs. `attack_schedule.csv`, `attack_events.csv` and `scada_timeout_events.csv` record attack and communication events; `cycle_timing.csv` records closed-loop timing.

## Modular physics and power feeder

UPSI provides typed signals, explicit units and physical time, and configuration-driven simulator plugins. Existing C-Town scenarios keep their EPANET behavior. See [UPSI](docs/upsi.md), the [adapter SDK](docs/adapter-sdk.md), the [power feeder](examples/power_feeder/README.md), the independent [thermal box](examples/thermal_box/README.md) and [validation results](docs/upsi-validation.md).

Install the optional dependencies with `python -m pip install -r requirements-power.txt`. From an interactive terminal with sudo access, run the baseline, transparent-proxy and MITM cases with:

```bash
PYTHON_BIN="$(command -v python)" bash scripts/run_power_experiments.sh
```

## Repository layout

```text
examples/c_town/  Water model and example configurations
examples/power_feeder/  Power feeder and three experiment configurations
docs/            Physics adapters and validation
scripts/         Simulation runner
src/             Hydraulic, control, SCADA, network, attack and I/O modules
tests/           Installer, runtime and physics regression tests
install.sh       Dependency installation and runtime build
requirements.txt Python dependencies
```

## Tests and feedback

The installer runs the Python test suite. To run it again in the activated environment:

```bash
python -m unittest discover -s tests
```

Report issues through [GitHub Issues](https://github.com/WandeF/hydro-cps-sim/issues), including your OS, source revision, configuration, command and relevant error logs.

Research and publication information is available under [Publications & Citation](https://wandef.github.io/en/latest/publications.html).
