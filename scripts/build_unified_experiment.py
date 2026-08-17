#!/usr/bin/env python3
"""Merge all experiment archives into one deduplicated scientific dataset."""
from __future__ import annotations

import csv
import json
import math
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

OUTPUT = Path("/home/lzh/MASTER/CODE/output")
REPORT = OUTPUT / "report"
ROOT = REPORT / "unified_experiment"
T1 = OUTPUT / "quantitative_20260716T113050_metric_cde39ea"
T2 = OUTPUT / "quantitative_todo2_20260716T160500+0800_metric_cde39ea"
T3 = OUTPUT / "quantitative_supplement_20260721T232042+0800_metric_cde39ea"


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


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
        writer.writerows(rows)


def copy_file(source: Path, target: Path) -> None:
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def source_ref(path: Path) -> str:
    return str(path.relative_to(OUTPUT)).replace("\\", "/")


def canonical_row(unified_id: str, family: str, condition: str, parameter: str,
                  value: Any, source: Path, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "unified_id": unified_id, "family": family, "condition": condition,
        "parameter": parameter, "value": value, "source": source_ref(source),
        "selection_reason": reason, **extra,
    }


def build() -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    source_dir = ROOT / "source_registry"
    source_dir.mkdir(exist_ok=True)
    figures = ROOT / "figures"
    figures.mkdir(exist_ok=True)
    for figure in (
        REPORT / "figures/todo3/configured_vs_measured_link_delay.png",
        REPORT / "figures/todo3/configured_vs_measured_loss_rate.png",
        REPORT / "figures/todo3/rho_vs_queue_occupancy.png",
        REPORT / "figures/todo3/rho_vs_modbus_timeout_rate.png",
        REPORT / "figures/todo2/timeline_mitm.png",
        REPORT / "figures/todo2/timeline_three_bots.png",
        REPORT / "figures/todo2/timeline_plc_logic.png",
    ):
        copy_file(figure, figures / figure.name)

    configurations: list[dict[str, Any]] = []
    dedup: list[dict[str, Any]] = []

    # Baseline correctness: one canonical reference, not repeated in every
    # later family table.
    baseline = T1 / "04_performance_and_batch_statistics__20260716T150420/correctness_summary.json"
    copy_file(baseline, data / "baseline_correctness.json")
    configurations.append(canonical_row("baseline_reference", "baseline", "C-Town reference", "none", 0, baseline, "single correctness reference used by all comparisons"))

    # Delay: retain the 5-repeat matrix as the canonical configuration family;
    # attach the newer directional/packet-boundary measurements as evidence,
    # rather than counting the seven newer rows a second time.
    delay_per_run = read_csv(T1 / "04_performance_and_batch_statistics__20260716T150420/target_link_delay_per_run.csv")
    for row in delay_per_run:
        configurations.append(canonical_row(
            row.get("experiment_id", "delay"), "delay", "target-link delay", "delay_ms",
            number(row.get("parameter_value_ms")), T1 / "04_performance_and_batch_statistics__20260716T150420/target_link_delay_per_run.csv",
            "five-repeat matrix is the strongest repetition evidence", repetition=row.get("repetition"),
        ))
    write_csv(data / "delay_repeated_matrix.csv", [{**row, "family": "delay", "canonical": True} for row in delay_per_run])
    delay_directional = read_csv(T3 / "09_combined_statistics/delay_link_direction_per_run.csv")
    delay_rtt = read_csv(T3 / "09_combined_statistics/delay_modbus_rtt_per_run.csv")
    write_csv(data / "delay_directional_evidence.csv", delay_directional)
    write_csv(data / "delay_rtt_evidence.csv", delay_rtt)

    # Packet loss: use the 300-cycle, fully instrumented scan; add only the
    # unique 10% level from the older scan. Other older levels are duplicates.
    loss_rows = read_csv(T3 / "09_combined_statistics/packet_loss_21_levels_per_run.csv")
    loss_levels = {round(number(row.get("configured_loss_rate")), 6) for row in loss_rows}
    canonical_loss: list[dict[str, Any]] = []
    for row in loss_rows:
        canonical_loss.append({**row, "family": "packet_loss", "canonical_source": "complete_300_cycle_scan"})
        configurations.append(canonical_row(row.get("experiment_id", "packet_loss"), "packet_loss", "target-link packet loss", "loss_rate", number(row.get("configured_loss_rate")), T3 / "09_combined_statistics/packet_loss_21_levels_per_run.csv", "300-cycle scan with full conservation and cross-layer instrumentation"))
    old_loss_path = T2 / "results/packet_loss_per_run.csv"
    for row in read_csv(old_loss_path):
        level = round(number(row.get("configured_loss_rate")), 6)
        if abs(level - 0.1) < 1e-9 and level not in loss_levels:
            canonical_loss.append({**row, "family": "packet_loss", "canonical_source": "unique_10pct_scan"})
            configurations.append(canonical_row(row.get("experiment_id", "packet_loss_10pct"), "packet_loss", "target-link packet loss", "loss_rate", level, old_loss_path, "unique 10% level absent from the complete scan"))
        else:
            dedup.append({"dropped_or_not_counted": row.get("experiment_id"), "family": "packet_loss", "reason": "duplicate loss level; complete 300-cycle scan retained", "kept_in": "packet_loss.csv"})
    write_csv(data / "packet_loss.csv", canonical_loss)

    # Bandwidth DoS remains a distinct factor family: bandwidth and rho jointly
    # define a different condition from the fixed 10 Mbps queue experiment.
    dos_path = T2 / "results/bandwidth_dos_per_run.csv"
    dos_rows = read_csv(dos_path)
    write_csv(data / "bandwidth_dos.csv", [{**row, "family": "bandwidth_dos"} for row in dos_rows])
    for row in dos_rows:
        configurations.append(canonical_row(row.get("experiment_id", "bandwidth_dos"), "bandwidth_dos", "bandwidth DoS", "bandwidth_mbps/rho", f"{row.get('bandwidth_mbps')}/{row.get('rho')}", dos_path, "unique bandwidth × rho factor combination"))

    queue_path = T3 / "09_combined_statistics/controlled_congestion_per_run.csv"
    queue_rows = read_csv(queue_path)
    write_csv(data / "queue_congestion.csv", [{**row, "family": "queue_congestion"} for row in queue_rows])
    for row in queue_rows:
        configurations.append(canonical_row(row.get("experiment_id", "queue"), "queue_congestion", "10 Mbps r0-to-r4 bottleneck with three bots", "rho", number(row.get("rho")), queue_path, "distinct controlled queue topology"))

    # Attack propagation: keep one single-bot result and the newer timestamped
    # representatives for three-bot DoS, MITM and logic injection. Older rows
    # remain auditable but are not double-counted.
    attack_rows: list[dict[str, Any]] = []
    single_path = T2 / "results/dos_propagation_per_run.csv"
    for row in read_csv(single_path):
        if str(row.get("scenario")) == "single_bot":
            attack_rows.append({**row, "family": "attack_propagation", "canonical_source": "single_bot"})
            configurations.append(canonical_row("attack_single_bot", "attack_propagation", "single-bot DoS", "scenario", "single_bot", single_path, "unique attack condition"))
        else:
            dedup.append({"dropped_or_not_counted": row.get("experiment_id"), "family": "attack_propagation", "reason": "replaced by timestamped representative", "kept_in": "attack_propagation.csv"})
    timestamp_path = T3 / "05_cross_layer_timestamps/attack_propagation_comparison.csv"
    for row in read_csv(timestamp_path):
        attack_rows.append({**row, "family": "attack_propagation", "canonical_source": "timestamped_representative"})
        configurations.append(canonical_row(row.get("experiment_id", "attack"), "attack_propagation", row.get("experiment_id", "attack"), "scenario", row.get("scenario", ""), timestamp_path, "newer cross-layer timestamp representative"))
    write_csv(data / "attack_propagation.csv", attack_rows)
    dedup.extend([
        {"dropped_or_not_counted": "historical_MITM_PLC4", "family": "attack_propagation", "reason": "duplicate MITM condition; timestamped representative retained", "kept_in": "attack_propagation.csv"},
        {"dropped_or_not_counted": "historical_three_bot_DoS", "family": "attack_propagation", "reason": "duplicate three-bot condition; timestamped representative retained", "kept_in": "attack_propagation.csv"},
        {"dropped_or_not_counted": "historical_PLC4_logic_injection", "family": "attack_propagation", "reason": "duplicate logic-injection condition; timestamped representative retained", "kept_in": "attack_propagation.csv"},
        {"dropped_or_not_counted": "dos_intensity_20mbps_reuse", "family": "bandwidth_dos", "reason": "diagnostic reused the canonical bandwidth DoS table; no second copy counted", "kept_in": "bandwidth_dos.csv"},
    ])
    old_logic_path = T2 / "results/plc_logic_injection_per_run.csv"
    for row in read_csv(old_logic_path):
        dedup.append({"dropped_or_not_counted": row.get("experiment_id", "historical_PLC4_logic_injection"), "family": "attack_propagation", "reason": "duplicate logic-injection condition; timestamped representative retained", "kept_in": "attack_propagation.csv"})

    sensitivity_source = T3 / "06_correctness_sensitivity/runs/correctness_sensitivity_plc4_4p8_to_4p7/attempt_03/summary.json"
    sensitivity = [{"unified_id": "correctness_sensitivity_plc4", "family": "correctness_sensitivity", "condition": "PLC4 threshold 4.8 to 4.7", "source": source_ref(sensitivity_source), "status": "valid", "note": "single sensitivity observation; raw check and event timeline remain in source archive"}]
    write_csv(data / "correctness_sensitivity.csv", sensitivity)
    configurations.append(canonical_row("correctness_sensitivity_plc4", "correctness_sensitivity", "PLC4 threshold change", "threshold", "4.8_to_4.7", sensitivity_source, "unique sensitivity condition"))

    write_csv(data / "unified_configurations.csv", configurations)
    write_csv(data / "deduplication_log.csv", dedup)

    source_registry = [
        {"source_archive": source_ref(T1), "role": "baseline, five-repeat delay matrix, historical MITM"},
        {"source_archive": source_ref(T2), "role": "unique 10% loss, bandwidth DoS, single-bot attack, historical duplicates"},
        {"source_archive": source_ref(T3), "role": "canonical full loss scan, directional delay evidence, queue, timestamped attacks, sensitivity"},
        {"source_archive": "quantitative_todo2_20260716T160131+0800_metric_cde39ea", "role": "preparation-only archive; no valid experiment result"},
        {"source_archive": "quantitative_supplement_20260722T162827+0800_metric_cde39ea", "role": "partial process archive; not counted in canonical result"},
    ]
    write_csv(source_dir / "source_registry.csv", source_registry)
    copy_file(T3 / "09_combined_statistics/delay_regression_summary.json", data / "delay_regression_summary.json")
    copy_file(T3 / "09_combined_statistics/supplement_summary.json", data / "network_quality_summary.json")
    copy_file(T3 / "08_large_scale_summary/large_scale_capability_summary.json", data / "large_scale_summary.json")

    counts: dict[str, int] = {}
    for row in configurations:
        counts[row["family"]] = counts.get(row["family"], 0) + 1
    manifest = {
        "title": "Hydro-CPS unified quantitative experiment",
        "generated_at": datetime.now().astimezone().isoformat(),
        "canonical_observation_count": len(configurations),
        "canonical_counts_by_family": counts,
        "deduplicated_record_count": len(dedup),
        "deduplication_policy": "Prefer repeated matrix or richer timestamped/instrumented data; retain unique parameter levels and topologies; preserve all originals in source archives.",
        "source_registry": "source_registry/source_registry.csv",
        "data_directory": "data/",
    }
    (ROOT / "UNIFIED_EXPERIMENT_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = f"""# Hydro-CPS 统一量化实验报告

生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}  
这是一个统一实验报告，不按历史任务编号分组。原始归档只作为证据来源；重复条件已去重，正式统计只使用 `data/` 中的规范表。

