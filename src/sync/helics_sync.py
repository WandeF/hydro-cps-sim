#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HELICS message based synchronization helpers for Hydro-CPS-Sim.

This module deliberately keeps the public surface small.  The runtime still
persists JSON/CSV files for auditability, while cycle release/ack signals can be
carried by HELICS messages instead of filesystem marker polling.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable


class HelicsUnavailableError(RuntimeError):
    pass


class HelicsSyncTimeoutError(TimeoutError):
    pass


def endpoint_name(role: str, name: str | None = None, prefix: str = "hydro") -> str:
    role = role.strip("/")
    if name:
        return f"{prefix}/{role}/{str(name).strip('/').lower()}"
    return f"{prefix}/{role}"


def coordinator_endpoint(prefix: str = "hydro") -> str:
    return endpoint_name("coordinator", prefix=prefix)


def scada_endpoint(prefix: str = "hydro") -> str:
    return endpoint_name("scada", prefix=prefix)


def plc_endpoint(plc_lower: str, prefix: str = "hydro") -> str:
    return endpoint_name("plc", plc_lower, prefix=prefix)


@dataclass
class HelicsSync:
    """Small wrapper around a HELICS message federate."""

    federate_name: str
    endpoint: str
    core_type: str = "ipc"
    core_init: str = ""
    broker_address: str = ""
    time_delta: float = 0.001
    log_level: int = 1
    timeout: float = 30.0
    prefix: str = "hydro"
    _h: Any = field(default=None, init=False, repr=False)
    _fed: Any = field(default=None, init=False, repr=False)
    _endpoint_id: Any = field(default=None, init=False, repr=False)
    _time: float = field(default=0.0, init=False)
    _buffer: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    @classmethod
    def from_args(cls, federate_name: str, endpoint: str, args: Any, *, timeout: float | None = None) -> "HelicsSync":
        return cls(
            federate_name=federate_name,
            endpoint=endpoint,
            core_type=str(getattr(args, "helics_core_type", "ipc") or "ipc"),
            core_init=str(getattr(args, "helics_core_init", "") or ""),
            broker_address=str(getattr(args, "helics_broker_address", "") or ""),
            time_delta=float(getattr(args, "helics_time_delta", 0.001) or 0.001),
            log_level=int(getattr(args, "helics_log_level", 1) or 1),
            timeout=float(timeout if timeout is not None else getattr(args, "sync_timeout", 30.0)),
            prefix=str(getattr(args, "helics_prefix", "hydro") or "hydro"),
        )

    def start(self) -> "HelicsSync":
        try:
            import helics as h  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on user env
            raise HelicsUnavailableError(
                "PyHELICS is not available. Install it in the Python environment "
                "used inside the namespaces, e.g. `python -m pip install helics`."
            ) from exc

        self._h = h
        fedinfo = h.helicsCreateFederateInfo()
        h.helicsFederateInfoSetCoreTypeFromString(fedinfo, self.core_type)

        core_init_parts: list[str] = []
        if self.core_init:
            core_init_parts.append(self.core_init)
        else:
            # One federate per core; an external broker can be supplied through
            # --helics-broker-address.  When using an auto-broker capable HELICS
            # installation, users may pass --helics-core-init "--autobroker ...".
            core_init_parts.append("--federates=1")
        if self.broker_address:
            core_init_parts.append(f"--broker_address={self.broker_address}")
        core_init = " ".join(core_init_parts)
        h.helicsFederateInfoSetCoreInitString(fedinfo, core_init)

        try:
            h.helicsFederateInfoSetIntegerProperty(fedinfo, h.HELICS_PROPERTY_INT_LOG_LEVEL, self.log_level)
        except Exception:
            pass
        try:
            h.helicsFederateInfoSetTimeProperty(fedinfo, h.HELICS_PROPERTY_TIME_DELTA, self.time_delta)
        except Exception:
            pass
        try:
            h.helicsFederateInfoSetFlagOption(fedinfo, h.HELICS_FLAG_UNINTERRUPTIBLE, False)
        except Exception:
            pass
        try:
            h.helicsFederateInfoSetFlagOption(fedinfo, h.HELICS_FLAG_TERMINATE_ON_ERROR, True)
        except Exception:
            pass

        if hasattr(h, "helicsCreateMessageFederate"):
            fed = h.helicsCreateMessageFederate(self.federate_name, fedinfo)
        else:
            fed = h.helicsCreateCombinationFederate(self.federate_name, fedinfo)
        self._fed = fed

        if hasattr(h, "helicsFederateRegisterGlobalEndpoint"):
            self._endpoint_id = h.helicsFederateRegisterGlobalEndpoint(fed, self.endpoint, "")
        else:
            self._endpoint_id = h.helicsFederateRegisterEndpoint(fed, self.endpoint, "")

        h.helicsFederateEnterExecutingMode(fed)
        return self

    def close(self) -> None:
        if self._h is None or self._fed is None:
            return
        try:
            self._h.helicsFederateFinalize(self._fed)
        except Exception:
            pass
        try:
            self._h.helicsFederateFree(self._fed)
        except Exception:
            pass
        self._fed = None

    def _request_next_time(self) -> None:
        h = self._h
        if h is None or self._fed is None:
            raise RuntimeError("HELICS federate is not started")
        next_time = self._time + max(self.time_delta, 1e-6)
        granted = h.helicsFederateRequestTime(self._fed, next_time)
        try:
            self._time = float(granted)
        except Exception:
            self._time = next_time

    def _drain(self) -> None:
        h = self._h
        if h is None or self._endpoint_id is None:
            raise RuntimeError("HELICS federate is not started")
        while h.helicsEndpointHasMessage(self._endpoint_id):
            msg = h.helicsEndpointGetMessage(self._endpoint_id)
            try:
                text = h.helicsMessageGetString(msg)
            except Exception:
                data = h.helicsMessageGetBytes(msg)
                text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
            try:
                payload = json.loads(text)
            except Exception:
                payload = {"kind": "raw", "raw": text}
            try:
                payload.setdefault("source", h.helicsMessageGetOriginalSource(msg))
            except Exception:
                pass
            self._buffer.append(payload)

    def send(self, destination: str, kind: str, iteration: int | None = None, payload: dict[str, Any] | None = None) -> None:
        h = self._h
        if h is None or self._endpoint_id is None:
            raise RuntimeError("HELICS federate is not started")
        data = dict(payload or {})
        data["kind"] = kind
        if iteration is not None:
            data["iteration"] = int(iteration)
        data.setdefault("sender", self.federate_name)
        data.setdefault("wall_time", time.time())
        text = json.dumps(data, ensure_ascii=False, sort_keys=True)
        # PyHELICS versions have differed in endpoint send argument order.  Try
        # the documented data,destination order first, then the older examples'
        # destination,data order.
        try:
            h.helicsEndpointSendBytesTo(self._endpoint_id, text.encode("utf-8"), destination)
        except TypeError:
            h.helicsEndpointSendBytesTo(self._endpoint_id, destination, text.encode("utf-8"))
        except Exception:
            try:
                h.helicsEndpointSendStringTo(self._endpoint_id, text, destination)
            except TypeError:
                h.helicsEndpointSendStringTo(self._endpoint_id, destination, text)

    def wait_for(
        self,
        kinds: str | Iterable[str],
        *,
        iteration: int | None = None,
        count: int = 1,
        timeout: float | None = None,
        stop_kinds: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        expected = {kinds} if isinstance(kinds, str) else set(kinds)
        stop_kinds = set(stop_kinds or {"stop"})
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        matched: list[dict[str, Any]] = []

        while len(matched) < count:
            self._drain()
            remaining: list[dict[str, Any]] = []
            for msg in self._buffer:
                kind = str(msg.get("kind", ""))
                msg_iter = msg.get("iteration")
                if kind in stop_kinds:
                    raise HelicsSyncTimeoutError(f"HELICS stop message received by {self.federate_name}: {msg}")
                is_match = kind in expected and (iteration is None or int(msg_iter) == int(iteration))
                if is_match and len(matched) < count:
                    matched.append(msg)
                else:
                    remaining.append(msg)
            self._buffer = remaining
            if len(matched) >= count:
                return matched
            if time.monotonic() > deadline:
                raise HelicsSyncTimeoutError(
                    f"timeout waiting for HELICS messages kinds={sorted(expected)} iteration={iteration} "
                    f"count={count} received={len(matched)} federate={self.federate_name}"
                )
            self._request_next_time()
        return matched

    def flush_time(self) -> None:
        """Advance HELICS time once to help release already-sent messages."""
        self._request_next_time()
