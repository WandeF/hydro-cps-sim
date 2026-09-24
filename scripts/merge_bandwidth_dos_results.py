#!/usr/bin/env python3
"""Merge historical and supplemental bandwidth-DoS observations.

The output keeps every historical observation for auditability, while also
emitting a strict regular-grid table.  Historical rho=0.8 and 1.2 are
preserved as off-grid legacy observations because they do not lie on the
requested 0.25 grid.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from scripts.analyze_todo2_experiments import BASELINE_100, enrich_run, read_csv, number


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OLD_CSV = Path("/home/lzh/MASTER/CODE/output/quantitative_todo2_20260716T160500+0800_metric_cde39ea/results/bandwidth_dos_per_run.csv")
BANDWIDTHS = (5, 10, 20)
GRID_RHOS = (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
OFF_GRID_RHOS = (0.8, 1.2)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in fields})


def rho_label(rho: float) -> str:
    return str(rho).replace(".", "p")


def load_new_rows(supplement_archive: Path, old_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    index_path = supplement_archive / "RUN_INDEX.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    old_by_bandwidth = {int(float(row["bandwidth_mbps"])): Path(row["output_path"]) for row in old_rows if float(row["rho"]) == 0.0}
    rows: list[dict[str, Any]] = []
    for item in index:
        if not item.get("valid"):
            continue
        config = json.loads((supplement_archive / "EXPERIMENT_PLAN.json").read_text(encoding="utf-8"))
        del config
        experiment_id = str(item["id"])
        parts = experiment_id.split("_")
        bandwidth = int(parts[2].removesuffix("mbps"))
        rho = float(parts[4].replace("p", "."))
        output = Path(item["output"])
        baseline = old_by_bandwidth[bandwidth]
        # The historical archive retains the summary CSV but not the raw
        # physics.csv files needed by the old correctness helper.  For the
        # supplemental run, use its own raw output only as a structural
        # reference; network/communication measurements remain independent.
        correctness_reference = "historical_bandwidth_rho0"
        if not (baseline / "runtime/csv/physics.csv").is_file() and not (baseline / "physics.csv").is_file():
            baseline = output
            correctness_reference = "self_reference_raw_archive_unavailable"
        metrics = enrich_run(output, baseline, {"r0-r_scada", "r0-r2"}, "r0-r2")
        rows.append({
            "experiment_id": experiment_id,
            "bandwidth_mbps": bandwidth,
            "rho": rho,
            "configured_dos_rate_mbps": bandwidth * rho,
            "output_path": str(output),
            "source_type": "supplemental_run",
            "correctness_reference": correctness_reference,
            **metrics,
        })
    return rows


def plot_grid(path: Path, rows: list[dict[str, Any]], field: str, title: str, ylabel: str) -> None:
    fig, ax = plt.subplots(figsize=(8.5, 4.8), constrained_layout=True)
    colors = {5: "#1967D2", 10: "#E37400", 20: "#188038"}
    markers = {5: "o", 10: "s", 20: "^"}
    for bandwidth in BANDWIDTHS:
        series = sorted((r for r in rows if int(float(r["bandwidth_mbps"])) == bandwidth), key=lambda r: number(r["rho"]))
        ax.plot(
            [number(r["rho"]) for r in series],
            [number(r.get(field), math.nan) for r in series],
            marker=markers[bandwidth], color=colors[bandwidth], linewidth=2, markersize=5,
            label=f"{bandwidth} Mbps",
        )
    ax.set_title(title, loc="left", fontsize=12, pad=12)
    ax.set_xlabel("DoS intensity (rho)")
    ax.set_ylabel(ylabel)
    ax.set_xticks(GRID_RHOS)
    ax.grid(axis="y", color="#DADCE0", linewidth=0.8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, ncol=3)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=180, facecolor="white")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--supplement-archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    supplement = args.supplement_archive.expanduser().resolve()
    report = args.output.expanduser().resolve()
    data = report / "data"
    figures = report / "figures"

    old_rows = read_csv(OLD_CSV)
    if len(old_rows) != 15:
        raise RuntimeError(f"expected 15 historical bandwidth rows, found {len(old_rows)}")
    old_rows = [{**row, "source_type": "historical_todo2"} for row in old_rows]
    new_rows = load_new_rows(supplement, old_rows)
    expected_new = len(BANDWIDTHS) * 6
    if len(new_rows) != expected_new:
        raise RuntimeError(f"expected {expected_new} valid supplemental rows, found {len(new_rows)}")

    all_rows = sorted(old_rows + new_rows, key=lambda r: (int(float(r["bandwidth_mbps"])), float(r["rho"])))
    grid_rows = [
        row for row in all_rows
        if any(abs(float(row["bandwidth_mbps"]) - b) < 1e-9 and abs(float(row["rho"]) - rho) < 1e-9 for b in BANDWIDTHS for rho in GRID_RHOS)
    ]
    off_grid_rows = [row for row in all_rows if abs(float(row["rho"]) - 0.8) < 1e-9 or abs(float(row["rho"]) - 1.2) < 1e-9]
    if len(grid_rows) != len(BANDWIDTHS) * len(GRID_RHOS):
        raise RuntimeError(f"regular grid is incomplete: {len(grid_rows)} rows")

    quality_rows = []
    for row in all_rows:
        quality_rows.append({
            "experiment_id": row["experiment_id"],
            "bandwidth_mbps": row["bandwidth_mbps"],
            "rho": row["rho"],
            "source_type": row["source_type"],
            "correctness_reference": row.get("correctness_reference", "historical_summary_preserved"),
            "simulation_end": row.get("simulation_end"),
            "metrics_writer_status": row.get("metrics_writer_status"),
            "network_conservation_ok": row.get("network_conservation_ok"),
            "modbus_conservation_ok": row.get("modbus_conservation_ok"),
            "quality_pass": row.get("quality_pass"),
            "attack_enabled": row.get("attack_enabled"),
            "attack_window_triggered": row.get("attack_window_triggered"),
            "actual_attack_event_count": row.get("actual_attack_event_count"),
        })

    write_csv(data / "bandwidth_dos_all_observations.csv", all_rows)
    write_csv(data / "bandwidth_dos_grid.csv", grid_rows)
    write_csv(data / "bandwidth_dos_off_grid_legacy.csv", off_grid_rows)
    write_csv(data / "bandwidth_dos_quality.csv", quality_rows)
    for field, title, ylabel in (
        ("queue_drop_packets", "Bandwidth DoS intensity vs queue drops", "Queue drops (packets)"),
        ("tcp_retransmission_rate", "Bandwidth DoS intensity vs TCP retransmission rate", "TCP retransmission rate"),
        ("modbus_rtt_p95_ms", "Bandwidth DoS intensity vs Modbus RTT P95", "RTT P95 (ms)"),
        ("modbus_timeout_rate", "Bandwidth DoS intensity vs Modbus timeout rate", "Modbus timeout rate"),
        ("maximum_data_age_ms", "Bandwidth DoS intensity vs maximum data age", "Maximum data age (ms)"),
    ):
        plot_grid(figures / f"rho_vs_{field}.png", grid_rows, field, title, ylabel)

    onset = {}
    for bandwidth in BANDWIDTHS:
        series = sorted((r for r in grid_rows if int(float(r["bandwidth_mbps"])) == bandwidth), key=lambda r: float(r["rho"]))
        onset[bandwidth] = {
            "first_tcp_retransmission_rho": next((float(r["rho"]) for r in series if number(r.get("tcp_retransmissions")) > 0), None),
            "first_modbus_timeout_rho": next((float(r["rho"]) for r in series if number(r.get("modbus_timeout_rate")) > 0), None),
            "first_queue_drop_rho": next((float(r["rho"]) for r in series if number(r.get("queue_drop_packets")) > 0), None),
        }
    manifest = {
        "schema_version": 1,
        "historical_source": str(OLD_CSV),
        "supplement_source": str(supplement),
        "bandwidth_mbps": list(BANDWIDTHS),
        "regular_grid_rho": list(GRID_RHOS),
        "historical_off_grid_rho": list(OFF_GRID_RHOS),
        "historical_row_count": len(old_rows),
        "supplement_row_count": len(new_rows),
        "all_observation_count": len(all_rows),
        "regular_grid_count": len(grid_rows),
        "off_grid_legacy_count": len(off_grid_rows),
        "onset_observations": onset,
        "interpretation": "single observation per bandwidth x rho; descriptive scan, not a statistical threshold estimate",
    }
    report.mkdir(parents=True, exist_ok=True)
    (report / "BANDWIDTH_DOS_COMPLETE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_text = f"""# 带宽 DoS 完整 rho 扫描报告

