#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate Linux namespace/TAP bootstrap script from config.yaml."""
from __future__ import annotations

import argparse
import ipaddress
import stat
from pathlib import Path
from typing import Any

from src.core.config import load_yaml


def _resolve_output_dir(config_path: Path, cfg: dict[str, Any]) -> Path:
    raw = cfg.get("output_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("config.yaml missing valid top-level output_path")
    p = Path(raw).expanduser()
    if p.is_absolute():
        if p.exists():
            return p.resolve()
        local_output = (config_path.parent / "output").resolve()
        if local_output.exists():
            return local_output
        return p.resolve()
    return (config_path.parent / p).resolve()


def _safe_lower(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _endpoint_entries(cfg: dict[str, Any]) -> list[dict[str, str]]:
    endpoints = cfg.get("network", {}).get("nodes", {}).get("endpoints", [])
    if not isinstance(endpoints, list):
        raise ValueError("network.nodes.endpoints must be a list")

    by_name: dict[str, dict[str, Any]] = {}
    for ep in endpoints:
        if isinstance(ep, dict) and ep.get("name") and ep.get("namespace"):
            by_name[str(ep["name"])] = ep

    result: list[dict[str, str]] = []
    for lan in cfg.get("network", {}).get("lans", []) or []:
        if not isinstance(lan, dict):
            continue
        interfaces = lan.get("interfaces", {})
        if not isinstance(interfaces, dict):
            continue
        for ep_name, ep in by_name.items():
            if ep_name not in interfaces:
                continue
            iface = interfaces[ep_name]
            if not isinstance(iface, dict):
                continue
            ip_raw = str(iface.get("ip", ""))
            gw_raw = str(iface.get("gateway", ""))
            if not ip_raw or not gw_raw:
                continue
            try:
                host_ip = str(ipaddress.ip_interface(ip_raw))
            except Exception:
                host_ip = ip_raw
            result.append({
                "name": ep_name,
                "role": str(ep.get("role", "")),
                "namespace": str(ep["namespace"]),
                "tap": str(ep.get("tap", ep.get("tap_name", f"tap-{_safe_lower(ep_name)}"))),
                "host_ip": host_ip,
                "gateway": gw_raw,
            })

    if not result:
        raise ValueError("No endpoint LAN interface with ip/gateway found in config")

    def key(item: dict[str, str]) -> tuple[int, str]:
        if item.get("role") == "scada":
            return (0, item["name"])
        return (1, item["name"])

    return sorted(result, key=key)


def generate_network_sh(cfg: dict[str, Any]) -> str:
    entries = _endpoint_entries(cfg)
    ns_list = [e["namespace"] for e in entries]
    tap_list = [e["tap"] for e in entries]
    br_list = [f"br-{_safe_lower(e['name'])}" for e in entries]
    veth_roots = [f"veth-{_safe_lower(e['name'])}-root" for e in entries]

    lines: list[str] = []
    lines.append("#!/usr/bin/env bash")
    lines.append("set -euo pipefail")
    lines.append("")
    lines.append("NS_LIST=(" + " ".join(ns_list) + ")")
    lines.append("TAP_LIST=(" + " ".join(tap_list) + ")")
    lines.append("BR_LIST=(" + " ".join(br_list) + ")")
    lines.append("")
    lines.append('echo "[*] Cleaning namespaces..."')
    lines.append('for ns in "${NS_LIST[@]}"; do')
    lines.append('    sudo ip netns delete "$ns" 2>/dev/null || true')
    lines.append("done")
    lines.append("")
    lines.append('echo "[*] Cleaning TAP / bridge / veth..."')
    lines.append('for tap in "${TAP_LIST[@]}"; do')
    lines.append('    sudo ip link set "$tap" down 2>/dev/null || true')
    lines.append('    sudo ip link delete "$tap" 2>/dev/null || true')
    lines.append("done")
    lines.append("")
    lines.append('for br in "${BR_LIST[@]}"; do')
    lines.append('    sudo ip link set "$br" down 2>/dev/null || true')
    lines.append('    sudo ip link delete "$br" type bridge 2>/dev/null || true')
    lines.append("done")
    lines.append("")
    lines.append("for dev in \\")
    for idx, dev in enumerate(veth_roots):
        suffix = " \\" if idx < len(veth_roots) - 1 else "; do"
        lines.append(f"    {dev}{suffix}")
    lines.append('    sudo ip link delete "$dev" 2>/dev/null || true')
    lines.append("done")
    lines.append("")
    lines.append('echo "[*] Creating namespaces..."')
    lines.append('for ns in "${NS_LIST[@]}"; do')
    lines.append('    sudo ip netns add "$ns"')
    lines.append('    sudo ip netns exec "$ns" ip link set lo up')
    lines.append("done")
    lines.append("")
    lines.append(r'''create_segment() {
    local name="$1"
    local ns="$2"
    local tap="$3"
    local br="$4"
    local veth_root="$5"
    local veth_ns="$6"
    local host_ip="$7"
    local gw_ip="$8"

    echo "[*] Creating segment: $name"

    # ns-3's ./ns3 launcher must run as the normal user, not as root.
    # Therefore the TAP is owned by the invoking user so TapBridge can open it.
    local tap_owner="${HYDRO_TAP_USER:-${SUDO_USER:-${USER}}}"
    sudo ip tuntap add dev "$tap" mode tap user "$tap_owner"
    sudo ip link set "$tap" up

    sudo ip link add name "$br" type bridge
    sudo ip link set "$br" up

    sudo ip link set "$tap" master "$br"

    sudo ip link add "$veth_root" type veth peer name "$veth_ns"
    sudo ip link set "$veth_root" up
    sudo ip link set "$veth_root" master "$br"

    sudo ip link set "$veth_ns" netns "$ns"
    sudo ip netns exec "$ns" ip link set "$veth_ns" up
    sudo ip netns exec "$ns" ip addr add "$host_ip" dev "$veth_ns"
    sudo ip netns exec "$ns" ip route add default via "$gw_ip" dev "$veth_ns"

    sudo sysctl -w "net.ipv4.conf.${tap}.rp_filter=0" >/dev/null || true
    sudo sysctl -w "net.ipv4.conf.${veth_root}.rp_filter=0" >/dev/null || true
    sudo sysctl -w "net.ipv4.conf.${br}.rp_filter=0" >/dev/null || true
    sudo sysctl -w "net.ipv4.conf.${tap}.accept_local=1" >/dev/null || true
    sudo sysctl -w "net.ipv4.conf.${veth_root}.accept_local=1" >/dev/null || true

    sudo ip netns exec "$ns" sysctl -w "net.ipv4.conf.all.rp_filter=0" >/dev/null || true
    sudo ip netns exec "$ns" sysctl -w "net.ipv4.conf.${veth_ns}.rp_filter=0" >/dev/null || true
    sudo ip netns exec "$ns" sysctl -w "net.ipv4.conf.${veth_ns}.accept_local=1" >/dev/null || true
}
''')

    for e in entries:
        lower = _safe_lower(e["name"])
        lines.append(
            f'create_segment "{lower}" "{e["namespace"]}" "{e["tap"]}" "br-{lower}" \\\n'
            f'    "veth-{lower}-root" "veth-{lower}-ns" "{e["host_ip"]}" "{e["gateway"]}"'
        )
        lines.append("")

    lines.append("sudo sysctl -w net.ipv4.conf.all.rp_filter=0 >/dev/null || true")
    lines.append("")
    lines.append('echo')
    lines.append('echo "✅ Network namespaces + bridges + TAPs configured."')
    for e in entries:
        lines.append(f'echo "   {e["namespace"]:10s}: {e["host_ip"]:<18s} via {e["gateway"]}"')
    lines.append('echo')
    lines.append('echo "TAPs left in root namespace for ns-3:"')
    lines.append('for tap in "${TAP_LIST[@]}"; do')
    lines.append('    echo "   $tap"')
    lines.append('done')
    return "\n".join(lines) + "\n"


def generate(config_path: Path) -> Path:
    cfg = load_yaml(config_path)
    output_dir = _resolve_output_dir(config_path, cfg)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "network.sh"
    path.write_text(generate_network_sh(cfg), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate namespace/TAP network.sh from config.yaml")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    path = generate(args.config.resolve())
    print(f"[NETWORK-SH] generated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
