#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Physics side of the closed-loop runner.

Supported backend:
  - dhalsim_epynet/epynet: DHALSIM-epynet EPANET wrapper, used by DHALSIM's
    epynet mode, with the same PhysicsEngine.step() interface.

For external PLC/SCADA closed-loop control, INP [CONTROLS] and [RULES] are
stripped by default so EPANET internal controls do not run in addition to
OpenPLC. Override with config.yaml:

physics:
  inp_controls: strip   # strip | keep
  step_order: next-run  # next-run | run-next
  init_flag: 0
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from src.core.config import RuntimeConfig


# EPANET Toolkit numeric constants.  They are stable across EPANET 2.0/2.2 and
# are used here to avoid version-specific enum imports in WNTR/epynet wrappers.
EN_DURATION = 0
EN_HYDSTEP = 1
EN_PATTERNSTEP = 3
EN_PATTERNSTART = 4
EN_REPORTSTEP = 5
EN_RULESTEP = 7

EN_TANKLEVEL = 8
EN_DEMAND = 9
EN_HEAD = 10
EN_PRESSURE = 11

EN_FLOW = 8
EN_STATUS = 11
EN_SETTING = 12


class PhysicsEngine:
    VALID_MODES = {"dhalsim_epynet", "epynet"}

    def __init__(self, cfg: RuntimeConfig, mode: str = "auto", work_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.requested_mode = mode
        self.mode = self._resolve_mode(mode)
        self.iteration = 0
        self.sim_time = 0
        self.state: dict[str, float] = dict(cfg.initial_state)
        self.actuator_state: dict[str, bool] = dict(cfg.actuator_initial_state)

        self.wntr = None
        self.wn = None
        self.available = False
        self.warning: str | None = None
        self.backend_kind = "mock"
        self.work_dir = (work_dir.resolve() if work_dir is not None else (cfg.output_dir / "runtime").resolve())

        self.inp_path: Path | None = None
        self.external_inp_path: Path | None = None

        # WNTR / EPANET Toolkit backend
        self.epanet = None
        self.toolkit_started = False

        # DHALSIM-epynet backend
        self.epynet = None
        self.epynet_started = False
        self.epynet_step_order = "next-run"
        self.epynet_inp_controls = "strip"
        self.epynet_init_flag = 0
        self.epynet_also_set_setting = False
        self.epynet_tank_elevation: dict[str, float] = {}
        self.epynet_tank_init_level: dict[str, float] = {}
        self.epynet_node_names: set[str] = set()
        self.epynet_link_names: set[str] = set()
        self.last_tstep: int | str = ""

        self.node_index: dict[str, int] = {}
        self.link_index: dict[str, int] = {}
        self.last_internal_steps = 0
        self.last_epanet_time = 0

        if mode not in self.VALID_MODES:
            raise ValueError("mode must be one of: dhalsim_epynet, epynet")

        self._try_init_dhalsim_epynet()
        if not self.available:
            raise RuntimeError(self.warning or "DHALSIM-epynet initialization failed")

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------
    def _resolve_mode(self, mode: str) -> str:
        # The project now uses DHALSIM's epynet wrapper as the single physical
        # simulator.  Keep "epynet" as a short alias for CLI/config readability.
        return "dhalsim_epynet" if mode == "epynet" else mode

    def _epynet_options(self) -> dict[str, Any]:
        raw_physics = self.cfg.raw.get("physics") if isinstance(self.cfg.raw, dict) else None
        if not isinstance(raw_physics, dict):
            return {}
        raw_epynet = raw_physics.get("epynet")
        if isinstance(raw_epynet, dict):
            return raw_epynet
        return raw_physics

    def _resolve_inp_path(self) -> Path | None:
        inp_file = self.cfg.raw.get("inp_file")
        if not inp_file:
            self.warning = "config.yaml has no inp_file; use mock physics"
            return None
        inp_path = Path(str(inp_file)).expanduser()
        if inp_path.exists():
            return inp_path.resolve()

        candidates = [
            (self.cfg.config_path.parent / str(inp_file)).resolve(),
            (self.cfg.config_path.parent / inp_path.name).resolve(),
        ]
        for rel in candidates:
            if rel.exists():
                return rel
        self.warning = f"inp_file not found: {inp_file}; use mock physics"
        return None

    @staticmethod
    def _section_name(line: str) -> str | None:
        s = line.strip()
        if s.startswith("[") and "]" in s:
            return s.split("]", 1)[0].strip("[]").upper()
        return None

    def _write_external_control_inp(self, inp_path: Path, filename: str = "epanet_external_control.inp") -> Path:
        """Create a runtime INP with EPANET internal controls disabled.

        External OpenPLC logic is the only controller in this co-simulation.
        The hydraulic network, demands, patterns, pumps, valves, and initial
        statuses are preserved; only [CONTROLS] and [RULES] bodies are removed.
        """
        self.work_dir.mkdir(parents=True, exist_ok=True)
        out = self.work_dir / filename

        lines = inp_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        stripped: list[str] = []
        skip_section: str | None = None
        for line in lines:
            sec = self._section_name(line)
            if sec is not None:
                skip_section = sec if sec in {"CONTROLS", "RULES"} else None
                stripped.append(line)
                if sec in {"CONTROLS", "RULES"}:
                    stripped.append("; disabled by Hydro-CPS-Sim: external OpenPLC controls are used")
                continue
            if skip_section is not None:
                if not line.strip():
                    stripped.append(line)
                continue
            stripped.append(line)

        out.write_text("\n".join(stripped) + "\n", encoding="utf-8")
        return out

    @staticmethod
    def _parse_float(value: str) -> float | None:
        try:
            x = float(value)
            return x if math.isfinite(x) else None
        except Exception:
            return None

    def _parse_inp_meta(self, inp_path: Path) -> None:
        self.epynet_tank_elevation.clear()
        self.epynet_tank_init_level.clear()
        self.epynet_node_names.clear()
        self.epynet_link_names.clear()

        section = ""
        for raw_line in inp_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            sec = self._section_name(raw_line)
            if sec is not None:
                section = sec
                continue
            line = raw_line.split(";", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if not parts:
                continue
            if section in {"JUNCTIONS", "RESERVOIRS", "TANKS"}:
                self.epynet_node_names.add(parts[0])
            if section == "TANKS" and len(parts) >= 3:
                elev = self._parse_float(parts[1])
                init_level = self._parse_float(parts[2])
                if elev is not None:
                    self.epynet_tank_elevation[parts[0]] = elev
                if init_level is not None:
                    self.epynet_tank_init_level[parts[0]] = init_level
            if section in {"PIPES", "PUMPS", "VALVES"}:
                self.epynet_link_names.add(parts[0])

    def _try_init_wntr_toolkit(self) -> None:
        inp_path = self._resolve_inp_path()
        if inp_path is None:
            return

        try:
            import wntr  # type: ignore
            from wntr.epanet.toolkit import ENepanet  # type: ignore
        except Exception as exc:
            self.warning = f"WNTR/EPANET Toolkit import failed: {exc}; use mock physics"
            return

        self.wntr = wntr
        self.inp_path = inp_path
        self.external_inp_path = self._write_external_control_inp(inp_path, "epanet_toolkit_external_control.inp")

        try:
            self.wn = wntr.network.WaterNetworkModel(str(self.external_inp_path))

            self.work_dir.mkdir(parents=True, exist_ok=True)
            rpt = self.work_dir / "epanet_toolkit.rpt"
            out = self.work_dir / "epanet_toolkit.bin"
            self.epanet = ENepanet(str(self.external_inp_path), str(rpt), str(out), version=2.2)
            self.epanet.ENopen(str(self.external_inp_path), str(rpt), str(out))

            self._configure_time_parameters(self.epanet)
            self._build_toolkit_indices()
            self._apply_actuators_to_toolkit(self.actuator_state)

            self.epanet.ENopenH()
            # 00: do not reinitialize flows, do not save hydraulics file.
            self.epanet.ENinitH(0)
            self._apply_actuators_to_toolkit(self.actuator_state)
            self.sim_time = int(self.epanet.ENrunH())
            self.last_epanet_time = self.sim_time
            self.state = self._capture_toolkit_state()
            self.available = True
            self.backend_kind = "epanet_toolkit"
            self.toolkit_started = True
            self.warning = None
        except Exception as exc:
            self.warning = f"EPANET Toolkit initialization failed: {exc}; use mock physics"
            self._close_toolkit_silent()
            self.epanet = None
            self.wn = None
            self.available = False
            self.toolkit_started = False
            self.backend_kind = "mock"

    def _import_epynet_toolkit(self):  # type: ignore[no-untyped-def]
        errors: list[str] = []
        for mod_name, attr in (("epynet.epanet2", "EPANET2"), ("epanet2", "EPANET2")):
            try:
                mod = __import__(mod_name, fromlist=[attr])
                return getattr(mod, attr)
            except Exception as exc:
                errors.append(f"{mod_name}: {exc}")
        raise RuntimeError(
            "Cannot import DHALSIM-epynet. Install it in the active Python environment, e.g.\n"
            "  python -m pip install -e /home/lzh/MASTER/CODE/DHALSIM-epynet\n"
            "Import errors: " + " | ".join(errors)
        )

    def _try_init_dhalsim_epynet(self) -> None:
        inp_path = self._resolve_inp_path()
        if inp_path is None:
            return

        opts = self._epynet_options()
        self.epynet_inp_controls = str(opts.get("inp_controls", "strip")).strip().lower()
        if self.epynet_inp_controls not in {"strip", "keep"}:
            self.epynet_inp_controls = "strip"
        self.epynet_step_order = str(opts.get("step_order", "next-run")).strip().lower()
        if self.epynet_step_order not in {"next-run", "run-next"}:
            self.epynet_step_order = "next-run"
        try:
            self.epynet_init_flag = int(opts.get("init_flag", 0))
        except Exception:
            self.epynet_init_flag = 0
        self.epynet_also_set_setting = bool(opts.get("also_set_setting", False))

        self.inp_path = inp_path
        self.external_inp_path = (
            self._write_external_control_inp(inp_path, "dhalsim_epynet_external_control.inp")
            if self.epynet_inp_controls == "strip"
            else inp_path
        )
        self._parse_inp_meta(self.external_inp_path)

        try:
            EPANET2 = self._import_epynet_toolkit()
            self.epynet = EPANET2()
            self.work_dir.mkdir(parents=True, exist_ok=True)
            rpt = self.work_dir / "dhalsim_epynet.rpt"
            out = self.work_dir / "dhalsim_epynet.bin"
            self.epynet.ENopen(str(self.external_inp_path), str(rpt), str(out))
            self._configure_time_parameters(self.epynet)
            self._build_epynet_indices()
            self._apply_actuators_to_epynet(self.actuator_state)
            self.epynet.ENopenH()
            self.epynet.ENinitH(self.epynet_init_flag)
            self._apply_actuators_to_epynet(self.actuator_state)
            self.sim_time = 0
            self.last_epanet_time = 0
            self.state = self._capture_epynet_state()
            self.available = True
            self.backend_kind = "dhalsim_epynet"
            self.epynet_started = True
            self.warning = None
        except Exception as exc:
            self.warning = f"DHALSIM-epynet initialization failed: {exc}; use mock physics"
            self._close_epynet_silent()
            self.epynet = None
            self.available = False
            self.epynet_started = False
            self.backend_kind = "mock"

    def _configure_time_parameters(self, engine: Any) -> None:
        horizon = max(int(self.cfg.raw.get("iterations", self.cfg.iterations) or self.cfg.iterations) + 2, 2)
        duration = max(int(self.cfg.hydraulic_timestep * horizon), int(self.cfg.hydraulic_timestep))
        for code, value in (
            (EN_DURATION, duration),
            (EN_HYDSTEP, self.cfg.hydraulic_timestep),
            (EN_REPORTSTEP, self.cfg.hydraulic_timestep),
            (EN_RULESTEP, self.cfg.hydraulic_timestep),
        ):
            try:
                engine.ENsettimeparam(code, int(value))
            except Exception:
                pass

    def _set_timeparam(self, code: int, value: int) -> None:
        if self.epanet is not None:
            try:
                self.epanet.ENsettimeparam(code, int(value))
            except Exception:
                pass

    def _build_toolkit_indices(self) -> None:
        if self.epanet is None:
            return
        self.node_index.clear()
        self.link_index.clear()
        names = self._all_sensor_names() | set(self.cfg.actuator_initial_state.keys())
        for name in names:
            if name.endswith("F"):
                self._cache_link_index(name[:-1])
            self._cache_node_index(name)
            self._cache_link_index(name)
        for name in self.cfg.actuator_initial_state:
            self._cache_link_index(name)

    def _build_epynet_indices(self) -> None:
        if self.epynet is None:
            return
        self.node_index.clear()
        self.link_index.clear()
        names = self._all_sensor_names() | set(self.cfg.actuator_initial_state.keys())
        for name in names:
            if name.endswith("F"):
                self._cache_epynet_link_index(name[:-1])
            self._cache_epynet_node_index(name)
            self._cache_epynet_link_index(name)
        for name in self.cfg.actuator_initial_state:
            self._cache_epynet_link_index(name)

    def _cache_node_index(self, name: str) -> int | None:
        if self.epanet is None or name in self.node_index:
            return self.node_index.get(name)
        try:
            idx = int(self.epanet.ENgetnodeindex(name))
            if idx > 0:
                self.node_index[name] = idx
                return idx
        except Exception:
            pass
        return None

    def _cache_link_index(self, name: str) -> int | None:
        if self.epanet is None or name in self.link_index:
            return self.link_index.get(name)
        try:
            idx = int(self.epanet.ENgetlinkindex(name))
            if idx > 0:
                self.link_index[name] = idx
                return idx
        except Exception:
            pass
        return None

    def _cache_epynet_node_index(self, name: str) -> int | None:
        if self.epynet is None or name in self.node_index:
            return self.node_index.get(name)
        try:
            idx = int(self.epynet.ENgetnodeindex(name))
            if idx > 0:
                self.node_index[name] = idx
                return idx
        except Exception:
            pass
        return None

    def _cache_epynet_link_index(self, name: str) -> int | None:
        if self.epynet is None or name in self.link_index:
            return self.link_index.get(name)
        try:
            idx = int(self.epynet.ENgetlinkindex(name))
            if idx > 0:
                self.link_index[name] = idx
                return idx
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Common status/actuator helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _bool_to_status_code(value: bool) -> float:
        return 1.0 if value else 0.0

    def _all_sensor_names(self) -> set[str]:
        names: set[str] = set(self.cfg.initial_state.keys())
        for plc in self.cfg.raw.get("plcs", []) or []:
            if not isinstance(plc, dict):
                continue
            for s in plc.get("sensors", []) or []:
                names.add(str(s))
            for c in plc.get("controls", []) or []:
                if isinstance(c, dict) and c.get("dependant"):
                    names.add(str(c["dependant"]))
        return names

    def _actuator_status_map(self, actuator_state: dict[str, bool]) -> dict[str, float]:
        return {str(name): (1.0 if bool(value) else 0.0) for name, value in sorted(actuator_state.items())}

    # ------------------------------------------------------------------
    # EPANET Toolkit backend
    # ------------------------------------------------------------------
    def _apply_actuators_to_toolkit(self, actuator_state: dict[str, bool]) -> None:
        if self.epanet is None:
            return
        for name, is_open in actuator_state.items():
            idx = self._cache_link_index(name)
            if not idx:
                continue
            self.epanet.ENsetlinkvalue(idx, EN_STATUS, self._bool_to_status_code(is_open))

    def _is_tank_node_toolkit(self, name: str) -> bool:
        if self.wn is None:
            return False
        try:
            node = self.wn.get_node(name)
            return hasattr(node, "init_level") and hasattr(node, "elevation")
        except Exception:
            return False

    def _toolkit_node_value(self, name: str) -> float | None:
        if self.epanet is None:
            return None
        idx = self._cache_node_index(name)
        if not idx:
            return None
        if self._is_tank_node_toolkit(name):
            try:
                head = float(self.epanet.ENgetnodevalue(idx, EN_HEAD))
                if math.isfinite(head) and self.wn is not None:
                    node = self.wn.get_node(name)
                    return head - float(getattr(node, "elevation"))
            except Exception:
                pass
            try:
                val = float(self.epanet.ENgetnodevalue(idx, EN_TANKLEVEL))
                if math.isfinite(val):
                    return val
            except Exception:
                pass
            return None

        for code in (EN_PRESSURE, EN_HEAD, EN_DEMAND):
            try:
                val = float(self.epanet.ENgetnodevalue(idx, code))
                if math.isfinite(val):
                    return val
            except Exception:
                continue
        return None

    def _toolkit_link_value(self, name: str) -> float | None:
        if self.epanet is None:
            return None
        idx = self._cache_link_index(name)
        if not idx:
            return None
        for code in (EN_FLOW, EN_STATUS, EN_SETTING):
            try:
                val = float(self.epanet.ENgetlinkvalue(idx, code))
                if math.isfinite(val):
                    return val
            except Exception:
                continue
        return None

    def _toolkit_link_status(self, name: str) -> float | None:
        if self.epanet is None:
            return None
        idx = self._cache_link_index(name)
        if not idx:
            return None
        try:
            return float(self.epanet.ENgetlinkvalue(idx, EN_STATUS))
        except Exception:
            return None

    def _toolkit_link_flow(self, name: str) -> float | None:
        if self.epanet is None:
            return None
        idx = self._cache_link_index(name)
        if not idx:
            return None
        try:
            return float(self.epanet.ENgetlinkvalue(idx, EN_FLOW))
        except Exception:
            return None

    def _capture_toolkit_state(self) -> dict[str, float]:
        state = dict(self.state)
        for sensor in self._all_sensor_names():
            val: float | None
            if sensor.endswith("F"):
                val = self._toolkit_link_flow(sensor[:-1])
            else:
                val = self._toolkit_node_value(sensor)
                if val is None:
                    val = self._toolkit_link_value(sensor)
            if val is not None:
                state[sensor] = float(val)

        # DHALSIM semantics: PU1F/PU2F/V2F are link flows, not BOOL statuses.
        for key in self.actuator_state:
            flow = self._toolkit_link_flow(key)
            if flow is not None:
                state[f"{key}F"] = float(flow)
            else:
                state.setdefault(f"{key}F", 0.0)
        return state

    def _capture_toolkit_link_diagnostics(self) -> tuple[dict[str, float], dict[str, float]]:
        statuses: dict[str, float] = {}
        flows: dict[str, float] = {}
        for name in sorted(self.cfg.actuator_initial_state):
            status = self._toolkit_link_status(name)
            flow = self._toolkit_link_flow(name)
            if status is not None:
                statuses[name] = status
            if flow is not None:
                flows[name] = flow
        return statuses, flows

    def _run_toolkit_step(self, actuator_state: dict[str, bool]) -> dict[str, float]:
        if self.epanet is None or not self.toolkit_started:
            raise RuntimeError("EPANET Toolkit is not initialized")

        self._apply_actuators_to_toolkit(actuator_state)
        target_time = self.sim_time + self.cfg.hydraulic_timestep
        internal_steps = 0

        while self.sim_time < target_time:
            tstep = int(self.epanet.ENnextH())
            self.last_tstep = tstep
            if tstep <= 0:
                raise RuntimeError(
                    f"EPANET hydraulic simulation ended before target time: current={self.sim_time}, target={target_time}"
                )
            current = int(self.epanet.ENrunH())
            internal_steps += 1
            self.sim_time = current
            self.last_epanet_time = current
            if internal_steps > 10000:
                raise RuntimeError("EPANET Toolkit step loop exceeded 10000 internal events")

        self.last_internal_steps = internal_steps
        return self._capture_toolkit_state()

    # ------------------------------------------------------------------
    # DHALSIM-epynet backend
    # ------------------------------------------------------------------
    def _apply_actuators_to_epynet(self, actuator_state: dict[str, bool]) -> None:
        if self.epynet is None:
            return
        for name, is_open in actuator_state.items():
            idx = self._cache_epynet_link_index(name)
            if not idx:
                continue
            v = self._bool_to_status_code(is_open)
            self.epynet.ENsetlinkvalue(idx, EN_STATUS, v)
            if self.epynet_also_set_setting:
                try:
                    self.epynet.ENsetlinkvalue(idx, EN_SETTING, v)
                except Exception:
                    pass

    def _epynet_current_time_from_wrapper(self) -> int | None:
        if self.epynet is None:
            return None
        cur = getattr(self.epynet, "_current_simulation_time", None)
        if cur is not None and hasattr(cur, "value"):
            try:
                return int(cur.value)
            except Exception:
                return None
        return None

    def _epynet_run_current(self) -> int:
        if self.epynet is None:
            return self.sim_time
        result = self.epynet.ENrunH()
        t: int | None = None
        if isinstance(result, (int, float)):
            t = int(result)
        if t is None:
            t = self._epynet_current_time_from_wrapper()
        if t is None:
            t = self.sim_time
        self.sim_time = int(t)
        self.last_epanet_time = self.sim_time
        return self.sim_time

    def _epynet_next_h(self) -> int:
        if self.epynet is None:
            return 0
        tstep = int(self.epynet.ENnextH())
        self.last_tstep = tstep
        return tstep

    def _epynet_node_value(self, name: str) -> float | None:
        if self.epynet is None:
            return None
        idx = self._cache_epynet_node_index(name)
        if not idx:
            return None
        if name in self.epynet_tank_elevation:
            try:
                head = float(self.epynet.ENgetnodevalue(idx, EN_HEAD))
                return head - float(self.epynet_tank_elevation[name])
            except Exception:
                try:
                    return float(self.epynet.ENgetnodevalue(idx, EN_TANKLEVEL))
                except Exception:
                    return None
        for code in (EN_PRESSURE, EN_HEAD, EN_DEMAND):
            try:
                val = float(self.epynet.ENgetnodevalue(idx, code))
                if math.isfinite(val):
                    return val
            except Exception:
                continue
        return None

    def _epynet_link_status(self, name: str) -> float | None:
        if self.epynet is None:
            return None
        idx = self._cache_epynet_link_index(name)
        if not idx:
            return None
        try:
            return float(self.epynet.ENgetlinkvalue(idx, EN_STATUS))
        except Exception:
            return None

    def _epynet_link_flow(self, name: str) -> float | None:
        if self.epynet is None:
            return None
        idx = self._cache_epynet_link_index(name)
        if not idx:
            return None
        try:
            return float(self.epynet.ENgetlinkvalue(idx, EN_FLOW))
        except Exception:
            return None

    def _capture_epynet_state(self) -> dict[str, float]:
        state = dict(self.state)
        for sensor in self._all_sensor_names():
            val: float | None
            if sensor.endswith("F"):
                val = self._epynet_link_flow(sensor[:-1])
            else:
                val = self._epynet_node_value(sensor)
                if val is None:
                    val = self._epynet_link_flow(sensor)
            if val is not None:
                state[sensor] = float(val)

        for key in self.actuator_state:
            flow = self._epynet_link_flow(key)
            if flow is not None:
                state[f"{key}F"] = float(flow)
            else:
                state.setdefault(f"{key}F", 0.0)
        return state

    def _capture_epynet_link_diagnostics(self) -> tuple[dict[str, float], dict[str, float]]:
        statuses: dict[str, float] = {}
        flows: dict[str, float] = {}
        for name in sorted(self.cfg.actuator_initial_state):
            status = self._epynet_link_status(name)
            flow = self._epynet_link_flow(name)
            if status is not None:
                statuses[name] = status
            if flow is not None:
                flows[name] = flow
        return statuses, flows

    def _run_epynet_step(self, actuator_state: dict[str, bool]) -> dict[str, float]:
        if self.epynet is None or not self.epynet_started:
            raise RuntimeError("DHALSIM-epynet is not initialized")

        self._apply_actuators_to_epynet(actuator_state)
        internal_steps = 0
        if self.epynet_step_order == "next-run":
            tstep = self._epynet_next_h()
            if tstep <= 0:
                self.last_internal_steps = 0
                return self._capture_epynet_state()
            self._epynet_run_current()
            internal_steps = 1
        elif self.epynet_step_order == "run-next":
            self._epynet_run_current()
            tstep = self._epynet_next_h()
            internal_steps = 1 if tstep >= 0 else 0
        else:
            raise ValueError(f"unsupported epynet step_order={self.epynet_step_order}")

        self.last_internal_steps = internal_steps
        return self._capture_epynet_state()

    # ------------------------------------------------------------------
    # Legacy/mock helpers
    # ------------------------------------------------------------------
    def _mock_step(self, actuator_state: dict[str, bool]) -> dict[str, float]:
        state = dict(self.state)
        t = self.iteration
        for name in sorted(self._all_sensor_names()):
            if name.endswith("F"):
                # Mock has no hydraulics; keep old BOOL-like placeholder.
                base = name[:-1]
                state[name] = 1.0 if actuator_state.get(base, False) else 0.0
            elif name.startswith("J"):
                state[name] = float(state.get(name, 0.0))
            elif name.startswith("T"):
                base = float(state.get(name, self.cfg.initial_state.get(name, 3.0)))
                delta = 0.02 * math.sin((t + 1) / 3.0)
                state[name] = max(0.0, min(8.0, base + delta))
            else:
                state.setdefault(name, 0.0)

        for key, value in actuator_state.items():
            state[f"{key}F"] = 1.0 if value else 0.0
        return state

    def _zero_values_for_all_runtime_tags(self) -> dict[str, float]:
        values: dict[str, float] = {}
        for name in self._all_sensor_names() | set(self.cfg.initial_state):
            values[str(name)] = 0.0
        for name in self.cfg.actuator_initial_state:
            values.setdefault(f"{name}F", 0.0)
        return values

    def _configured_initial_values(self) -> dict[str, float]:
        values = dict(self.cfg.initial_state)
        for name in self._all_sensor_names():
            values.setdefault(name, 0.0)
        for name in self.cfg.actuator_initial_state:
            values.setdefault(f"{name}F", 0.0)
        return {str(k): float(v) for k, v in values.items()}

    def dhalsim_zero_snapshot(self, iteration: int = 0) -> dict[str, Any]:
        zero_actuators = {name: False for name in self.cfg.actuator_initial_state}
        zero_status = self._actuator_status_map(zero_actuators)
        zero_flow = {name: 0.0 for name in self.cfg.actuator_initial_state}
        return {
            "iteration": iteration,
            "backend": "dhalsim_compatible_zero",
            "warning": self.warning,
            "hydraulic_timestep": self.cfg.hydraulic_timestep,
            "sim_time": 0,
            "sim_time_start": 0,
            "sim_time_end": 0,
            "values": self._zero_values_for_all_runtime_tags(),
            "actuators_applied": zero_actuators,
            "link_status": zero_status,
            "link_flow": zero_flow,
            "internal_hydraulic_steps": 0,
            "advanced": False,
            "initialization_stage": "dummy_zero",
        }

    def dhalsim_initial_snapshot(self, iteration: int = 1) -> dict[str, Any]:
        actuator_state = dict(self.actuator_state)
        return {
            "iteration": iteration,
            "backend": "dhalsim_compatible_initial",
            "warning": self.warning,
            "hydraulic_timestep": self.cfg.hydraulic_timestep,
            "sim_time": 0,
            "sim_time_start": 0,
            "sim_time_end": 0,
            "values": self._configured_initial_values(),
            "actuators_applied": actuator_state,
            "link_status": self._actuator_status_map(actuator_state),
            "link_flow": {name: 0.0 for name in actuator_state},
            "internal_hydraulic_steps": 0,
            "advanced": False,
            "initialization_stage": "configured_initial",
        }

    def current_snapshot(self, iteration: int | None = None) -> dict[str, Any]:
        if self.available and self.backend_kind == "dhalsim_epynet":
            self.state = self._capture_epynet_state()
            link_status, link_flow = self._capture_epynet_link_diagnostics()
            backend = "dhalsim_epynet_initial" if self.sim_time == 0 else "dhalsim_epynet_snapshot"
        elif self.available:
            self.state = self._capture_toolkit_state()
            link_status, link_flow = self._capture_toolkit_link_diagnostics()
            backend = "epanet_toolkit_initial" if self.sim_time == 0 else "epanet_toolkit_snapshot"
        else:
            state = dict(self.state)
            for key, value in self.actuator_state.items():
                state[f"{key}F"] = 1.0 if value else 0.0
            self.state = state
            link_status, link_flow = {}, {}
            backend = "mock_initial" if self.sim_time == 0 else "mock_snapshot"

        return {
            "iteration": self.iteration if iteration is None else iteration,
            "backend": backend,
            "warning": self.warning,
            "hydraulic_timestep": self.cfg.hydraulic_timestep,
            "sim_time": self.sim_time,
            "sim_time_start": self.sim_time,
            "sim_time_end": self.sim_time,
            "values": self.state,
            "actuators_applied": self.actuator_state,
            "link_status": link_status,
            "link_flow": link_flow,
            "internal_hydraulic_steps": 0,
            "advanced": False,
        }

    def step(self, actuator_state: dict[str, bool] | None = None, iteration: int | None = None) -> dict[str, Any]:
        if actuator_state is not None:
            self.actuator_state.update(actuator_state)
        if iteration is not None:
            self.iteration = iteration

        start_time = self.sim_time
        link_status: dict[str, float] = {}
        link_flow: dict[str, float] = {}

        if not (self.available and self.backend_kind == "dhalsim_epynet"):
            raise RuntimeError("DHALSIM-epynet is not available; no alternate physics backend is enabled")

        try:
            self.state = self._run_epynet_step(self.actuator_state)
            link_status, link_flow = self._capture_epynet_link_diagnostics()
            backend = "dhalsim_epynet"
        except Exception as exc:
            self.warning = f"DHALSIM-epynet step failed at iteration {self.iteration}: {exc}"
            raise RuntimeError(self.warning) from exc

        result_iteration = self.iteration
        result = {
            "iteration": result_iteration,
            "backend": backend,
            "warning": self.warning,
            "hydraulic_timestep": self.cfg.hydraulic_timestep,
            "sim_time": self.sim_time,
            "sim_time_start": start_time,
            "sim_time_end": self.sim_time,
            "values": self.state,
            "actuators_applied": dict(self.actuator_state),
            "link_status": link_status,
            "link_flow": link_flow,
            "internal_hydraulic_steps": self.last_internal_steps,
            "advanced": True,
            "step_order": self.epynet_step_order if backend == "dhalsim_epynet" else "",
            "inp_controls": self.epynet_inp_controls if backend == "dhalsim_epynet" else "",
            "last_tstep": self.last_tstep,
        }
        self.iteration = result_iteration + 1
        return result

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def _close_toolkit_silent(self) -> None:
        if self.epanet is None:
            return
        try:
            if self.toolkit_started:
                self.epanet.ENcloseH()
        except Exception:
            pass
        try:
            self.epanet.ENclose()
        except Exception:
            pass

    def _close_epynet_silent(self) -> None:
        """Best-effort cleanup for DHALSIM-epynet.

        Some epynet/EPANET shared-library builds can segfault inside ENcloseH or
        ENclose after a long hydraulic run.  Python exceptions cannot catch that
        failure because it happens in native code.  By default we therefore do
        not explicitly close the DHALSIM-epynet handle; the OS reclaims the
        process resources on exit.

        Set HYDROCPS_FORCE_EPYNET_CLOSE=1 only when debugging a build known to
        close safely.
        """
        if self.epynet is None:
            return
        if os.environ.get("HYDROCPS_FORCE_EPYNET_CLOSE", "0") != "1":
            self.epynet_started = False
            self.epynet = None
            return
        try:
            if self.epynet_started:
                self.epynet.ENcloseH()
        except Exception:
            pass
        for name in ("ENclose", "ENdeleteproject"):
            try:
                fn = getattr(self.epynet, name)
            except Exception:
                continue
            try:
                fn()
                break
            except Exception:
                pass

    def close(self) -> None:
        self._close_toolkit_silent()
        self._close_epynet_silent()
        self.toolkit_started = False
        self.epynet_started = False
        self.epanet = None
        self.epynet = None

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        try:
            self.close()
        except Exception:
            pass
