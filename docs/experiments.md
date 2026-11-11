# Hydro-CPS-Sim V1.0 实验场景

以下为实际存在的 C-Town 配置。文件名沿用历史命名，当前攻击目标以 YAML 内容及下表为准。

| examples/c_town/ 下配置 | YAML 中的场景与窗口 |
| --- | --- |
| `config.yaml` | 无攻击实验 |
| `config_dos_plc2_single.yaml` | udp_dos → PLC2，迭代 5–90 |
| `config_dos_plc2_three_bots.yaml` | 三个 UDP 攻击源 → PLC2，迭代 5–90 |
| `config_mitm_composite_plc7_plc9.yaml` | modbus_mitm → PLC6，迭代 20–40；modbus_mitm → PLC8，迭代 30–50 |
| `config_mitm_plc4.yaml` | modbus_mitm → PLC4，迭代 20–40 |
| `config_mitm_plc9.yaml` | modbus_mitm → PLC8，迭代 20–40 |
| `config_openplc_logic_plc4.yaml` | openplc_logic_injection → PLC4，迭代 20–90 |

复合 MITM 配置包含两个触发窗口重叠的场景。单源和三源 DoS 使用 UDP 发送模块。PLC 逻辑注入示例使用 `threshold_shift`；`force_actuator` 和 `invert_condition` 分支的独立运行检查待补充。MITM 的目标变量和转发流程见[案例说明](../examples/c_town/README_attack_mitm.md)。

## 运行已有配置

先确认配置路径和输出目录，再逐个运行（每个命令是独立实验，不要同时执行）：

```bash
bash scripts/run_all.sh examples/c_town/config.yaml
bash scripts/run_all.sh examples/c_town/config_mitm_plc4.yaml
bash scripts/run_all.sh examples/c_town/config_mitm_plc9.yaml
bash scripts/run_all.sh examples/c_town/config_mitm_composite_plc7_plc9.yaml
bash scripts/run_all.sh examples/c_town/config_dos_plc2_single.yaml
bash scripts/run_all.sh examples/c_town/config_dos_plc2_three_bots.yaml
bash scripts/run_all.sh examples/c_town/config_openplc_logic_plc4.yaml
```

这些原始配置可能共享输出目录。为避免覆盖，推荐每次先按下例制作独立副本，再传入运行器：

```bash
python -m src.run.prepare_config --config examples/c_town/config_mitm_plc4.yaml \
  --destination local-configs/mitm-plc4.yaml --output output/v1-mitm-plc4
bash scripts/run_all.sh local-configs/mitm-plc4.yaml
```

其他场景分别使用新的 `--destination` 和 `--output` 路径。复现实验时保留原迭代数、触发窗口和攻击参数，并运行至攻击窗口结束。

完成后检查退出码、`attack_schedule.csv` 的开启和关闭事件、`attack_events.csv` 的修改或发送记录，以及水力和 SCADA 结果。预期事件缺失时，依次检查是否进入触发窗口、目标通信是否发生及运行日志是否报错。七份配置的完整运行结果见[检查清单](release_checklist.md)。
