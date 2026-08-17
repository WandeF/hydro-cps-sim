#!/usr/bin/env python3
"""Generate isolated, reproducible experiment configurations."""
from __future__ import annotations

import math
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import yaml

from src.core.config import load_yaml


SUPPORTED_PARAMETERS = (
    "delay_ms",
    "loss_rate",
    "data_rate_mbps",
    "queue_packets",
    "iterations",
)
NETWORK_PARAMETERS = frozenset(SUPPORTED_PARAMETERS[:-1])

_PARAMETER_GROUPS = {
    "delay_ms": "network_delay",
    "loss_rate": "network_loss",
    "data_rate_mbps": "network_data_rate",
    "queue_packets": "network_queue",
    "iterations": "simulation_iterations",
}


def set_named_link(config: dict[str, Any], link_name: str, **updates: Any) -> None:
    """Update one named ns-3 backbone link in ``config``."""
    network = config.get("network", {}) or {}
    if not isinstance(network, dict):
        raise ValueError("network must be a mapping")
    links = network.get("backbone_links", []) or []
    if not isinstance(links, list):
        raise ValueError("network.backbone_links must be a list")
    for link in links:
        if isinstance(link, dict) and str(link.get("name")) == link_name:
            link.update(updates)
            return
    raise KeyError(f"Unknown backbone link: {link_name}")


def _enable_metrics(config: dict[str, Any]) -> None:
    metrics = config.setdefault("metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be a mapping")
    metrics.update(
        {
            "enabled": True,
            "event_log": True,
            "communication": True,
            "resource_monitor": True,
        }
    )

    network = config.setdefault("network", {})
    if not isinstance(network, dict):
        raise ValueError("network must be a mapping")
    measurement = network.setdefault("measurement", {})
    if not isinstance(measurement, dict):
        raise ValueError("network.measurement must be a mapping")
    measurement.update(
        {
            "enabled": True,
            "flow_monitor": True,
            "link_metrics": True,
            "pcap": bool(network.get("pcap", True)),
        }
    )


def _disable_attacks(config: dict[str, Any]) -> None:
    attacks = config.get("attacks")
    if attacks is None:
        attacks = {}
        config["attacks"] = attacks
    if not isinstance(attacks, dict):
        raise ValueError("attacks must be a mapping")
    attacks["enabled"] = False


def _finite_float(raw: Any, parameter: str) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"{parameter} must be numeric")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{parameter} must be numeric: {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{parameter} must be finite")
    return value


def _positive_integer(raw: Any, parameter: str) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{parameter} must be a positive integer")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{parameter} must be a positive integer: {raw!r}") from exc
    if not value.is_finite() or value != value.to_integral_value() or value <= 0:
        raise ValueError(f"{parameter} must be a positive integer")
    return int(value)


def normalize_parameter_value(parameter: str, raw: Any) -> float | int:
    """Validate and normalize one supported matrix value."""
    if parameter not in SUPPORTED_PARAMETERS:
        supported = ", ".join(SUPPORTED_PARAMETERS)
        raise ValueError(f"Unsupported experiment parameter {parameter!r}; choose one of: {supported}")

    if parameter in {"queue_packets", "iterations"}:
        return _positive_integer(raw, parameter)

    value = _finite_float(raw, parameter)
    if parameter == "delay_ms" and value < 0:
        raise ValueError("delay_ms must be >= 0")
    if parameter == "loss_rate" and not 0 <= value <= 1:
        raise ValueError("loss_rate must be between 0 and 1")
    if parameter == "data_rate_mbps" and value <= 0:
        raise ValueError("data_rate_mbps must be > 0")
    return value


def _number_text(value: float | int) -> str:
    if isinstance(value, int) or value.is_integer():
        return str(int(value))
    # ns3_generation.time_value_expr expects ordinary decimal notation, not an
    # exponent. Decimal(str(...)) preserves the human-facing shortest form, so
    # values such as 0.1 do not become 0.10000000000000001 in YAML or IDs.
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _filename_label(value: float | int) -> str:
    return _number_text(value).replace("-", "m").replace(".", "p")


def _identity(parameter: str, value: float | int) -> tuple[str, str]:
    group = _PARAMETER_GROUPS[parameter]
    label = _filename_label(value)
    suffix = {
        "delay_ms": "ms",
        "loss_rate": "",
        "data_rate_mbps": "mbps",
        "queue_packets": "packets",
        "iterations": "iterations",
    }[parameter]
    return group, f"{group}_{label}{suffix}"


def _target_link_names(base: dict[str, Any], link_names: Iterable[str]) -> list[str]:
    names: list[str] = []
    for raw_name in link_names:
        name = str(raw_name).strip()
        if not name:
            raise ValueError("target link names must not be empty")
        if name not in names:
            names.append(name)

    network = base.get("network", {}) or {}
    if not isinstance(network, dict):
        raise ValueError("network must be a mapping")
    links = network.get("backbone_links", []) or []
    if not isinstance(links, list):
        raise ValueError("network.backbone_links must be a list")
    available = {
        str(link.get("name"))
        for link in links
        if isinstance(link, dict) and link.get("name") is not None
    }
    unknown = [name for name in names if name not in available]
    if unknown:
        raise KeyError(f"Unknown backbone link(s): {', '.join(unknown)}")
    return names


