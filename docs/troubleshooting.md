# Hydro-CPS-Sim V1.0 常见问题

| 现象 | 排查与处理 |
| --- | --- |
| `No module named yaml/pymodbus/epynet` | 激活安装环境，检查 `command -v python` 和 `python -m pip check`；必要时将 `PYTHON_BIN` 设置为该解释器的绝对路径 |
| OpenPLC compiler / ns-3 launcher not found | 核对依赖目录，使用 `prepare_config --dependency-parent` 创建配置副本，并确认依赖已完成编译 |
| 原配置含其他机器的路径 | 使用配置副本工具更新路径；自定义水网模型通过 `--inp-file` 指定 |
| 输出目录非空，副本工具拒绝执行 | 归档已有结果，使用新的 `--destination` 和 `--output` 路径 |
| `Another Hydro-CPS run` | 等待占用共享构建目录的运行或安装进程结束；保留运行期间的锁文件 |
| namespace/TAP 初始化失败 | 查看终端输出及生成的 `network.sh`，检查 sudo、iproute2、设备名称和网络管理权限 |
| PLC 编译或启动失败 | 查看 `logs/` 中的编译和运行日志，检查生成的 ST、OpenPLC 脚本及动态库 |
| ns-3 未就绪 | 查看 `logs/ns3_build.log` 和 `logs/ns3_network.log`，检查编译、网络连接和就绪消息；入口等待时间为 30 秒 |
| SCADA 超时 | 核对 namespace、IP 地址、PLC 的 502 端口、攻击窗口及 `scada_timeout_events.csv` |
| EPANET 初始化失败 | 核对 INP 文件路径和内容、epynet 安装情况及 EPANET 原生库加载错误 |
| 导出失败或结果表为空 | 检查退出码及原始 JSONL/JSON 记录是否完整；无攻击事件时，事件表只包含表头 |
| 未知命令行参数 | 使用 `bash scripts/run_all.sh --help` 查看参数及格式 |

重新运行会清理所选输出目录中的旧运行产物。先归档日志和数据，再使用新的输出目录运行；已有记录可按[输出说明](outputs.md)单独导出。
