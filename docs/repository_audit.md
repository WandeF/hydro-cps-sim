# Hydro-CPS-Sim V1.0 仓库检查

## 工程组成

| 目录或文件 | 职责 |
| --- | --- |
| `scripts/run_all.sh` | 环境检查、生成、编译、启动、运行和导出 |
| `src/run/` | 配置路径、本机配置副本和终端进度 |
| `src/control/` | PLC ST 生成校验、编译与启动 |
| `src/network/` | Linux 网络脚本和 ns-3 拓扑代码生成 |
| `src/core/` | YAML、运行对象与寄存器映射 |
| `src/comm/`、`src/plc/`、`src/scada/` | Modbus、PLC 适配和 SCADA 进程 |
| `src/physics/`、`src/runtime/`、`src/sync/` | 水力适配、闭环协调和进程同步 |
| `src/attack/` | 已有攻击场景的执行与调度 |
| `src/io/` | CSV/JSON 记录和结果导出 |
| `src/test/` | 人工 PLC 诊断脚本 |
| `tests/` | 自动化回归测试 |

## 配置与数据管理

原始 C-Town YAML、INP 和 `baseline/csv/` 历史数据保留。通过 `prepare_config` 创建独立运行副本，保留已有结果。`install.sh` 的路径更新与备份行为见[安装说明](installation.md)。

`output/`、案例输出目录、`local-configs/`、本地软著材料及 `hydro.zip` 已忽略。实验样本 CSV 继续纳入版本管理。运行 CSV 的表头扩展和追加写入使用进程间文件锁，生成的锁文件保存在对应运行目录。

## 文档与依赖

README 提供当前可用命令，详细文档按架构、安装、配置、实验、输出和故障处理组织。依赖源码与生成程序单独管理，候选项目源码清单保存在本地申请材料中。

许可证状态见[第三方依赖说明](../THIRD_PARTY_NOTICES.md)。正式公开前，需确认个人路径处理方式、代码来源、依赖声明和申请资料范围。技术内容的公开时间及专利关联信息由项目负责人结合申请资料确认。
