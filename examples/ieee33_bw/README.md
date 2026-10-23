# IEEE 33-bus 配电网实验

使用 Baran–Wu 33 节点标准电网，运行三台 OpenPLC、SCADA、ns-3 和 pandapower 的闭环实验。

电网为 12.66 kV，包含 33 个母线、37 条线路和 32 处负荷；32 条线路初始投入，5 条联络线初始停运。总负荷为 3.715 MW / 2.300 MVAr。模型由 `pandapower.networks.case33bw()` 加载，保持公开模型的阻抗、源电压、负荷和联络状态。

## 控制与测量

| PLC | 本地测量 | 控制 |
| --- | --- | --- |
| PLC1 | Bus 2 电压 | 向 SCADA 提供 `V_BUS02` |
| PLC2 | Bus 18、Bus 33 电压 | 远端监测 |
| PLC3 | Bus 24 电压、负荷功率、网侧功率、总线损和供电功率 | 通过 SCADA 接收 PLC1 电压，控制 `LOAD24_ENABLE` |

`LOAD24_ENABLE=True` 投入 Bus 24 原有的 0.420 MW / 0.200 MVAr 负荷；`False` 切除该负荷。Bus 24 本身保持带电，负荷功率为零并不表示母线电压为零。

实验控制规则：`V_BUS02 < 0.95 pu` 切除负荷，`V_BUS02 > 1.03 pu` 恢复，两者之间保持当前状态。正常值约 0.99703 pu，不会误切除。这组阈值用于验证控制链路，不是原论文的保护定值。

## 运行

使用已安装 `requirements-power.txt` 的 Python 环境。纯求解与接口检查不需要 root：

```bash
python scripts/ieee33_reference.py
python -m src.physics.contract --config examples/ieee33_bw/config_baseline.yaml
```

运行完整对照需要项目的 OpenPLC、ns-3 和 namespace 环境：

```bash
PYTHON_BIN="$PWD/output/modular-physics-venv/bin/python" \
  bash scripts/run_ieee33_experiments.sh
python scripts/check_ieee33_results.py
```

检查脚本也需要 pandapower；使用安装电网依赖的同一个 Python。完整脚本依次运行 10 轮快速 baseline，以及各 100 轮 baseline、透明代理、MITM。已有 IEEE33 结果移入 `output/ieee33-archives/`，其他模型的结果目录不会改变。

| 配置 | 行为 |
| --- | --- |
| `config_baseline.yaml` | 正常闭环，包含相同网络位置的攻击者节点 |
| `config_transparent.yaml` | 20–40 轮通过同一代理，电压增量为 0 |
| `config_mitm.yaml` | 20–40 轮篡改 SCADA→PLC3 的 `PLC1_V_BUS02` FC16 写入，增量 −0.10 pu |

篡改从第 20 轮开始，正常电压会降至约 0.89703 pu。PLC3 第 20 轮切除负荷，第 21 轮电网快照显示负荷功率为 0。攻击窗口结束后，电压仍低于恢复阈值，负荷继续保持切除状态。

## 结果

结果保存在 `output/ieee33_bw/`，按 `native`、`quick`、`baseline`、`transparent`、`mitm` 隔离：

- `native/benchmark_reference.json`：源模型版本、完整输入表、双重 SHA-256、基准潮流、节点电压和线路结果。
- `native/bus_id_map.json`：论文节点号、源名称和实际表索引的映射。
- 各场景的 `bus_voltage.csv`、`physical_metrics.csv`、`control_trace.csv`：电网状态、功率和跨层控制轨迹。
- 各场景的 `runtime/json/` 与 `reports/csv/`：原始快照、实际 PLC 线圈、SCADA 轮询/写入、攻击事件和质量审计。
- `comparison.csv`、`validation.json`：电压/线损差异、首次偏离轮次和实验验收。

`control_trace.csv` 把第 k 轮写入与第 k+1 轮轮询读到的 PLC3 寄存器对齐。读回发生在下一次下发之前；最后一轮没有后续轮询，明确留空。客户端确认写入值与代理篡改后的接收值分别记录。网络缓存和信号源时间保留在原始质量记录中；本次三组运行均要求轮询成功且不使用缓存。

供电功率取 `res_load.p_mw` 总和；未供电功率取原负荷需求（`p_mw × scaling`）减实际供电功率，包括主动脱扣和拓扑失电。累计未供电电量用每次求解返回的准稳态功率，对该命令覆盖的 `input_effective_time_s` 至 `sim_time` 区间积分；初始化快照不占时间，终点求解覆盖最后一个区间。该场景每秒重复求解 AC 准稳态潮流，不模拟机电或电磁暂态。

可选图表：

```bash
python -m pip install -r requirements-power-plots.txt
python scripts/plot_ieee33_results.py
```

生成 `figures/` 下的电气拓扑、基准电压曲线和攻击—控制—物理事件时序图（SVG/PNG）。数据来源及派生设置见 [references.md](references.md)，实测数值见[验证记录](../../docs/ieee33-validation.md)。
