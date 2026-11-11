# Hydro-CPS-Sim V1.0 版本说明

水网信息物理系统跨层安全仿真软件（Hydro-CPS-Sim V1.0）提供 C-Town 水网、PLC 控制、SCADA 通信和 ns-3 网络联合仿真，以及 MITM、UDP DoS 和 PLC 逻辑注入实验。

## 使用体验

- 简洁日志模式显示初始化、控制周期和结果导出的进度，并单独保存详细日志。命令行可使用 `--log-level concise`，YAML 可使用 `log_level: concise` 或 `log_level: 简洁`。
- 本机配置副本工具更新依赖、模型和输出路径，保留原有实验参数，并检查目标文件和输出目录。
- CSV 写入使用跨进程文件锁，协调表头扩展和记录追加。
- 使用文档覆盖安装、配置、实验、输出及故障处理，MITM 说明与当前配置对应。

## 验证

30 项自动化测试通过。七份 C-Town 配置分别完成 99 个控制周期，生成 101 行物理状态数据及九张结果表。检查涵盖 MITM 值修改、DoS 发送记录、PLC 逻辑注入和程序恢复。详见[检查清单](release_checklist.md)。

## 安装与运行

安装目标为 Ubuntu 22.04/24.04 x86_64、Conda Python 3.10。完整运行已在现有 Ubuntu 24.04.2 环境验证。

```bash
bash install.sh --dry-run
bash install.sh
conda activate hydro-cps
python -m src.run.prepare_config --config examples/c_town/config.yaml \
  --destination local-configs/baseline.yaml --output output/v1-baseline
bash scripts/run_all.sh local-configs/baseline.yaml --log-level concise
```

重复实验时使用新的配置副本和输出目录。安装行为见[安装说明](installation.md)，外部组件来源和许可证状态见[第三方依赖说明](../THIRD_PARTY_NOTICES.md)。
