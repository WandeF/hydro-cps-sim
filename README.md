# Hydro-CPS-Sim V1.0

水网信息物理系统跨层安全仿真软件（Hydro-CPS-Sim）是面向水网信息物理系统（Water CPS）的跨层安全联合仿真软件。通过 OpenPLC 执行控制程序、ns-3 承载通信网络、DHALSIM-epynet/EPANET 计算水力状态，由 Python 协调闭环运行并导出实验记录。

## 核心功能

- 从 YAML 生成并校验 PLC Structured Text（ST）程序、Linux 网络初始化脚本和 ns-3 拓扑代码。
- SCADA 通过 Modbus/TCP 轮询传感数据并转发跨 PLC 控制依赖。
- 按迭代同步本地传感写入、SCADA 下发、执行器读取和水力推进。
- 配置已有 Modbus MITM、UDP DoS、OpenPLC 逻辑注入场景及多场景调度。
- 导出水力状态、执行器、SCADA 观测、攻击事件与周期耗时 CSV。
- 简洁日志模式用进度条显示初始化、仿真周期和结果导出，详细日志独立保存。

## 软件架构

`YAML → ST/网络生成 → OpenPLC + ns-3 → 闭环协调器 ↔ DHALSIM-epynet → CSV/JSON`

物理状态经 PLC 适配器写入寄存器；SCADA 读取并分发依赖；PLC 线圈输出驱动下一步水力状态。详见[架构说明](docs/architecture.md)。

## 环境要求与安装

支持的安装环境为 Ubuntu 22.04/24.04 x86_64 和 Conda Python 3.10，安装时需要网络连接及 sudo 权限。外部组件版本由 `install.sh` 固定，Python 依赖列于 `requirements.txt`。

```bash
bash install.sh --help
bash install.sh --dry-run
# 确认安装计划后，在普通用户终端执行：
bash install.sh
conda activate hydro-cps
```

安装脚本会安装系统软件包、编译依赖，并在备份 C-Town 配置后更新其中的五个路径字段。已有可用环境可直接创建本机配置副本，详见[安装说明](docs/installation.md)。

## 快速开始

已完成安装并确认原配置路径有效时：

```bash
bash scripts/run_all.sh examples/c_town/config.yaml
```

为保留原实验配置与已有结果，推荐使用独立副本：

```bash
python -m src.run.prepare_config --config examples/c_town/config.yaml \
  --destination local-configs/baseline.yaml --output output/v1-baseline
bash scripts/run_all.sh local-configs/baseline.yaml --log-level concise
```

工具默认依赖位于仓库父目录，可用 `--dependency-parent /path/to/dependencies` 指定。完整流程见[快速开始](docs/quickstart.md)。运行器会清理所选输出目录中的旧运行产物，重复实验应使用新的配置副本和输出目录。

## 实验场景与输出

七份 C-Town 配置分别用于无攻击实验、三个 MITM 实验、两个 DoS 实验和一个 PLC 逻辑注入实验。各配置对应的目标及触发窗口见[实验说明](docs/experiments.md)。

主要结果位于 `<output_path>/reports/csv/`，原始记录位于 `runtime/`，流程日志位于 `logs/`。字段与初始化行含义见[输出说明](docs/outputs.md)。

## 项目目录

| 路径 | 用途 |
| --- | --- |
| `src/` | 配置、生成器、通信适配、攻击调度、物理适配与闭环运行 |
| `scripts/run_all.sh` | 完整运行入口 |
| `install.sh` | 固定依赖版本的安装器 |
| `examples/c_town/` | 水网模型、实验 YAML 与历史实验数据 |
| `tests/` | 安装预检查、运行入口及离线回归测试 |
| `docs/` | 安装、使用、配置和运行检查文档 |
| `VERSION` | 唯一机器可读版本号 `1.0`，展示为 V1.0 |

## 第三方依赖与版本

V1.0 对应 `main` 分支的水网联合仿真功能。版本号保存在 `VERSION` 中，当前发布状态见[检查清单](docs/release_checklist.md)。

OpenPLC、ns-3、DHALSIM-epynet 和 EPANET 的来源及许可说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。项目级许可证与代码来源清单待权利人确认。

## 使用限制与验证

在隔离实验环境中运行网络和攻击场景，并避免并发使用同名网络命名空间或 TAP 设备。软件通过命令行操作，使用 DHALSIM-epynet 水力引擎和文件标记同步各进程。

```bash
bash -n install.sh
bash -n scripts/run_all.sh
python -X pycache_prefix=output/pycache -m compileall -q src
python -m unittest discover -s tests -v
```

运行检查结果见[检查清单](docs/release_checklist.md)，版本变更见[V1.0 版本说明](docs/release_notes_v1.0.md)。另见[配置说明](docs/configuration.md)、[常见问题](docs/troubleshooting.md)。
