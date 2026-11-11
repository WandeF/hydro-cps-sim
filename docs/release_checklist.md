# Hydro-CPS-Sim V1.0 检查清单

检查日期：2026-11-11（Asia/Shanghai）。工作分支：`main`。版本文件：`VERSION`，内容为 `1.0`。

## 功能范围

V1.0 包括 C-Town 水网仿真、PLC ST 生成与编译、OpenPLC 运行、ns-3 网络、SCADA Modbus 通信、文件标记同步、已有 MITM/UDP DoS/PLC 逻辑注入实验，以及 CSV/JSON 结果导出。

简洁模式通过 `--log-level concise` 或 YAML `log_level: concise` / `log_level: 简洁` 启用，显示初始化、仿真和导出进度。命令行参数优先。详细终端输出保存在 `logs/run_all-*.log`。运行 CSV 采用跨进程文件锁保护表头扩展与追加写入。

## 版本文件

- 修改：`README.md`、`.gitignore`、`scripts/run_all.sh`、`src/run/config_info.py`、`src/io/csv.py`、`examples/c_town/README_attack_mitm.md`。
- 新增程序：`src/run/prepare_config.py`、`src/run/progress.py`。
- 新增版本与依赖说明：`VERSION`、`THIRD_PARTY_NOTICES.md`。
- 新增测试：`tests/test_release_preparation.py`、`tests/test_csv_concurrency.py`、`tests/test_progress.py`。
- 新增文档：`docs/architecture.md`、`installation.md`、`quickstart.md`、`configuration.md`、`experiments.md`、`outputs.md`、`troubleshooting.md`、`repository_audit.md`、本清单。
- 删除：`src/command_line.py`、`README.local.bak`、`.gitignore.local.bak`。
- 本地申请材料：`deliverables/soft_copyright/`，已由 `.gitignore` 排除。

配置副本工具只替换五个顶层路径，保留实验参数并检查外部文件。进度显示读取运行程序输出的阶段和周期完成消息。水力算法、控制规则、寄存器映射、攻击参数和导出表字段保持不变。

## 检查命令

使用现有 Conda `hydro-cps` 环境：

| 命令 | 结果 |
| --- | --- |
| `bash -n install.sh` | 通过 |
| `bash -n scripts/run_all.sh` | 通过 |
| `bash install.sh --dry-run` | 通过 |
| `bash scripts/run_all.sh --help` | 通过 |
| `python -X pycache_prefix=output/v1-verification-pycache -m compileall -q src` | 通过 |
| `python -m pip check` | 通过 |
| `python -m unittest discover -s tests -v` | 30 项通过 |

测试覆盖配置参数保留、ST 与网络代码生成、攻击目标解析、结果字段、CSV 并发写入、终端进度、文件重定向、日志保存和中断清理。测试日志按运行批次归档。

## 完整运行检查

在现有 Ubuntu 环境中，七份 C-Town 配置通过 `scripts/run_all.sh --log-level concise` 完成生成、编译、启动、闭环及导出。所有配置保留原实验参数，并使用独立的配置副本和输出目录。

| 配置 | 完成控制周期 | 物理数据行 | 攻击事件行 | 结果 |
| --- | --- | --- | --- | --- |
| `config.yaml` | 99 | 101 | 0 | 通过 |
| `config_dos_plc2_single.yaml` | 99 | 101 | 96 | 通过 |
| `config_dos_plc2_three_bots.yaml` | 99 | 101 | 1422 | 通过 |
| `config_mitm_composite_plc7_plc9.yaml` | 99 | 101 | 42 | 通过 |
| `config_mitm_plc4.yaml` | 99 | 101 | 21 | 通过 |
| `config_mitm_plc9.yaml` | 99 | 101 | 21 | 通过 |
| `config_openplc_logic_plc4.yaml` | 99 | 101 | 2 | 通过 |

每个场景均导出九张结果表。检查覆盖控制周期与物理状态索引、攻击开启/停止记录、MITM 值修改、DoS 发送统计，以及 PLC 逻辑注入后的 ST 恢复。`iterations: 100` 对应 99 个控制周期和 101 行物理数据。

另以本地 YAML `log_level: 简洁` 执行无攻击场景，99 个控制周期和结果检查通过，未传入 `--log-level` 参数。

完整运行检查日期为 2026-11-11。每个场景的日志、结果表和截图按独立运行目录归档。

## 发布范围

公开版本包含项目源码、安装和运行入口、示例配置、历史实验数据、自动化测试及使用文档。申请表、操作说明书及其中的个人或单位资料保存在本地申请材料目录，该目录由 Git 忽略规则排除。

版本使用 `v1.0` 标签标识，对应提交可在 [GitHub 版本页面](https://github.com/WandeF/hydro-cps-sim/releases/tag/v1.0) 查看。发布说明见 [V1.0 版本说明](release_notes_v1.0.md)。

## 待完成检查

- 全新系统安装尚未执行；运行验证使用现有 Ubuntu 24.04.2 环境。
- DHALSIM-epynet 的许可证声明、EPANET 二进制来源、OpenPLC 内含组件和 fork 修改归属待核实，详见[第三方依赖说明](../THIRD_PARTY_NOTICES.md)。
- 项目级许可证及自主源码范围由权利人确认。
