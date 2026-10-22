# Adapter SDK

An adapter provides native signals and physical time through UPSI. It does not import PLC, Modbus, network, attack or scheduler code. The [thermal box adapter](../examples/thermal_box/adapter.py) is a complete standalone example outside the platform's `src/` directory.

## Implement an adapter

Subclass `src.physics.base.PhysicsBackend` and accept one `BackendConfig` argument. It contains the scene path, work directory, model parameters, adapter options, sampling interval and mapped native initial inputs. The `compatibility` field is reserved for historical adapters; new adapters ignore it.

1. `describe()` returns `signals: dict[str, SignalSpec]` and `timing: target | event_step`. Port keys must equal their `SignalSpec.name`.
2. `initialize()` loads the model, applies `config.initial_inputs` and returns a `StepResult` at the starting time. Repeated calls do not reset it.
3. `advance(until_s, inputs)` applies a complete native input map and returns actual physical time, every output and every applied input. A valid step advances time; `target` backends reach the target, while `event_step` backends may stop earlier.
4. `close()` releases resources safely, including partial initialization and repeated calls.

Use `SignalSpec.validate()` for commands and `StepResult.validate()` to check direct SDK results. The platform session also enforces the contract at its boundary. Raise an exception or return an explicit error status when a solver fails. Never substitute previous values as newly solved measurements.

The interface is independent of numerical method. An adapter may substep internally, integrate analytically, call a native solver or calculate a steady operating point. It reports the method in diagnostics and the model kind in `describe()`.

## Declare the plugin

Keep the adapter in an importable Python module and select it through scene configuration:

```yaml
physics:
  backend: my_plant
  plugins:
    my_plant:
      factory: my_package.adapter:PlantBackend
```

For an installed package, declare a named entry point:

```toml
[project.entry-points."hydro_cps.physics"]
my_plant = "my_package.adapter:PlantBackend"
```

Then select it explicitly:

```yaml
physics:
  backend: my_plant
  plugins:
    my_plant:
      entry_point: my_plant
```

Factories and entry points execute Python code and must come from trusted packages. The registry does not scan and activate arbitrary installed adapters. The source must be importable by the same Python executable used in the runner and namespaces. For an uninstalled module in this repository, run commands from the repository root. No core registration, scheduler branch or network/control/attack changes are needed.

## Test the adapter without a PLC

The thermal model is `dT/dt = -loss × (T - ambient) + heater_gain × u`, where `u` is 0 or 1. It also supports a float input in [0, 1] when its model sets `input_dtype: float`.

```python
from pathlib import Path
import yaml
from src.physics.registry import configure_plugins, create_backend
from src.physics.types import BackendConfig

scene = Path("examples/thermal_box/config.yaml").resolve()
raw = yaml.safe_load(scene.read_text())
configure_plugins(raw)
config = BackendConfig(
    config_path=scene,
    work_dir=Path("output/sdk-example"),
    model=raw["physics"]["model"],
    options=raw["physics"],
    step_seconds=1.0,
    initial_inputs={"heater.enabled": False},
)
backend = create_backend("thermal_box", config)
try:
    ports = backend.describe()["signals"]
    initial = backend.initialize()
    initial.validate(ports)
    heated = backend.advance(1.0, {"heater.enabled": True})
    heated.validate(ports)
    print(heated.time_s, heated.outputs)
finally:
    backend.close()
```

At 1 s, this model reaches approximately 19.666138 °C with 800 W heater power. This is an actual ODE solution; it does not depend on either built-in simulator.

## Run the existing closed loop

```bash
PYTHON_BIN="$(command -v python)" bash scripts/run_all.sh \
  --config examples/thermal_box/config.yaml --logic-wait 0.3
```

The same runner generates and compiles two PLC programs, creates namespaces and ns-3 networking, starts OpenPLC/SCADA and advances the configured adapter. PLC1 exposes temperature and power; PLC2 turns heating on below 20 °C and off above 22 °C. Physical commands are BOOL values; protocol conversion stays in the existing PLC/Modbus layer.

Before integrating another backend, exercise initialization/close, a known physical trajectory, unit/type/range failures, a failed solver and a stale or invalid result. Verify it in a fresh process with only its own dependencies available. The test suite also checks installed entry-point metadata and real EPANET/pandapower compatibility.