## 1. 统一实验结论

统一数据集包含 **{len(configurations)} 个规范观测配置**，另有 **{len(dedup)} 条被识别为重复或过程记录的条目**。重复内容没有从原始归档删除，而是通过 `data/deduplication_log.csv` 明确排除。

- 延迟条件使用早期 7 个水平 × 5 次重复矩阵作为主统计；后续四方向和四时刻 Modbus 记录作为同一条件的测量证据，不重复计数。
- 丢包条件保留完整 300 周期扫描的 21 个水平，并只补入旧扫描中唯一的 10% 水平；旧扫描中与其重叠的 1%、2%、5% 和 50% 被舍去。
- 带宽 DoS 与固定 10 Mbps 三 Bot 队列拥塞属于不同网络拓扑和因子组合，全部保留。
- 单 Bot DoS 保留一次；MITM、三 Bot DoS、PLC4 逻辑注入保留有四时刻时间戳的代表运行，历史重复记录不再进入规范表。
- 基线和 PLC4 阈值敏感性各保留一次。

## 2. 实验设计

实验在本机 C-Town 数字孪生网络执行，使用 `hydro-cps` conda 环境、ns-3、Linux network namespace、OpenPLC 与 EPANET/DHALSim。网络规模为 388 junction、7 tank、1 reservoir、429 pipe、11 pump、4 valve、9 PLC 和 1 SCADA。

