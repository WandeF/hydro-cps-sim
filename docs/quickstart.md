# Hydro-CPS-Sim V1.0 快速开始

在仓库根目录运行，先按 [安装说明](installation.md) 准备依赖并激活 `hydro-cps` 环境。

## 创建本机配置并运行

```bash
python -m src.run.prepare_config --version
python -m src.run.prepare_config --config examples/c_town/config.yaml \
  --destination local-configs/baseline.yaml --output output/v1-baseline
bash scripts/run_all.sh local-configs/baseline.yaml
```

默认依赖位于仓库父目录的 `OpenPLC_v3` 和 `ns-3-dev-git`；可加 `--dependency-parent /path/to/dependencies`。INP 默认取原配置旁同名模型，可用 `--inp-file` 明确指定。副本只替换五个顶层路径，保留注释和实验参数，检查 INP、OpenPLC 编译脚本及 ns-3 launcher 是否存在。原生组件的编译和运行由完整仿真流程检查。

工具要求目标配置文件尚不存在，输出目录为空或尚不存在，并会创建指定输出目录。重复实验时使用新的 `--destination` 和 `--output` 路径。`local-configs/` 和 `output/` 已加入 Git 忽略规则。

## 原配置运行与参数

确认原配置所有路径有效、旧输出已归档后，可直接运行：

```bash
bash scripts/run_all.sh examples/c_town/config.yaml
bash scripts/run_all.sh --help
```

支持 `--config`、`--iterations`、`--logic-wait`（默认 0.3 秒）、`--poll-interval`（默认 0.005 秒）、`--scada-modbus-workers`（默认 8）和 `--log-level`。复现实验时保留 YAML 中的迭代数和默认运行参数。`--iterations 2` 可用于短时启动检查；验证攻击时需要运行至相应触发窗口结束。

脚本自动申请 sudo 并清理所选输出目录的 `runtime/`、`reports/`、`st/`、`plcs/`；还会重建配置中的 namespace/接口。勿与同名拓扑并发。ns-3 和 OpenPLC 共享构建目录由运行锁保护。

详细模式完成时显示 `[DONE] Results: .../reports/csv`；简洁模式显示完成状态和详细日志路径。运行结束后检查退出码、CSV 完整性及周期数。程序退出时清理运行进程并尝试导出已生成的数据，详见[输出说明](outputs.md)。

## 简洁日志与进度条

```bash
bash scripts/run_all.sh local-configs/baseline.yaml --log-level concise
```

也可在本地 YAML 中设置 `log_level: concise` 或 `log_level: 简洁`。命令行 `--log-level` 优先于 YAML；`--log-level debug` 显示详细终端输出。原有案例的设置保持不变。

简洁模式在交互式终端显示三个进度条：初始化按十个准备阶段更新，仿真按完成的控制周期更新，导出在结果写入完成后更新。状态行显示当前操作和已用时间。`iterations: 100` 的仿真进度为 `0/99` 至 `99/99`。

简洁模式的完整运行输出写入 `<output_path>/logs/run_all-*.log`，终端显示实际日志路径。将终端输出重定向到文件时，程序按阶段和约 10% 的进度间隔输出纯文本记录。运行失败时显示退出码和日志末尾；按 Ctrl+C 可停止运行并等待清理。

截图时，建议将终端宽度设置为至少 80 列，保留软件名称、配置名、三个进度条和状态行。详细日志路径可用于查阅运行过程。