def _apply_parameter(
    config: dict[str, Any],
    parameter: str,
    value: float | int,
    link_names: list[str],
) -> None:
    if parameter == "iterations":
        config["iterations"] = int(value)
        return

    for link_name in link_names:
        if parameter == "delay_ms":
            set_named_link(config, link_name, delay=f"{_number_text(value)}ms")
        elif parameter == "data_rate_mbps":
            set_named_link(config, link_name, data_rate=f"{_number_text(value)}Mbps")
        elif parameter == "loss_rate":
            network = config["network"]
            links = network["backbone_links"]
            link = next(item for item in links if item.get("name") == link_name)
            error_model = link.get("error_model")
            if not isinstance(error_model, dict):
                error_model = {}
                link["error_model"] = error_model
            error_model.update(
                {
                    "type": "rate",
                    "unit": "packet",
                    "error_rate": float(value),
                }
            )
        elif parameter == "queue_packets":
            network = config["network"]
            links = network["backbone_links"]
            link = next(item for item in links if item.get("name") == link_name)
            queue = link.get("queue")
            if not isinstance(queue, dict):
                queue = {}
                link["queue"] = queue
            queue.setdefault("type", "DropTailQueue")
            queue["max_packets"] = int(value)


def generate_parameter_configs(
    base_config: Path,
    output_dir: Path,
    *,
    parameter: str,
    values: Iterable[Any],
    repetitions: int,
    results_root: Path,
    link_names: Iterable[str] = (),
    seed_base: int = 1000,
) -> list[Path]:
    """Generate a one-factor experiment matrix.

    Network factors are applied to every link named by ``link_names``. The
    ``iterations`` factor updates the top-level simulation iteration count and
    therefore does not accept target links.
    """
    parameter = str(parameter).strip()
    if parameter not in SUPPORTED_PARAMETERS:
        # Reuse the public validator's detailed unsupported-parameter message.
        normalize_parameter_value(parameter, 0)
    repetitions = _positive_integer(repetitions, "repetitions")
    seed_base = _positive_integer(seed_base, "seed_base")

    normalized_values = [normalize_parameter_value(parameter, raw) for raw in values]
    if not normalized_values:
        raise ValueError("At least one matrix value is required")
    if len(set(normalized_values)) != len(normalized_values):
        raise ValueError(f"Duplicate {parameter} values are not allowed")

    base_config = base_config.resolve()
    output_dir = output_dir.resolve()
    results_root = results_root.resolve()
    base = load_yaml(base_config)
    names = _target_link_names(base, link_names)
    if parameter in NETWORK_PARAMETERS and not names:
        raise ValueError(f"At least one target link is required for {parameter}")
    if parameter == "iterations" and names:
        raise ValueError("target links are not valid for the iterations parameter")

    total_configs = len(normalized_values) * repetitions
    if seed_base + total_configs > 2**32 - 1:
        raise ValueError("generated random_seed values exceed the ns-3 uint32 range")

    # Validate optional sections before creating output directories, so an
    # invalid base file never leaves behind a partial matrix.
    validation_config = deepcopy(base)
    _enable_metrics(validation_config)
    _disable_attacks(validation_config)

    output_dir.mkdir(parents=True, exist_ok=True)
    results_root.mkdir(parents=True, exist_ok=True)

    generated: list[Path] = []
    sequence = 0
    for value in normalized_values:
        group, identity = _identity(parameter, value)
        for repetition in range(1, repetitions + 1):
            sequence += 1
            config = deepcopy(base)
            _apply_parameter(config, parameter, value, names)
            experiment_id = f"{identity}_run_{repetition:02d}"
            result_dir = results_root / experiment_id
            config["output_path"] = str(result_dir / "output")
            config["experiment"] = {
                "id": experiment_id,
                "name": experiment_id,
                "group": group,
                "parameter": parameter,
                "value": value,
                "target_links": names,
                "repetition": repetition,
                "random_seed": seed_base + sequence,
                "base_config": str(base_config),
            }
            _enable_metrics(config)
            _disable_attacks(config)
            path = output_dir / f"{experiment_id}.yaml"
            with path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
            generated.append(path)
    return generated


def generate_delay_configs(
    base_config: Path,
    output_dir: Path,
    *,
    link_names: Iterable[str],
    delays_ms: Iterable[float],
    repetitions: int,
    results_root: Path,
    seed_base: int = 1000,
) -> list[Path]:
    """Backward-compatible wrapper for the original delay-only matrix API."""
    return generate_parameter_configs(
        base_config,
        output_dir,
        parameter="delay_ms",
        values=delays_ms,
        repetitions=repetitions,
        results_root=results_root,
        link_names=link_names,
        seed_base=seed_base,
    )
