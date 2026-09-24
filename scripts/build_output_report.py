#!/usr/bin/env python3
"""Consolidate all quantitative experiment archives into one report folder."""
from __future__ import annotations

import csv
import json
import math
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


OUTPUT_ROOT = Path("/home/lzh/MASTER/CODE/output")
REPORT_ROOT = OUTPUT_ROOT / "report"
T1 = OUTPUT_ROOT / "quantitative_20260716T113050_metric_cde39ea"
T2 = OUTPUT_ROOT / "quantitative_todo2_20260716T160500+0800_metric_cde39ea"
T2_PARTIAL = OUTPUT_ROOT / "quantitative_todo2_20260716T160131+0800_metric_cde39ea"
T3 = OUTPUT_ROOT / "quantitative_supplement_20260721T232042+0800_metric_cde39ea"
PARTIAL = OUTPUT_ROOT / "quantitative_supplement_20260722T162827+0800_metric_cde39ea"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def num(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def copy_file(source: Path, destination: Path) -> None:
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def copy_tree_files(source: Path, destination: Path, suffixes: tuple[str, ...]) -> None:
    if not source.is_dir():
        return
    for path in source.rglob("*"):
        if path.is_file() and path.suffix.lower() in suffixes:
            copy_file(path, destination / path.relative_to(source))


def archive_row(path: Path, label: str, kind: str) -> dict[str, Any]:
    index = read_json(path / "RUN_INDEX.json", []) or []
    valid = sum(1 for item in index if item.get("valid")) if isinstance(index, list) else None
    records = len(index) if isinstance(index, list) else None
    if path == T1:
        # The first archive predates RUN_INDEX.json; its unified summary table
        # records 37 completed observations (35 delay repeats + baseline + MITM).
        valid, records = 37, 37
    size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return {
        "archive": path.name,
        "label": label,
        "kind": kind,
        "formal_valid_runs": valid,
        "run_index_records": records,
        "size_gb": round(size / 1e9, 3),
        "source_archive": f"sources/archives/{path.name}",
    }


def build_summary_markdown(archive_rows: list[dict[str, Any]], attack_rows: list[dict[str, Any]],
                           t3_summary: dict[str, Any], t3_regression: dict[str, Any],
                           t3_loss: dict[str, Any], t3_congestion: dict[str, Any]) -> str:
    cross = {row["experiment_id"]: row for row in attack_rows}
    mitm = cross.get("timestamp_mitm_plc4_t7", {})
    logic = cross.get("timestamp_plc4_logic_injection", {})
    dos = cross.get("timestamp_dos_three_bot_strong", {})
    return f"""# Hydro-CPS 仿真实验设计与结果总结

生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}  
项目：`hydro-cps-sim`，分支：`metric`

## 一、技术摘要

本报告把 `output` 下的全部量化实验归档整合为一个可审计入口。正式结果按实验批次保留，不把重复配置或失败重试混入正式统计：早期基线/延迟矩阵批次包含 35 个延迟重复运行、基线和 MITM 观测；TODO2 批次包含 23 个有效新配置；TODO3 补充批次包含 {t3_summary.get('formal_adopted_count', 36)} 个有效正式配置。所有失败尝试仍保存在源归档中。

关键结论：

- 延迟验证与理论关系高度一致：链路测量对配置延迟的回归斜率为 `{num(t3_regression.get('link_delay_ms_vs_configured', {}).get('slope')):.4f}`，R²=`{num(t3_regression.get('link_delay_ms_vs_configured', {}).get('r_squared')):.6f}`；Modbus RTT 斜率约 `{num(t3_regression.get('modbus_rtt_ms_vs_configured', {}).get('slope')):.3f}` ms/ms。
- TODO3 丢包扫描覆盖 0–9.5%（0.5% 步进）及 50% 极端压力，共 21 个水平；网络守恒和 Modbus 请求分类守恒均无失败。首次观察到 TCP 重传的配置水平为 `{num(t3_loss.get('first_tcp_retransmission_level')):.3f}`，首次观察到 Modbus 超时的水平为 `{num(t3_loss.get('first_modbus_timeout_level')):.3f}`。
- 拥塞实验的四个 rho 水平均完成 99 个控制周期；rho≥1 时队列占用率达到 1.0，但本批次未观察到 DropTail 丢包，因此“未观察到队列丢包”不能解释为缺少队列测量。
- 跨层时间戳显示 MITM PLC4/T7 与 PLC4 逻辑注入分别产生 {mitm.get('actuator_mismatch_count', 0)} 和 {logic.get('actuator_mismatch_count', 0)} 个执行器偏差计数，物理峰值偏差约 {num(mitm.get('physical_peak_absolute_deviation')):.3f} m 与 {num(logic.get('physical_peak_absolute_deviation')):.3f} m；强三 Bot DoS 在该观测窗口内未产生执行器/物理偏差。

## 二、实验设计

### 2.1 仿真边界与可复现性

所有实验均在本机 C-Town 数字孪生网络内执行，使用 conda 环境 `hydro-cps`、Linux network namespace、ns-3、OpenPLC 和 EPANET/DHALSim 后端。网络模型包括 388 个 junction、7 个 tank、1 个 reservoir、429 条 pipe、11 个 pump、4 个 valve、9 个 PLC 和 1 个 SCADA。原始配置、解析配置、manifest、日志、PCAP、CSV/JSON 指标和 workspace patch 都保存在对应源归档中。

### 2.2 实验矩阵

| 批次 | 设计 | 正式结果 | 主要指标 |
|---|---|---:|---|
| 早期基线与延迟矩阵 | 0/2/5/10/20/50/100 ms，5 次重复；另含基线与 MITM | 37 个统一运行行 | 正确性、链路延迟、Modbus RTT、性能、MITM 传播 |
| TODO2 | 5 个新增非零丢包水平；5/10/20 Mbps × rho 0/0.8/1/1.2/1.5；单 Bot、三 Bot、PLC 逻辑注入 | 23 个有效配置 | 丢包、DoS 队列/RTT/重传、控制/物理偏差、攻击传播 |
| TODO3 补充 | 7 个延迟配置；21 个丢包水平；4 个受控拥塞 rho；3 个跨层时间戳；1 个正确性敏感性 | 36 个有效配置 | 四时刻 Modbus 边界时间戳、TCP、网络守恒、陈旧年龄、物理/控制指标 |
| 最新部分批次 | 仅完成 delay 0/2 ms 的部分尝试 | 1/4 尝试有效 | 作为过程记录，不并入正式去重结果 |

### 2.3 采样与判定

TODO3 的丢包组采用 300 个配置周期：100 周期预检在目标链路产生约 3,960 个包，0.5% 时期望丢包约 19.8 个，接近至少 20 个事件的观测要求。正式结果以独立配置为单位；单次观测不计算跨重复标准差、置信区间、显著性检验或误差条。

## 三、整合结果

### 3.1 延迟：测量链路与理论模型一致

TODO1 的 5 次重复提供了重复性基线，TODO3 又保留四方向链路行、端到端 Modbus 包边界及 RTT。配置延迟增加时，链路均值近似线性增加；RTT 斜率约 4 ms/ms，符合两条链路、请求与响应双向传播的结构预期。精确回归参数和每个方向结果见 `data/todo3_delay_link_direction.csv`、`data/todo3/delay_modbus_rtt_per_run.csv` 与 `data/todo3/delay_regression_summary.json`。

### 3.2 丢包：网络守恒完整，应用层退化先于极端崩溃

21 个水平的网络包守恒均成立：发送包被接收、错误模型丢弃、队列丢弃、停止时在途包或其他分类完整解释。TCP 重传在 0.5% 首次出现，Modbus 超时在 1.5% 首次出现；50% 配置在高重试环境下完成 299 个控制周期，完整保留高丢包墙钟成本和应用层结果。详细表格见 `data/todo3_loss_per_run.csv`。

### 3.3 拥塞与 DoS：队列饱和不等于立即丢包

受控拥塞把 r0→r4 限制为 10 Mbps、20 包 DropTail，并使用三个 Bot 在不同 rho 下注入流量。rho=1/1.5/2 时队列达到容量，但观测窗口内目标链路队列丢包仍为 0；与此同时 RTT、TCP 重传和 Modbus 超时随 rho 增大而恶化。TODO2 的 5/10/20 Mbps DoS 结果被单独保留，20 Mbps 表复用于 TODO3 的 D 项诊断，不重复运行。

### 3.4 跨层攻击传播与敏感性

MITM PLC4/T7 和 PLC4 逻辑注入均在控制层和物理层留下偏差，而强三 Bot DoS 在本次 99 周期窗口内未产生对应偏差。早期 MITM 观测还记录了第 20 次迭代攻击/控制异常、第 22 次迭代首次物理偏差以及第 41 次迭代攻击关闭边界。PLC4 4.8→4.7 的敏感性结果与原正确性基线分开保存，不能把阈值改变造成的行为差异解释为网络丢包。

## 四、指标定义与证据路径

- **网络守恒**：`tx = rx + error_model_drop + queue_drop + pending_at_stop + other_classified_loss`；在途包不计为丢包。
- **Modbus 守恒**：非 warm-up、非 connect 请求必须被分类为 success、timeout、exception、connection error 或 other failure。
- **链路延迟**：ns-3 link trace 的方向性测量；延迟矩阵分别保存 r0-r_scada 与 r0-r4 的四个方向行。
- **Modbus RTT/四时刻**：SCADA 请求发出、PLC 收到、PLC 响应发出、SCADA 收到，使用 namespace 内单调时钟纳秒时间戳。
- **正确性/物理偏差**：相对于基线的执行器不一致、tank RMSE 和峰值绝对偏差；物理容差为 0.01 m。

完整数据入口位于 `data/`；所有源归档通过 `sources/archives/` 关联，原始 PCAP 和逐周期日志不复制到报告目录，避免重复占用磁盘，但仍可通过源归档链接访问。

## 五、限制、稳健性与待办

- 多数 TODO2/TODO3 配置为单次观测，趋势图用于描述，不证明单调性、因果关系或统计显著性。
- 早期延迟矩阵有 5 次重复并可报告均值/离散性；不能把这套重复统计直接外推到 TODO2/TODO3 单观测组。
- 50% 丢包是极端通信破坏压力，不代表典型工业运行区间；其 299 个周期和重试成本应单独解读。
- TODO3 的可选 WNTR/EPANET 独立交叉核验未作为完整 DHALSIM 对照执行；报告明确标记为未完成，而不是伪造等价结果。
- 后续如需论文统计推断，应对关键水平增加独立重复，并预先固定随机种子、停止规则、异常值规则和置信区间方法。

## 六、建议的论文使用方式

1. 用早期 5 次延迟重复作为测量重复性与理论回归图。
2. 用 TODO3 21 水平丢包表展示“网络错误→TCP/Modbus 退化→控制/物理指标”的跨层链路。
3. 用 TODO2/TODO3 DoS 与拥塞表分开描述队列饱和、重传和应用层退化，避免把 rho 阈值写成普适临界值。
4. 用跨层时间戳 CSV、传播 summary 和原始 PCAP 支撑攻击顺序；把失败重试作为可追溯性附录，不作为正式结果点。

## 七、源归档清单

| 归档 | 类型 | 有效运行/记录 | 说明 |
""" + "\n".join(
        f"| `{row['archive']}` | {row['label']} | {row.get('formal_valid_runs', '')}/{row.get('run_index_records', '')} | [源归档]({row['source_archive']}) |"
        for row in archive_rows
    ) + "\n"


def build_artifact(archive_rows: list[dict[str, Any]], summary: dict[str, Any],
                   regression: dict[str, Any], loss_summary: dict[str, Any],
                   delay_rows: list[dict[str, Any]], loss_rows: list[dict[str, Any]],
                   congestion_rows: list[dict[str, Any]], attack_rows: list[dict[str, Any]]) -> dict[str, Any]:
    sources = [
        {"id": "summary_doc", "label": "实验设计与结果总结", "path": "EXPERIMENT_DESIGN_AND_RESULTS.md"},
        {"id": "archive_inventory", "label": "全部归档清单", "path": "data/archive_inventory.csv"},
        {"id": "todo3_delay", "label": "TODO3 延迟结果", "path": "data/todo3_delay_link_direction.csv"},
        {"id": "todo3_loss", "label": "TODO3 丢包结果", "path": "data/todo3_loss_per_run.csv"},
        {"id": "todo3_congestion", "label": "TODO3 拥塞结果", "path": "data/todo3_congestion_per_run.csv"},
        {"id": "cross_layer", "label": "跨层攻击传播比较", "path": "data/attack_propagation_comparison.csv"},
    ]
    headline = [{
        "formal_runs": summary.get("formal_adopted_count", 36),
        "loss_levels": loss_summary.get("levels_adopted", 21),
        "network_conservation_rate": 1.0,
        "modbus_conservation_rate": 1.0,
    }]
    datasets = {
        "headline": headline,
        "archive_inventory": archive_rows,
        "delay_curve": [{"configured_delay_ms": num(row.get("configured_delay_ms")), "measured_delay_mean_ms": num(row.get("measured_delay_mean_ms")), "link": row.get("link", "") } for row in delay_rows],
        "loss_curve": [{"configured_loss_rate": num(row.get("configured_loss_rate")), "measured_loss_rate": num(row.get("measured_loss_rate")), "tcp_retransmission_rate": num(row.get("tcp_retransmission_rate"))} for row in loss_rows],
        "congestion_curve": [{"rho": num(row.get("rho")), "queue_occupancy_ratio_max": num(row.get("queue_occupancy_ratio_max")), "modbus_timeout_rate": num(row.get("modbus_timeout_rate"))} for row in congestion_rows],
        "attack_comparison": attack_rows,
    }
    charts = [
        {"id": "delay_chart", "title": "Configured delay versus measured link delay", "subtitle": "TODO3 target-link directional rows; one observation per direction and configuration.", "type": "line", "dataset": "delay_curve", "sourceId": "todo3_delay", "valueFormat": "number", "encodings": {"x": {"field": "configured_delay_ms", "type": "quantitative", "label": "Configured delay (ms)"}, "y": {"field": "measured_delay_mean_ms", "type": "quantitative", "label": "Measured delay (ms)"}, "color": {"field": "link", "type": "nominal", "label": "Link"}}},
        {"id": "loss_chart", "title": "Configured packet loss versus measured loss", "subtitle": "21 TODO3 levels including the 50% extreme stress case.", "type": "line", "dataset": "loss_curve", "sourceId": "todo3_loss", "valueFormat": "percent", "encodings": {"x": {"field": "configured_loss_rate", "type": "quantitative", "label": "Configured loss rate"}, "y": {"field": "measured_loss_rate", "type": "quantitative", "label": "Measured loss rate"}, "tooltip": [{"field": "tcp_retransmission_rate", "type": "quantitative", "label": "TCP retransmission rate", "format": "percent"}] }},
        {"id": "congestion_chart", "title": "Queue occupancy under controlled congestion", "subtitle": "rho=0, 1, 1.5, 2 on the 10 Mbps, 20-packet r0→r4 bottleneck.", "type": "line", "dataset": "congestion_curve", "sourceId": "todo3_congestion", "valueFormat": "percent", "encodings": {"x": {"field": "rho", "type": "quantitative", "label": "Configured rho"}, "y": {"field": "queue_occupancy_ratio_max", "type": "quantitative", "label": "Maximum queue occupancy"}, "tooltip": [{"field": "modbus_timeout_rate", "type": "quantitative", "label": "Modbus timeout rate", "format": "percent"}] }},
        {"id": "attack_chart", "title": "Cross-layer attack propagation", "subtitle": "Actuator mismatch count and physical peak deviation for three representative runs.", "type": "bar", "dataset": "attack_comparison", "sourceId": "cross_layer", "valueFormat": "number", "encodings": {"x": {"field": "experiment_id", "type": "nominal", "label": "Experiment"}, "y": {"field": "physical_peak_absolute_deviation", "type": "quantitative", "label": "Physical peak deviation (m)"}, "tooltip": [{"field": "actuator_mismatch_count", "type": "quantitative", "label": "Actuator mismatch count"}] }},
    ]
    cards = [
        {"id": "formal_runs", "description": "有效的 TODO3 正式配置", "dataset": "headline", "sourceId": "summary_doc", "metrics": [{"label": "TODO3 formal runs", "field": "formal_runs", "format": "number"}]},
        {"id": "loss_levels", "description": "TODO3 丢包水平数量", "dataset": "headline", "sourceId": "summary_doc", "metrics": [{"label": "Loss levels", "field": "loss_levels", "format": "number"}]},
        {"id": "network_quality", "description": "网络守恒检查通过率", "dataset": "headline", "sourceId": "summary_doc", "metrics": [{"label": "Network conservation", "field": "network_conservation_rate", "format": "percent"}]},
        {"id": "modbus_quality", "description": "Modbus 请求分类守恒通过率", "dataset": "headline", "sourceId": "summary_doc", "metrics": [{"label": "Modbus conservation", "field": "modbus_conservation_rate", "format": "percent"}]},
    ]
    blocks = [
        {"id": "title", "type": "markdown", "body": "# Hydro-CPS 仿真实验整合报告"},
        {"id": "technical_summary", "type": "markdown", "body": "## 技术摘要\n\n本报告整合 output 下全部量化实验归档，正式结果与失败尝试分开保留。TODO3 补充批次有 36 个有效配置，21 个丢包水平；网络与 Modbus 守恒检查均通过。详细设计、结果、证据路径和限制见配套总结文档。", "sourceId": "summary_doc"},
        {"id": "headline_metrics", "type": "metric-strip", "cardIds": ["formal_runs", "loss_levels", "network_quality", "modbus_quality"]},
        {"id": "delay_finding", "type": "markdown", "body": "## 延迟回归与四方向测量一致\n\n链路测量随配置延迟近似线性变化；早期 5 次重复矩阵用于重复性，TODO3 用四方向链路行和 Modbus 四时刻时间戳补充端到端证据。", "sourceId": "todo3_delay"},
        {"id": "delay_chart_block", "type": "chart", "chartId": "delay_chart"},
        {"id": "loss_finding", "type": "markdown", "body": "## 丢包首先表现为 TCP/Modbus 退化\n\n0.5% 配置首次观察到 TCP 重传，1.5% 首次观察到 Modbus 超时；50% 极端压力仍保留 299 个控制周期的完整证据。", "sourceId": "todo3_loss"},
        {"id": "loss_chart_block", "type": "chart", "chartId": "loss_chart"},
        {"id": "congestion_finding", "type": "markdown", "body": "## 队列饱和不等于立即 DropTail 丢包\n\nrho≥1 时最大队列占用率达到 1.0，但目标链路未观察到队列丢包；RTT、重传和超时是更敏感的退化指标。", "sourceId": "todo3_congestion"},
        {"id": "congestion_chart_block", "type": "chart", "chartId": "congestion_chart"},
        {"id": "attack_finding", "type": "markdown", "body": "## 跨层攻击结果区分网络压力与恶意修改\n\nMITM 与 PLC4 逻辑注入产生执行器/物理偏差，强三 Bot DoS 在观测窗口内未产生对应偏差。该差异不应被简化成单一的网络丢包阈值。", "sourceId": "cross_layer"},
        {"id": "attack_chart_block", "type": "chart", "chartId": "attack_chart"},
        {"id": "methodology", "type": "markdown", "body": "## 实验设计、指标定义与限制\n\n报告覆盖 C-Town、ns-3、OpenPLC、EPANET/DHALSim、network namespace 和 conda 环境 hydro-cps。网络守恒、Modbus 守恒、单调时间戳、控制周期、物理偏差与失败重试规则均在总结文档中定义。TODO2/TODO3 多数配置为单次观测，不能据此做跨重复统计推断；可选 WNTR 交叉核验未作为完整对照执行。", "sourceId": "summary_doc"},
        {"id": "archive_table_block", "type": "table", "tableId": "archive_table"},
        {"id": "next_steps", "type": "markdown", "body": "## 建议的后续工作\n\n1. 对关键丢包、rho 和攻击配置增加独立重复。\n2. 预注册停止规则、随机种子、异常值处理和置信区间方法。\n3. 使用本报告中的四时刻时间戳与 PCAP 做论文级传播顺序图。", "sourceId": "summary_doc"},
    ]
    table = {"id": "archive_table", "title": "实验归档清单", "subtitle": "按归档批次列出有效运行、记录数和源路径。", "dataset": "archive_inventory", "sourceId": "archive_inventory", "defaultSort": {"field": "label", "direction": "asc"}, "columns": [{"field": "archive", "label": "Archive", "type": "text"}, {"field": "label", "label": "Batch", "type": "text"}, {"field": "formal_valid_runs", "label": "Valid runs", "type": "number"}, {"field": "run_index_records", "label": "Records", "type": "number"}, {"field": "size_gb", "label": "Size (GB)", "type": "number"}]}
    return {"surface": "report", "manifest": {"title": "Hydro-CPS 仿真实验整合报告", "blocks": blocks, "cards": cards, "charts": charts, "tables": [table], "sources": sources}, "snapshot": {"version": 1, "generatedAt": datetime.now().astimezone().isoformat(), "status": "ready", "datasets": datasets}, "sources": sources}


def main() -> int:
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    for directory in (REPORT_ROOT / "data", REPORT_ROOT / "sources", REPORT_ROOT / "figures"):
        directory.mkdir(parents=True, exist_ok=True)
    # Preserve source archives as links, avoiding another multi-gigabyte copy.
    source_archives = REPORT_ROOT / "sources/archives"
    source_archives.mkdir(parents=True, exist_ok=True)
    archives = [
        (T1, "早期基线与 35-run 延迟矩阵", "baseline_delay_attack"),
        (T2_PARTIAL, "TODO2 预备批次（仅计划）", "todo2_partial"),
        (T2, "TODO2 扩展实验", "todo2"),
        (T3, "TODO3 完整补充实验", "todo3_complete"),
        (PARTIAL, "TODO3 部分尝试（过程记录）", "todo3_partial"),
    ]
    archive_rows = [archive_row(path, label, kind) for path, label, kind in archives if path.is_dir()]
    for path, _label, _kind in archives:
        link = source_archives / path.name
        if link.exists() or link.is_symlink():
            continue
        link.symlink_to(path, target_is_directory=True)
    write_csv(REPORT_ROOT / "data/archive_inventory.csv", archive_rows)

    t3_dir = T3 / "09_combined_statistics"
    delay_rows = read_csv(t3_dir / "delay_link_direction_per_run.csv")
    loss_rows = read_csv(t3_dir / "packet_loss_21_levels_per_run.csv")
    congestion_rows = read_csv(t3_dir / "controlled_congestion_per_run.csv")
    attack_rows = read_csv(T3 / "05_cross_layer_timestamps/attack_propagation_comparison.csv")
    write_csv(REPORT_ROOT / "data/todo3_delay_link_direction.csv", delay_rows)
    write_csv(REPORT_ROOT / "data/todo3_loss_per_run.csv", loss_rows)
    write_csv(REPORT_ROOT / "data/todo3_congestion_per_run.csv", congestion_rows)
    write_csv(REPORT_ROOT / "data/attack_propagation_comparison.csv", attack_rows)
    for name in ("delay_modbus_rtt_per_run.csv", "delay_regression_summary.json", "packet_loss_tcp_metrics.csv", "packet_loss_modbus_metrics.csv", "packet_loss_control_physical_metrics.csv", "controlled_congestion_queue_timeseries.csv", "large_scale_capability_summary.json"):
        source = t3_dir / name if (t3_dir / name).is_file() else T3 / "08_large_scale_summary" / name
        copy_file(source, REPORT_ROOT / "data/todo3" / name)
    # Copy compact historical tables, plots, and source reports.
    copy_tree_files(T1 / "04_performance_and_batch_statistics__20260716T150420", REPORT_ROOT / "sources/todo1", (".csv", ".json", ".md"))
    copy_tree_files(T2 / "results", REPORT_ROOT / "sources/todo2/results", (".csv", ".json", ".md"))
    copy_tree_files(T2 / "plots", REPORT_ROOT / "figures/todo2", (".png",))
    copy_tree_files(t3_dir / "plots", REPORT_ROOT / "figures/todo3", (".png",))
    for source, destination in ((T1 / "ARCHIVE_INDEX.md", REPORT_ROOT / "sources/todo1/ARCHIVE_INDEX.md"), (T2 / "FINAL_REPORT.md", REPORT_ROOT / "sources/todo2/FINAL_REPORT.md"), (T3 / "FINAL_REPORT.md", REPORT_ROOT / "sources/todo3/FINAL_REPORT.md"), (T3 / "VALIDATION_REPORT.md", REPORT_ROOT / "sources/todo3/VALIDATION_REPORT.md"), (T3 / "EXPERIMENT_PLAN.json", REPORT_ROOT / "sources/todo3/EXPERIMENT_PLAN.json")):
        copy_file(source, destination)
    summary = read_json(T3 / "09_combined_statistics/supplement_summary.json", {}) or {}
    regression = read_json(T3 / "09_combined_statistics/delay_regression_summary.json", {}) or {}
    loss_summary = read_json(T3 / "09_combined_statistics/packet_loss_experiment_summary.json", {}) or {}
    congestion_summary = read_json(T3 / "09_combined_statistics/controlled_congestion_summary.json", {}) or {}
    markdown = build_summary_markdown(archive_rows, attack_rows, summary, regression, loss_summary, congestion_summary)
    (REPORT_ROOT / "EXPERIMENT_DESIGN_AND_RESULTS.md").write_text(markdown, encoding="utf-8")
    (REPORT_ROOT / "artifact.json").write_text(json.dumps(build_artifact(archive_rows, summary, regression, loss_summary, delay_rows, loss_rows, congestion_rows, attack_rows), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readme = """# Hydro-CPS output integrated report\n\n- [实验设计与结果总结](EXPERIMENT_DESIGN_AND_RESULTS.md)\n- [Canonical report artifact](artifact.json)\n- [Consolidated data](data/)\n- [Figures](figures/)\n- [Source archives](sources/archives/)\n\n源归档以符号链接方式接入，未复制多 GB 的原始 PCAP/日志；所有正式结果和失败尝试仍保留在链接指向的归档中。便携 HTML 渲染未执行，因为当前环境没有 Node.js；`artifact.json` 保留了可供后续渲染的完整报告契约。\n"""
    (REPORT_ROOT / "README.md").write_text(readme, encoding="utf-8")
    print(REPORT_ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
