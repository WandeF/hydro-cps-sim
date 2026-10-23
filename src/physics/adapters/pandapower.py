"""Real quasi-static AC power flow behind configured sensor/actuator mappings."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

from src.core.config import RuntimeConfig, load_yaml
from src.physics.base import PhysicsAdapter, Snapshot
from src.physics.config import physics_options, physics_timestep


class PandapowerAdapter(PhysicsAdapter):
    """Four-bus examples are data, not special cases in the shared runtime.

    Models are declarative YAML. Boolean commands map to switch.closed or
    load.in_service; positive dt advances the quasi-static sampling clock.
    Load flow is solved at every sample; no dynamic/transient model is implied.
    """

    def __init__(self, cfg: RuntimeConfig, work_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.work_dir = work_dir or cfg.output_dir / "runtime"
        self.options = physics_options(cfg.raw)
        self.timestep = physics_timestep(cfg.raw) or float(cfg.hydraulic_timestep)
        self.available = False
        self.warning = None
        self.net = None
        self.closed = False
        self.iteration = 0
        self.sim_time = 0.0
        self.state: dict[str, float] = {}
        self.actuator_state = dict(cfg.actuator_initial_state)
        self._names: dict[str, dict[str, int]] = {}
        self._sensor_map = self.options.get("sensors", {})
        self._actuator_map = self.options.get("actuators", {})
        self.initialize()

    def _index(self, table: str, name: Any) -> int:
        try:
            return self._names[table][str(name)]
        except KeyError as exc:
            raise ValueError(f"Unknown {table} element {name!r}") from exc

    def _remember(self, table: str, name: str, index: int) -> None:
        if name in self._names.setdefault(table, {}):
            raise ValueError(f"Duplicate {table} element {name!r}")
        self._names[table][name] = index

    def _build_model(self, pp: Any, model: dict[str, Any]) -> None:
        self.provenance = None
        if model.get("source") == "pandapower_builtin":
            from src.physics.adapters.power_models import load_builtin
            self.net, self._names, self.provenance = load_builtin(model)
            return
        if "source" in model:
            raise ValueError("Unknown power model source")
        self.net = pp.create_empty_network(sn_mva=float(model.get("sn_mva", 1.0)))
        for item in model.get("buses", []):
            item = dict(item)
            name = str(item.pop("name"))
            self._remember("bus", name, pp.create_bus(self.net, name=name, **item))
        for item in model.get("ext_grids", []):
            item = dict(item)
            name = str(item.pop("name"))
            item["bus"] = self._index("bus", item["bus"])
            self._remember("ext_grid", name, pp.create_ext_grid(self.net, name=name, **item))
        for item in model.get("lines", []):
            item = dict(item)
            name = str(item.pop("name"))
            for key in ("from_bus", "to_bus"):
                item[key] = self._index("bus", item[key])
            self._remember("line", name, pp.create_line_from_parameters(self.net, name=name, **item))
        for item in model.get("loads", []):
            item = dict(item)
            name = str(item.pop("name"))
            item["bus"] = self._index("bus", item["bus"])
            self._remember("load", name, pp.create_load(self.net, name=name, **item))
        for item in model.get("switches", []):
            item = dict(item)
            name = str(item.pop("name"))
            item["bus"] = self._index("bus", item["bus"])
            element_table = {"l": "line", "b": "bus"}.get(item.get("et"))
            if element_table is None:
                raise ValueError("Model switches support et=l (line) or b (bus)")
            item["element"] = self._index(element_table, item["element"])
            self._remember("switch", name, pp.create_switch(self.net, name=name, **item))
        for table in ("bus", "ext_grid", "line", "load"):
            if not self._names.get(table):
                raise ValueError(f"Power model needs at least one {table} element")

    def _validate_mappings(self) -> None:
        if not isinstance(self._sensor_map, dict) or not self._sensor_map:
            raise ValueError("physics.sensors must be a nonempty mapping")
        if not isinstance(self._actuator_map, dict):
            raise ValueError("physics.actuators must be a mapping")
        declared = {str(tag) for plc in self.cfg.raw.get("plcs", []) for tag in plc.get("sensors", [])}
        missing = declared - self._sensor_map.keys()
        if missing:
            raise ValueError(f"Unmapped physical sensors: {sorted(missing)}")
        if set(self.actuator_state) != set(self._actuator_map):
            raise ValueError("physics.actuators must map every configured actuator exactly once")
        for tag, spec in self._sensor_map.items():
            if not isinstance(spec, dict):
                raise ValueError(f"Invalid sensor mapping: {tag}")
            table = str(spec.get("table", ""))
            if table == "metrics":
                if spec.get("element") != "total" or spec.get("column") not in (
                        "p_grid_mw", "p_loss_mw", "served_load_mw", "unserved_load_mw"):
                    raise ValueError(f"Unknown derived power metric for {tag}")
                continue
            if table not in ("res_bus", "res_line", "res_load", "res_ext_grid"):
                raise ValueError(f"Unsupported result table for {tag}: {table}")
            self._index(table.removeprefix("res_"), spec.get("element"))
            if spec.get("missing", "error") not in ("error", "zero_if_unsupplied"):
                raise ValueError(f"Unknown missing-value policy for {tag}")
            if spec.get("missing") == "zero_if_unsupplied" and (table != "res_bus" or spec.get("column") != "vm_pu"):
                raise ValueError("zero_if_unsupplied is only valid for bus voltage magnitude")
        for tag, spec in self._actuator_map.items():
            if not isinstance(spec, dict):
                raise ValueError(f"Invalid actuator mapping: {tag}")
            table, column = spec.get("table"), spec.get("column")
            if (table, column) not in (("switch", "closed"), ("load", "in_service")):
                raise ValueError(f"Invalid BOOL actuator mapping for {tag}: {table}.{column}")
            self._index(table, spec.get("element"))
            if not isinstance(spec.get("invert", False), bool):
                raise ValueError(f"invert must be boolean for {tag}")

    def initialize(self) -> Snapshot:
        if self.closed:
            raise RuntimeError("Physics adapter is closed")
        if self.available:
            return self.current_snapshot()
        try:
            import pandapower as pp
        except ImportError as exc:
            raise RuntimeError("pandapower backend requires: python -m pip install -r requirements-power.txt") from exc
        raw_path = self.options.get("model_file")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("pandapower requires physics.model_file")
        path = Path(raw_path).expanduser()
        self.model_path = path if path.is_absolute() else self.cfg.config_path.parent / path
        self.model = load_yaml(self.model_path)
        self._build_model(pp, self.model)
        self._validate_mappings()
        self._apply_profiles(0)
        self._apply_actuators(self.actuator_state)
        self._solve()
        self.available = True
        return self.current_snapshot()

    def _apply_actuators(self, commands: Mapping[str, bool]) -> None:
        unknown = commands.keys() - self._actuator_map.keys()
        if unknown:
            raise ValueError(f"Unknown actuators: {sorted(unknown)}")
        if any(not isinstance(value, bool) for value in commands.values()):
            raise ValueError("Actuator commands must be BOOL values")
        for tag, value in commands.items():
            spec = self._actuator_map[tag]
            actual = not value if spec.get("invert", False) else value
            self.net[spec["table"]].at[self._index(spec["table"], spec["element"]), spec["column"]] = actual
        self.actuator_state.update(commands)

    def _solve(self) -> None:
        import pandapower as pp
        options = self.options.get("solver", {})
        if not isinstance(options, dict) or set(options) - {"algorithm", "max_iteration", "tolerance_mva"}:
            raise ValueError("physics.solver accepts algorithm, max_iteration and tolerance_mva")
        try:
            pp.runpp(self.net, numba=False, **options)
            if not self.net.converged:
                raise RuntimeError("solver did not converge")
            self.unsupplied = set(pp.topology.unsupplied_buses(self.net))
            from src.physics.adapters.power_models import power_metrics
            self.metrics = power_metrics(self.net)
            state = {}
            for tag, spec in self._sensor_map.items():
                table = spec["table"]
                if table == "metrics":
                    state[str(tag)] = self.metrics[spec["column"]]
                    continue
                index = self._index(table.removeprefix("res_"), spec["element"])
                value = float(self.net[table].at[index, spec["column"]])
                if not math.isfinite(value):
                    if spec.get("missing") == "zero_if_unsupplied" and index in self.unsupplied:
                        value = 0.0
                    else:
                        raise ValueError(f"Nonfinite result for sensor {tag}")
                state[str(tag)] = value
            self.state = state
        except Exception as exc:
            self.available = False
            self.warning = f"pandapower solve failed at iteration {self.iteration}: {exc}"
            raise RuntimeError(self.warning) from exc

    def get_state(self) -> dict[str, float]:
        if self.closed or not self.available:
            raise RuntimeError("Physics adapter has no valid solved state")
        return dict(self.state)

    def current_snapshot(self, iteration: int | None = None) -> Snapshot:
        return {
            "iteration": self.iteration if iteration is None else iteration,
            "backend": "pandapower", "domain": "power", "warning": self.warning,
            "sim_time": self.sim_time, "sim_time_start": self.sim_time, "sim_time_end": self.sim_time,
            "timestep": self.timestep, "values": self.get_state(),
            "actuators_applied": dict(self.actuator_state),
            "solver_converged": bool(self.net.converged),
            "unsupplied_buses": sorted(int(bus) for bus in self.unsupplied),
            "advanced": False,
        }

    def _apply_profiles(self, iteration: int) -> None:
        profiles = self.model.get("load_profiles", {})
        if not isinstance(profiles, dict):
            raise ValueError("load_profiles must be a mapping")
        for name, profile in profiles.items():
            if not isinstance(profile, list) or not profile:
                raise ValueError("Load profiles must be nonempty scaling lists")
            scaling = float(profile[min(iteration, len(profile) - 1)])
            if not math.isfinite(scaling) or scaling < 0:
                raise ValueError("Load scaling must be finite and nonnegative")
            self.net.load.at[self._index("load", name), "scaling"] = scaling

    def step(self, actuator_state: Mapping[str, bool] | None = None,
             iteration: int | None = None, *, dt: float | None = None) -> Snapshot:
        if self.closed or not self.available:
            raise RuntimeError("Physics adapter has no valid solved state")
        duration = self.timestep if dt is None else float(dt)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("dt must be finite and positive")
        next_iteration = self.iteration + 1 if iteration is None else iteration
        if isinstance(next_iteration, bool) or not isinstance(next_iteration, int) or next_iteration <= self.iteration:
            raise ValueError("Physics iterations must increase monotonically")
        self._apply_actuators({} if actuator_state is None else actuator_state)
        self._apply_profiles(next_iteration)
        start = self.sim_time
        self.iteration = next_iteration
        self._solve()
        self.sim_time += duration
        result = self.current_snapshot()
        result.update(sim_time_start=start, sim_time_end=self.sim_time, advanced=True)
        return result

    def close(self) -> None:
        self.available = False
        self.closed = True
        self.net = None


# UPSI adapter. The historical step API above remains import-compatible.
from types import SimpleNamespace
from src.physics.base import PhysicsBackend
from src.physics.types import SignalSpec, StepResult


class PandapowerBackend(PhysicsBackend):
    def __init__(self, config):
        self.config = config
        self.adapter = None
        self.closed = False
        self.result = None
        self.sensors, self.commands = {}, {}
        self.ports = {}
        units = {'vm_pu':'pu', 'va_degree':'degree', 'p_mw':'MW', 'q_mvar':'Mvar',
                 'loading_percent':'%', 'pl_mw':'MW', 'ql_mvar':'Mvar', 'i_ka':'kA',
                 'i_from_ka':'kA', 'i_to_ka':'kA', 'vm_from_pu':'pu', 'vm_to_pu':'pu',
                 'p_grid_mw':'MW', 'p_loss_mw':'MW', 'served_load_mw':'MW', 'unserved_load_mw':'MW',
                 'va_from_degree':'degree', 'va_to_degree':'degree', 'p_from_mw':'MW',
                 'p_to_mw':'MW', 'q_from_mvar':'Mvar', 'q_to_mvar':'Mvar'}
        for group, direction in (('observations','output'), ('commands','input')):
            for name, item in config.options['signals'][group].items():
                port = item['port']
                table, rest = port.split('.', 1)
                element, column = rest.rsplit('.', 1)
                spec = {'table':table, 'element':element, 'column':column}
                if direction == 'output':
                    if column not in units:
                        raise ValueError(f'Unsupported pandapower result column metadata: {column}')
                    old = config.options.get('sensors', {}).get(name, {})
                    if 'missing' in item or 'missing' in old:
                        spec['missing'] = item.get('missing', old.get('missing'))
                    self.sensors[port] = spec
                else:
                    self.commands[port] = spec
                self.ports[port] = SignalSpec(port, direction, 'float' if direction == 'output' else 'bool',
                                              units.get(column, '1') if direction == 'output' else '1')

    def describe(self):
        return {'signals':dict(self.ports), 'timing':'target', 'model_kind':'quasi_static_ac_power_flow'}

    def _capture(self, snapshot):
        quality = {p: 'good' for p in self.sensors}
        for port,spec in self.sensors.items():
            if spec.get('missing') == 'zero_if_unsupplied':
                index = self.adapter._index('bus', spec['element'])
                if index in self.adapter.unsupplied: quality[port] = 'unsupplied'
        diagnostics = {'solver_converged':snapshot['solver_converged'],
                       'unsupplied_buses':snapshot['unsupplied_buses']}
        if self.adapter.provenance is not None:
            a = self.adapter
            diagnostics.update(power_metrics=dict(a.metrics), source=a.provenance['source'],
                               input_tables_sha256=a.provenance['input_tables_sha256'],
                               sampling='quasi_static_ac_power_flow',
                               bus_voltages=[{'bus_number':m['paper_bus_number'],
                                   'vm_pu':float(a.net.res_bus.at[m['index'], 'vm_pu'])
                                       if m['index'] not in a.unsupplied else None,
                                   'quality':'unsupplied' if m['index'] in a.unsupplied else 'good'}
                                   for m in a.provenance['bus_id_map'].values()],
                               controlled_loads={name:bool(a.net.load.at[idx,'in_service'])
                                                 for name,idx in a._names['load'].items()})
            for row in diagnostics['bus_voltages']:
                if row['vm_pu'] is not None and not math.isfinite(row['vm_pu']):
                    raise ValueError('Nonfinite energized bus voltage')
        return StepResult(snapshot['sim_time'], dict(snapshot['values']), dict(snapshot['actuators_applied']),
                          diagnostics=diagnostics, quality=quality)

    def initialize(self):
        if self.closed: raise RuntimeError('Physics backend is closed')
        if self.result is None:
            options = dict(self.config.options)
            options.update(model_file=str(self.config.model_path()), timestep=self.config.step_seconds,
                           sensors=self.sensors, actuators=self.commands)
            options.pop('timing', None)
            cfg = SimpleNamespace(config_path=self.config.config_path, output_dir=self.config.work_dir.parent,
                raw={'physics':options, 'plcs':[{'sensors':list(self.sensors)}]},
                hydraulic_timestep=self.config.step_seconds, actuator_initial_state=dict(self.config.initial_inputs))
            self.adapter = PandapowerAdapter(cfg, self.config.work_dir)
            self.result = self._capture(self.adapter.initialize())
        import copy
        return copy.deepcopy(self.result)

    def advance(self, until_s, inputs):
        if self.closed or self.result is None: raise RuntimeError('Physics backend is not initialized')
        duration = until_s-self.result.time_s
        if duration <= 0: raise ValueError('Target time must increase')
        try:
            self.result = self._capture(self.adapter.step(inputs, dt=duration))
        except Exception:
            self.close()
            raise
        import copy
        return copy.deepcopy(self.result)

    def close(self):
        if self.adapter is not None: self.adapter.close()
        self.closed = True