统一实验覆盖五类科学因素：

1. **传播延迟**：0、2、5、10、20、50、100 ms，主结果为 35 次重复；另有方向和 Modbus 四时刻证据。
2. **随机丢包**：0–9.5% 以 0.5% 步进，加 50% 极端压力；主扫描 300 周期，另保留唯一 10% 水平。
3. **资源拥塞**：带宽 DoS 的带宽×rho 组合，以及 10 Mbps/20 包 DropTail 三 Bot 队列压力。
4. **攻击传播**：单 Bot DoS、强三 Bot DoS、MITM PLC4/T7、PLC4 逻辑注入。
5. **正确性**：基线参考和 PLC4 4.8→4.7 阈值敏感性。

## 3. 结果描述

### 延迟与 RTT

链路测量对配置延迟的回归斜率约为 1，Modbus RTT 对配置延迟的斜率约为 4 ms/ms。重复矩阵用于估计重复性，方向性和四时刻表用于验证路径边界；两者在统一实验中承担不同证据角色而不是相互平均。

### 丢包与网络质量

规范丢包表的网络包守恒和 Modbus 请求分类守恒均通过。首次观察到 TCP 重传的配置约为 0.5%，首次观察到 Modbus 超时约为 1.5%。50% 极端条件保留 299 个闭环周期和重试成本，作为压力测试结果，不当作典型运行阈值。

