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
registry = configure_plugins(raw)
config = BackendConfig(
    config_path=scene,
    work_dir=Path("output/sdk-example"),
    model=raw["physics"]["model"],
    options=raw["physics"],
    step_seconds=1.0,
    initial_inputs={"heater.enabled": False},
    registry=registry,
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


## Context and fault contracts

Capture `registry = configure_plugins(raw)` and pass it through `BackendConfig.registry` or `create_backend(..., registry=registry)`. Scene registrations never modify the global programmatic registry. A plugin name can be reused by another scene with a different source. Reserved built-in names and aliases cannot be replaced. `register_backend(..., registry=context)` supports explicit programmatic registration in one context; its global form remains for existing SDK applications.

A plugin is trusted executable Python. Module/class and entry-point source strings are validated for shape and loaded only when selected. Import errors identify the failing source. Context isolation is not a sandbox or a guarantee that a native solver is thread-safe.

Return `StepResult.validity` and `freshness` explicitly when necessary. Existing adapters may omit them: valid solved measurements default to true/fresh, configured initialization to false/initial. `unsupplied` describes physical state and does not by itself prescribe usability. The default strict measurement timestamp contract remains active; bounded asynchronous measurements require both the backend capability and an explicit scene age limit.

Keep diagnostic details under `diagnostics`. They cannot override standardized snapshot fields. Close safely after partial initialization and failures. If advancement and cleanup both fail, the session records both and preserves the advancement exception. No failed result is published with a ready marker.

Unit transformations must match the catalog and dimensions. Unknown/domain units and `pu` permit identity only. Same-unit calibration uses an explicit `calibration_reason`; water-column pressure conversion needs density and gravity. See [UPSI](upsi.md) for the full quality and unit contract.

## Rootless contract fixture and standalone checks

Third-party SDK tests can use the reusable fixture without PLC programs, root, namespaces or old runtime output:

```python
from src.physics.contract import exercise_backend
samples = exercise_backend(backend, config.initial_inputs, config.timestep_seconds, steps=3)
```

The fixture validates descriptions/results, repeated initialization, input application and actual time grants, and always attempts cleanup. An original failure is retained if cleanup also fails.

Run individual real physical backends without starting OpenPLC or ns-3:

```bash
python -m src.physics.contract --config examples/thermal_box/config.yaml
python -m src.physics.contract --config examples/c_town/config.yaml
python -m src.physics.contract --config examples/power_feeder/config_v2.yaml
```

Missing declared optional solver dependencies produce an explicit `skipped` status. Other loading/model failures produce `failed`. These are standalone solver checks, not complete network experiments. Full-loop and policy validation commands are listed in [the hardening report](upsi-hardening-validation.md).
