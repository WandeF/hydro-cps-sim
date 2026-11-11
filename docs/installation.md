# Hydro-CPS-Sim V1.0 安装与环境验证

## 支持目标

安装目标为 Ubuntu 22.04/24.04 x86_64 和 Conda Python 3.10。安装过程需要编译工具、sudo 权限和网络连接；运行环境需要 Linux 网络命名空间、TAP 和 iptables。安装脚本部署 OpenPLC 命令行运行组件。

在仓库根目录查看计划：

```bash
bash install.sh --help
bash install.sh --dry-run
bash install.sh --parent /path/to/dependencies --env hydro-cps --jobs 2 --dry-run
```

`--dry-run` 显示安装计划。确认依赖目录和环境名称后，去掉该参数即可执行安装。

## 安装器行为

依赖来源及版本见 `install.sh` 中的 `EPYNET_URL/REV`、`OPENPLC_URL/REV` 和 `NS3_URL/REV`。脚本检查已有目录的 Git origin 和提交版本，兼容版本列于脚本中。可通过 `--parent` 指定独立目录。

安装脚本依次安装系统编译依赖、创建或复用 Conda 环境、安装 `requirements.txt` 中的包，以可编辑模式安装 DHALSIM-epynet，再编译 OpenPLC 原生库、运行程序和 ns-3 模块。复用环境时保留其中的其他软件包。

安装末段会对全部 `examples/c_town/*.yaml` 替换 `root_path`、`openplc_path`、`ns3_path`、`inp_file`、`output_path`，原文件备份在 `output/install-backups/<时间戳>/`。实验参数保持原值，但各配置会指向同一案例输出目录，运行前建议用 `prepare_config` 创建独立副本。已有依赖可用时无需为生成副本重跑安装器。

## 验证

```bash
conda activate hydro-cps
python --version
python -m pip check
python -c 'import yaml, pymodbus; from epynet.epanet2 import EPANET2; print("Python imports OK")'
python -m unittest discover -s tests -v
bash scripts/run_all.sh --help
```

上述命令检查 Python 环境及程序入口。随后按[快速开始](quickstart.md)执行完整仿真，检查原生组件编译、网络启动和结果导出。运行脚本会检查 Python 依赖、ns-3 启动器、OpenPLC 编译脚本及 `sudo`、`ip`、`flock`、`setsid`。

以普通用户启动 `run_all.sh`，由脚本调用 sudo。需要指定 Python 解释器时，设置环境变量 `PYTHON_BIN`。