### 拥塞与 DoS

队列压力达到 rho≥1 时队列占用率达到容量，但未观察到目标链路 DropTail 丢包；RTT、TCP 重传和 Modbus 超时更早体现退化。带宽 DoS 表保留了 5/10/20 Mbps 的全部唯一组合，因为其攻击流量和拓扑不能被固定 10 Mbps 队列实验替代。

### 攻击传播与正确性

时间戳代表运行表明 MITM 和 PLC4 逻辑注入产生控制/物理偏差，而强三 Bot DoS 在观测窗口内没有产生相同偏差。基线和阈值敏感性独立保存，用于区分正常控制逻辑变化与网络攻击影响。

## 4. 质量与去重规则

- 网络守恒：发送包必须由接收、错误模型丢弃、队列丢弃、停止时在途包或其他分类完全解释。
- Modbus 守恒：每个非连接、非 warm-up 请求必须有唯一结果类别。
- 规范记录优先级：重复矩阵 > 300 周期完整扫描 > 时间戳/边界更完整的代表运行 > 旧单次运行。
- 旧记录、失败尝试和准备批次不删除，只通过去重日志和源注册表排除出规范数据。

## 5. 限制与后续

多数网络压力和攻击配置为单次观测，因此统一表用于描述效应和比较条件，不提供普适临界值、因果证明或跨重复置信区间。可选 WNTR 独立交叉核验未作为完整对照执行。论文统计推断前，应对关键丢包、rho 和攻击条件增加独立重复并预注册停止规则、随机种子和区间估计方法。

## 6. 文件入口

- [统一实验清单](UNIFIED_EXPERIMENT_MANIFEST.json)
- [规范配置表](data/unified_configurations.csv)
- [去重日志](data/deduplication_log.csv)
- [规范结果表](data/)
- [统一实验图表](figures/)
- [源归档注册表](source_registry/source_registry.csv)
"""
    (ROOT / "UNIFIED_EXPERIMENT_REPORT.md").write_text(summary, encoding="utf-8")
    (ROOT / "README.md").write_text("# Unified experiment\n\n- [统一实验报告](UNIFIED_EXPERIMENT_REPORT.md)\n- [统一实验清单](UNIFIED_EXPERIMENT_MANIFEST.json)\n- [规范数据](data/)\n- [去重日志](data/deduplication_log.csv)\n- [源注册表](source_registry/source_registry.csv)\n\n这里不再按历史任务编号划分实验；所有源归档仅用于审计。\n", encoding="utf-8")
    copy_file(ROOT / "UNIFIED_EXPERIMENT_REPORT.md", REPORT / "UNIFIED_EXPERIMENT_REPORT.md")
    copy_file(ROOT / "UNIFIED_EXPERIMENT_MANIFEST.json", REPORT / "UNIFIED_EXPERIMENT_MANIFEST.json")
    return ROOT


if __name__ == "__main__":
    print(build())
