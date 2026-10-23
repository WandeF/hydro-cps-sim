# Hydro-CPS-Sim

**面向供水系统安全研究的联合仿真平台。**

[中文文档](https://wandef.github.io/zh/latest/) · [English Documentation](https://wandef.github.io/en/latest/) · [English README](README.md)

Hydro-CPS-Sim 将 EPANET 水力仿真、OpenPLC 控制程序、Modbus TCP 通信和 ns-3 网络连接在同一闭环中。你可以配置水网、PLC 控制规则和通信路径，运行攻击实验，并观察传感器数据、泵阀动作和水力状态的变化。

## 主要功能

- **闭环仿真**：将泵阀控制结果反馈给水力模型，更新水位、压力和流量。
- **PLC 执行**：根据配置生成结构化文本（ST）程序，运行独立的 OpenPLC 实例。
- **网络配置**：通过 Linux network namespace 和 ns-3 连接 PLC、SCADA 与攻击节点。
- **攻击实验**：提供 Modbus MITM、UDP DoS 和 PLC 逻辑注入，可设置目标与生效窗口。
- **数据导出**：保存水力状态、SCADA 观测、执行器状态、攻击事件和运行耗时。

组件与闭环流程见[系统架构](https://wandef.github.io/zh/latest/architecture.html)。

## 环境要求

- Ubuntu 22.04 或 24.04，x86_64。
- 已安装 Conda，并可在当前终端中使用。
- 可联网安装软件包和获取依赖仓库。
- 使用具有 `sudo` 权限的普通用户，完成安装与网络配置。

安装程序创建或复用名为 `hydro-cps` 的 Python 3.10 环境，并安装 OpenPLC 命令行运行时、ns-3 和 DHALSIM-epynet，不包含 OpenPLC Web 管理服务。

## 安装

```bash
git clone https://github.com/WandeF/hydro-cps-sim.git
cd hydro-cps-sim

# 查看安装计划，不修改系统。
bash install.sh --dry-run

# 安装依赖并编译运行环境。
bash install.sh
conda activate hydro-cps
```

第三方依赖默认安装在项目的母目录。可以使用 `--parent` 指定独立的依赖目录，通过 `--env` 和 `--jobs` 修改环境名称与编译并行数。完整选项见 `bash install.sh --help`。

安装程序会更新 C-Town YAML 配置中的路径，并将修改前的文件保存到 `output/install-backups/`。移动项目或依赖目录后，请更新配置路径。详细步骤见[安装与环境](https://wandef.github.io/zh/latest/installation.html)。

## 快速开始

安装完成后，运行 C-Town 无攻击基线：

```bash
bash scripts/run_all.sh --config examples/c_town/config.yaml --logic-wait 0.3
```

也可以先运行 10 轮：

```bash
bash scripts/run_all.sh --iterations 10 --logic-wait 0.3
```

在已激活的环境中使用普通用户启动，脚本会按需请求 `sudo`。配置文件、运行轮数、轮询间隔和 SCADA 工作线程数等选项见 `bash scripts/run_all.sh --help`。

**再次运行前保存结果：** 每次运行会清理配置输出目录中的 `runtime/`、`reports/`、`st/` 和 `plcs/`。请先将需要保留的结果复制到独立目录，再开始下一个实验。

## 攻击实验

C-Town 提供以下示例配置：

| 配置文件 | 场景 |
| --- | --- |
| [`config.yaml`](examples/c_town/config.yaml) | 无攻击基线 |
| [`config_mitm_plc4.yaml`](examples/c_town/config_mitm_plc4.yaml) | 修改 SCADA 下发至 PLC4 的 T7 数据 |
| [`config_mitm_plc9.yaml`](examples/c_town/config_mitm_plc9.yaml) | 修改 PLC8 的 T7 响应 |
| [`config_mitm_composite_plc7_plc9.yaml`](examples/c_town/config_mitm_composite_plc7_plc9.yaml) | 联合修改 T5 和 T7 响应 |
| [`config_dos_plc2_single.yaml`](examples/c_town/config_dos_plc2_single.yaml) | 单攻击节点对 PLC2 发起 UDP DoS |
| [`config_dos_plc2_three_bots.yaml`](examples/c_town/config_dos_plc2_three_bots.yaml) | 三个攻击节点对 PLC2 发起 UDP DoS |
| [`config_openplc_logic_plc4.yaml`](examples/c_town/config_openplc_logic_plc4.yaml) | 修改 PLC4 中 PU10 的控制阈值 |

使用 `--config` 选择场景：

```bash
bash scripts/run_all.sh --config examples/c_town/config_mitm_plc4.yaml --logic-wait 0.3
```

每个 YAML 包含相应的网络、控制和攻击设置。修改目标、参数与时间窗口的方法见[攻防实验](https://wandef.github.io/zh/latest/experiments.html)和[配置说明](https://wandef.github.io/zh/latest/configuration.html)。

## 实验结果

运行结束后，终端会显示结果位置。以下目录位于所选 YAML 的 `output_path` 下：

```text
<output_path>/
├── reports/csv/   # 水力、执行器、SCADA 和攻击数据
├── logs/          # 运行与网络日志
└── timing/        # 运行脚本各阶段耗时
```

可以先查看 `physics.csv`、`actuator_state.csv` 和 `scada_observed_*.csv`，比较基线与攻击运行。`attack_schedule.csv`、`attack_events.csv` 和 `scada_timeout_events.csv` 记录攻击与通信事件，`cycle_timing.csv` 记录闭环耗时。

## 模块化物理仿真与电网示例

UPSI 统一物理信号的类型、单位与时间信息，通过配置加载仿真器插件，原有 C-Town 配置继续使用 EPANET。详见 [UPSI 接口与配置](docs/upsi.md)、[第三方适配器 SDK](docs/adapter-sdk.md)、[电网示例](examples/power_feeder/README.md)、独立的[恒温箱示例](examples/thermal_box/README.md)和[验证记录](docs/upsi-validation.md)。

论文标准电网实验见 [IEEE 33-bus](examples/ieee33_bw/README.md) 和[验证记录](docs/ieee33-validation.md)。

使用 `python -m pip install -r requirements-power.txt` 安装可选依赖。在具有 sudo 权限的交互终端中，可运行基线、透明代理与 MITM 三组实验：

```bash
PYTHON_BIN="$(command -v python)" bash scripts/run_power_experiments.sh
```

## 项目结构

```text
examples/c_town/  水网模型与示例配置
examples/power_feeder/  电网模型与三组实验配置
examples/ieee33_bw/     IEEE 33 节点标准电网与 PLC 攻防实验
docs/            物理接口与验证记录
scripts/         仿真运行脚本
src/             水力、控制、SCADA、网络、攻击与数据导出模块
tests/           安装、运行与物理仿真回归测试
install.sh       依赖安装与运行环境编译
requirements.txt Python 依赖
```

## 测试与反馈

安装程序会运行 Python 测试。在已激活的环境中，也可以手动执行：

```bash
python -m unittest discover -s tests
```

遇到问题时，可以在 [GitHub Issues](https://github.com/WandeF/hydro-cps-sim/issues) 中反馈，并附上操作系统、源码版本、配置文件、启动命令和相关错误日志。

研究与发表信息见[论文与引用](https://wandef.github.io/zh/latest/publications.html)。
