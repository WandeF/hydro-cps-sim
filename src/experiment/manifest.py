#!/usr/bin/env python3
"""Create reproducibility metadata for one Hydro-CPS-Sim experiment."""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from src.core.config import load_yaml, write_json


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_output_dir(config_path: Path, cfg: dict[str, Any]) -> Path:
    raw = cfg.get("output_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("config file missing valid top-level output_path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def _git_info(path: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return (result.stdout or "").strip() if result.returncode == 0 else ""

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {
        "commit": commit,
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status),
    }


def _optional_repo_commit(path_value: Any) -> str:
    if not isinstance(path_value, str) or not path_value.strip():
        return ""
    path = Path(path_value).expanduser()
    if not path.exists():
        return ""
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def _hydraulic_timestep(cfg: dict[str, Any]) -> int:
    for item in cfg.get("time", []) or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("hydraulic_timestep")
        if isinstance(raw, list) and len(raw) >= 2:
            try:
                return int(raw[1])
            except (TypeError, ValueError):
                pass
    return 300


def _memory_gb() -> float | None:
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        return pages * page_size / (1024.0 ** 3)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _network_impairments(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe every directional ns-3 receive error model explicitly."""
    network = cfg.get("network", {}) or {}
    if not isinstance(network, dict):
        return []
    result: list[dict[str, Any]] = []
    for link in network.get("backbone_links", []) or []:
        if not isinstance(link, dict) or not isinstance(link.get("error_model"), dict):
            continue
        endpoints = link.get("endpoints", [])
        if not isinstance(endpoints, list) or len(endpoints) != 2:
            continue
        a, b = str(endpoints[0]), str(endpoints[1])
        error = link["error_model"]
        raw_direction = str(error.get("direction", "both")).strip().lower().replace("_", "-")
        directions = {
            "both": [(a, b, 0), (b, a, 1)],
            "a-to-b": [(a, b, 0)],
            "b-to-a": [(b, a, 0)],
            f"{a.lower()}-to-{b.lower()}": [(a, b, 0)],
            f"{b.lower()}-to-{a.lower()}": [(b, a, 0)],
        }.get(raw_direction, [])
        stream_base = int(error.get("stream", 1) or 0)
        for source, receive, offset in directions:
            result.append(
                {
                    "link_name": str(link.get("name", "")),
                    "direction": f"{source}-to-{receive}",
                    "source_device": source,
                    "receive_device": receive,
                    "error_model_type": str(error.get("type", "rate")),
                    "error_unit": str(error.get("unit", "packet")),
                    "configured_error_rate": float(error.get("error_rate", 0.0) or 0.0),
                    "random_stream": stream_base + offset,
                }
            )
    return result


def build_manifest(
    config_path: Path,
    *,
    experiment_id: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    config_path = config_path.resolve()
    cfg = load_yaml(config_path)
    project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    experiment = cfg.get("experiment", {}) or {}
    if not isinstance(experiment, dict):
        experiment = {}
    resolved_id = experiment_id or str(
        experiment.get("id") or experiment.get("name") or config_path.stem
    )
    random_seed = experiment.get("random_seed", cfg.get("random_seed", ""))
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    output_dir = resolve_output_dir(config_path, cfg)

    ns3_run = int(experiment.get("ns3_run", experiment.get("repetition", 1)) or 1)
    drain_period_sec = float(experiment.get("drain_period_sec", 0.0) or 0.0)

    return {
        "schema_version": 2,
        "experiment_id": resolved_id,
        "group": experiment.get("group", ""),
        "parameter": experiment.get("parameter", ""),
        "parameter_value": experiment.get("value", ""),
        "repetition": experiment.get("repetition", ""),
        "timestamp": timestamp,
        "git": _git_info(project_root),
        "config_file": str(config_path),
        "config_sha256": sha256_file(config_path),
        "output_dir": str(output_dir),
        "random_seed": random_seed,
        "ns3_seed": random_seed,
        "ns3_run": ns3_run,
        "error_models": _network_impairments(cfg),
        "drain_period_sec": drain_period_sec,
        "iterations": int(cfg.get("iterations", 1) or 1),
        "hydraulic_step_sec": _hydraulic_timestep(cfg),
        "host": {
            "hostname": socket.gethostname(),
            "os": platform.platform(),
            "machine": platform.machine(),
            "cpu": platform.processor(),
            "cpu_count": os.cpu_count(),
            "memory_gb": _memory_gb(),
        },
        "software": {
            "python": sys.version.replace("\n", " "),
            "python_executable": sys.executable,
            "ns3_commit": _optional_repo_commit(cfg.get("ns3_path")),
            "openplc_commit": _optional_repo_commit(cfg.get("openplc_path")),
            "epanet_backend": str((cfg.get("physics", {}) or {}).get("backend", "dhalsim_epynet")),
        },
    }


def write_manifest(
    config_path: Path,
    runtime_dir: Path | None = None,
    *,
    experiment_id: str | None = None,
    project_root: Path | None = None,
) -> tuple[Path, Path]:
    config_path = config_path.resolve()
    cfg = load_yaml(config_path)
    output_dir = resolve_output_dir(config_path, cfg)
    runtime_dir = (runtime_dir or (output_dir / "runtime")).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = runtime_dir / "manifest.json"
    resolved_config_path = runtime_dir / "config_resolved.yaml"
    write_json(
        manifest_path,
        build_manifest(
            config_path,
            experiment_id=experiment_id,
            project_root=project_root,
        ),
    )
    with resolved_config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(cfg, handle, allow_unicode=True, sort_keys=False)
    try:
        shutil.copystat(config_path, resolved_config_path)
    except OSError:
        pass
    return manifest_path, resolved_config_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write reproducibility metadata for one experiment")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--experiment-id")
    parser.add_argument("--project-root", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest_path, resolved_config_path = write_manifest(
        args.config.expanduser().resolve(),
        args.runtime_dir.expanduser().resolve() if args.runtime_dir else None,
        experiment_id=args.experiment_id,
        project_root=args.project_root.expanduser().resolve() if args.project_root else None,
    )
    print(f"[MANIFEST] {manifest_path}")
    print(f"[CONFIG-RESOLVED] {resolved_config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