## 结论

本报告合并历史 TODO2 带宽 DoS 结果和本次补充运行，覆盖 5、10、20 Mbps 三个瓶颈带宽。规范分析表为每个带宽的 `rho=0..2`、步长 `0.25` 网格，共 **27 条观测**；补充运行共 **{len(new_rows)} 条**，历史结果共 **{len(old_rows)} 条**。

历史矩阵中的 `rho=0.8` 和 `rho=1.2` 不在 0.25 网格上，未删除，单独保存在 `data/bandwidth_dos_off_grid_legacy.csv`，完整审计表保存在 `data/bandwidth_dos_all_observations.csv`。规范曲线只使用 `data/bandwidth_dos_grid.csv`，不对缺失水平插值。

历史归档当前只保留汇总 CSV，原始 `physics.csv` 已不在旧运行目录中。因此历史 15 条记录直接沿用原汇总值；新增运行的网络、通信和质量指标从本次原始输出重新提取，正确性辅助字段使用本次运行自身作为结构参考，并在 `correctness_reference` 中标记为 `self_reference_raw_archive_unavailable`。这不会改变带宽、rho、网络守恒或 Modbus 指标。

## 实验条件

- 目标：PLC2；攻击：单 Bot UDP DoS；攻击速率：`bandwidth × rho`。
- 瓶颈链路：`r0-r_scada` 和 `r0-r2`，双向配置同一带宽，传播时延 `2 ms`。
- 队列：`DropTailQueue`，容量 `100 packets`。
- 控制窗口：`100 iterations`；其他 OpenPLC、SCADA、水力模型、攻击窗口和指标配置沿用历史 TODO2 带宽实验。
- 每个配置为一次观测；结果用于描述扫描曲线，不据此声明普适临界值。

## 数据文件

- [规范 27 点网格](data/bandwidth_dos_grid.csv)
- [全部 33 条观测](data/bandwidth_dos_all_observations.csv)
- [历史非网格记录](data/bandwidth_dos_off_grid_legacy.csv)
- [质量检查](data/bandwidth_dos_quality.csv)
- [实验清单](BANDWIDTH_DOS_COMPLETE_MANIFEST.json)

## 阈值观察

`rho` 的首个重传、超时或队列丢弃点只是本机仿真、单次运行下的观测；若没有队列丢弃，`null` 表示“未观察到”，不是缺少测量。

```json
{json.dumps(onset, ensure_ascii=False, indent=2)}
```

## 图表

图表位于 `figures/`，包括队列丢弃、TCP 重传率、Modbus RTT P95、超时率和最大数据陈旧度随 `rho` 的完整网格曲线。
"""
    (report / "BANDWIDTH_DOS_COMPLETE_REPORT.md").write_text(report_text, encoding="utf-8")
    print(json.dumps({"all": len(all_rows), "grid": len(grid_rows), "off_grid": len(off_grid_rows), "output": str(report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
