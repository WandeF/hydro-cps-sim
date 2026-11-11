# Hydro-CPS-Sim V1.0 配置说明

示例配置位于 `examples/c_town/*.yaml`。`src/core/config.py` 加载 YAML 并构建运行对象，`src/run/config_info.py` 为运行脚本读取路径和参数。配置检查分布在各功能模块中；编写配置时应使用下表字段及现有示例。

| 配置项 | 实际用途 |
| --- | --- |
| `root_path` | 外部依赖根路径信息；副本工具同步迁移 |
| `inp_file` | EPANET 水网模型文件 |
| `output_path` | ST、网络脚本、运行记录和导出结果根目录 |
| `openplc_path` / `ns3_path` | 外部组件源码/构建目录 |
| `log_level` | `concise` 或 `简洁` 启用进度条，`debug` 显示详细输出；命令行参数优先 |
| `iterations` | 最终物理状态索引；`dhalsim` 初始化方式下执行 `iterations - 1` 个控制周期，可用命令行参数覆盖 |
| `initial_tank_values` | 配置初始水箱状态 |
| `actuators[].name/initial_state` | 执行器与初始开闭状态 |
| `plcs[].name/sensors/actuators/controls` | PLC 归属、输入输出、控制规则 |
| `controls[].actuator/dependant/type/value/action` | 生成 ST 所需的依赖变量、比较阈值和动作 |
| `physics.inp_controls/step_order/init_flag` | INP 控制处理、EPANET 推进顺序和初始化标志 |
| `time[].duration/hydraulic_timestep` | EPANET 时间参数，列表保留枚举名与数值 |
| `network.nodes` | routers、switches、endpoints，包括 namespace 和 TAP |
| `network.backbone_links/lans` | 网络连接、速率、延迟、子网、IP 地址与接口 |
| `network.scheduler/routing/pcap/ascii_trace` | 生成网络运行与跟踪配置 |
| `attacks.enabled/scenarios` | 场景启用与攻击列表 |
| `scenarios[].type/trigger` | 已实现攻击类型及触发窗口 |

MITM 使用 `intercept` 与 `rules`；DoS 使用 `attacker`、`target`、`traffic`；PLC 逻辑注入使用 `target`、`injection`。复现实验时应保留对应 YAML 的阈值、攻击强度和触发窗口。`network.attack_points` 保存攻击位置的描述信息，当前程序未读取此字段；`src.attack.launch` 根据 `attacks` 执行攻击任务。

## 路径迁移

示例中的绝对路径需要适配本机环境。使用 `python -m src.run.prepare_config` 创建配置副本，可统一设置五个路径字段并保留实验参数。命令见[快速开始](quickstart.md)。

运行前应检查 INP 文件、PLC 端点与 ST 名称、网络命名空间、LAN 内 IP 地址以及攻击目标。七份示例的配置解析、代码生成和完整运行检查结果见[检查清单](release_checklist.md)。
