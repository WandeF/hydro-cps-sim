# C-Town MITM 实验说明

MITM 场景通过 Modbus/TCP 代理转发 SCADA 与 PLC 的通信，并在配置的控制周期窗口内修改指定变量。

## 场景配置

| 配置文件 | 目标 PLC | 变量 | 修改位置 | 控制周期窗口 | 代理端口 |
| --- | --- | --- | --- | --- | --- |
| `config_mitm_plc4.yaml` | PLC4 | `PLC8_T7` | 功能码 16 的写入请求 | 20–40 | 15030 |
| `config_mitm_plc9.yaml` | PLC8 | `PLC8_T7` | 功能码 3 的读取响应 | 20–40 | 15020 |
| `config_mitm_composite_plc7_plc9.yaml` | PLC6 | `PLC6_T5` | 功能码 3 的读取响应 | 20–40 | 15040 |
| 同上 | PLC8 | `PLC8_T7` | 功能码 3 的读取响应 | 30–50 | 15050 |

上述规则均将匹配值设置为 `5.2`。文件名沿用历史命名，当前目标以 YAML 中的 `target` 和 `variable` 为准。PLC4 场景修改 SCADA 下发给 PLC4 的跨 PLC 依赖变量；PLC8 场景修改 SCADA 读取的水箱观测值。

## 网络与触发流程

配置中的攻击端点由网络生成器写入 `network.sh` 和 `ns3_network.cc`，通过 Linux namespace、TAP 和 ns-3 TapBridge 接入实验网络。

MITM 代理和 DNAT 规则在 SCADA 进程启动前建立。以 `config_mitm_plc9.yaml` 为例，SCADA 发往 PLC8 的 502 端口的流量被重定向到攻击端点的 15020 端口，代理再连接真实 PLC8。

协调器按控制周期更新攻击状态。第 1–19 周期代理正常转发，第 20–40 周期修改匹配响应，第 41 周期起恢复正常转发；运行结束时清理代理和重定向规则。

`trigger` 控制场景的迭代窗口。`rules[].window` 控制代理启动后的实际时间窗口，示例使用 `start_after_sec: 0.0`。规则支持 `set`、`add`/`bias` 和 `multiply`/`scale` 操作。

## 运行实验

在仓库根目录激活安装环境，创建独立的本机配置和输出目录：

```bash
python -m src.run.prepare_config --config examples/c_town/config_mitm_plc9.yaml \
  --destination local-configs/mitm-plc8.yaml --output output/v1-mitm-plc8
bash scripts/run_all.sh local-configs/mitm-plc8.yaml --log-level concise
```

其他场景使用表中对应的配置文件，并分别指定新的配置副本和输出目录。保留原配置的迭代数和攻击参数，运行至攻击窗口结束。

## 查看结果

在 `<output_path>/reports/csv/` 中检查：

- `attack_schedule.csv`：代理启动、逐周期攻击状态及结束记录；结束记录的控制周期字段为空。
- `attack_events.csv`：匹配变量、修改前后的值及事件记录。
- 水力和 SCADA 结果表：攻击窗口前后观测值及控制结果的变化。

比较无攻击和 MITM 实验时，使用相同的水网模型、初始状态及控制参数，按控制周期关联结果。字段和初始化记录的含义见[输出说明](../../docs/outputs.md)，完整场景运行结果见[检查清单](../../docs/release_checklist.md)。
