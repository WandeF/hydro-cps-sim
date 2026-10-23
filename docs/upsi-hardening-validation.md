# UPSI 可靠性加固验收记录

本轮基于 `720e6da` 加固现有 UPSI P0–P4，开发分支为 `fix/upsi-contract-hardening`。未修改 EPANET 求解算法、pandapower 模型行为、控制阈值或攻击规则；未推送远端。

验证日期：2026-10-23。工作区原有的 `examples/c_town/README_attack_mitm.md` 删除和未跟踪的 `hydro.zip` 保持原状，不纳入提交。

## 任务与实现

| 任务 | 变更与验收 |
| --- | --- |
| P0 快照完整性 | 移除 diagnostics 向顶层的任意合并；标准字段由 Session 独占。历史水网展示字段、电网收敛与孤岛字段使用显式白名单。冲突诊断保留在嵌套区。注入 `values=9999`、伪造时间/状态/元数据不能覆盖已校验值。返回值为独立副本。 |
| P1-A 质量传播 | 数值有效性、源头供电/物理状态、采样新鲜度和网络缓存新鲜度分别记录。PLC 写入、SCADA 轮询、跨 PLC 下发携带实际来源元数据。支持 pass_through、hold_last、fail_fast、skip_write，并记录原始/选择/确认写入值与各自源时间。 |
| P1-B 插件上下文 | 场景解析返回独立 Registry，保存在 BackendConfig 中，不写全局 `_BACKENDS`。保留程序式注册和旧 SDK 调用模式；推荐显式传递 registry。同名 external_demo 在顺序及并行场景中可使用不同工厂。内置名称、alias、非法来源和重复声明被拒绝。 |
| P1-C 故障契约 | 覆盖部分初始化失败、关闭失败、原始异常与清理异常同时出现、端口/类型/非有限/范围/输入应用错误、状态及时间错误。错误记录包含阶段、backend、iteration、异常类别、请求/最后有效时间与 UTC 时间。关闭失败不再被运行器默认为成功。 |
| P2 单位与时间 | 使用显式单位目录校验维度及 scale/offset。拒绝跨维度、错误系数和无上下文 pu 换算；保留单位一致的显式校准例外并记录理由。水柱压力换算要求密度和重力。默认测量时间严格相同；有能力声明且显式配置才启用有限异步年龄。TimeCoordinator 支持受限浮点容差及不可表示时步检查。 |
| P3 文档与脚本 | 更新 UPSI/SDK 文档，增加无需 root 的契约夹具、独立求解器入口、真实质量策略实验及检查脚本，完成本记录。 |

JSON 写入采用临时文件加原子替换，写入异常保留旧文件；ready marker 只在持久化成功后发布。CSV 头部扩展和追加使用跨进程文件锁，JSONL 追加也串行保护。

## 执行顺序与单元验证

1. 创建独立分支后，原有 **54 项基线测试通过**，没有基线单元失败。
2. 首先实施 P0，单独运行 `test_upsi.py`，**21 项通过**，包括新增的快照污染回归。
3. 再实施质量策略、插件上下文、故障及单位/时间增强，运行完整测试：

```bash
output/modular-physics-venv/bin/python -m unittest discover -s tests -v
/home/lzh/anaconda3/envs/hydro-cps/bin/python -m unittest discover -s tests -v
```

- 电力环境：**77 项通过**。
- 原水网环境：**63 项通过，14 项可选电力测试 skipped**。
- `pip check`、shell 语法、Git 空白和本地 Markdown 链接检查通过。

针对性夹具无需 root、OpenPLC 或 ns-3：

```bash
python -m unittest discover -s tests -p test_upsi_hardening.py -v
```

`src.physics.contract.exercise_backend()` 可被第三方 SDK 复用，检查重复初始化、输入应用、完整输出、时间推进并始终尝试关闭。测试包括临时安装元数据 entry point、场景并行隔离、并行 CSV 头扩展、原子 JSON 写入中断、质量策略非法/冲突、缺失值、超时缓存和跨 PLC 依赖。

## 三适配器独立运行

以下命令只调用真实物理后端，不启动控制或网络：

```bash
python -m src.physics.contract --config examples/c_town/config.yaml
python -m src.physics.contract --config examples/power_feeder/config_v2.yaml
python -m src.physics.contract --config examples/thermal_box/config.yaml
```

在电力环境中均通过：EPANET 4 个样本到 900 s；pandapower、thermal_box 各 4 个样本到 3 s。原水网环境缺少 pandapower 时，该命令明确输出 `status: skipped` 和依赖原因，不作为通过统计。独立求解器检查不替代完整网络实验。

## 真实闭环回归

已有结果先复制至 `output/upsi-hardening/before/`，然后执行：

```bash
PYTHON_BIN="$PWD/output/modular-physics-venv/bin/python" bash scripts/run_upsi_validation.sh
```

使用真实 OpenPLC、Linux namespace、SCADA、Modbus 和 ns-3。参考代码来自干净的 `4dd6b2be27899fb55ee9973cb81a5d58120f1a17` 归档，参考与当前水网结果写入独立目录，不使用原 C-Town 输出目录。

| C-Town `--iterations 100` 对照 | 行数 | 差异 |
| --- | ---: | ---: |
| `physics.csv` | 101 | 0 |
| `actuator_state.csv` | 99 | 0 |
| `scada_observed_key.csv` | 99 | 0 |

历史轮次约定保持不变：两行初始化及 99 个控制周期。原始物理时间、执行器值及暴露的传感器值也通过全精度对照（绝对容差 `1e-10`）。真实水力事件时间保持，未用轮次乘名义步长代替。

当前运行还保留了完整的 **792 条 PLC 周期记录**及 **5,600 条质量审计记录**，后者包括 5,488 条 good 和 112 条 initial 决策，均记录成功写入。

电网 baseline 与透明代理轨迹一致；MITM 在轮次 3–6 修改 4 次真实 FC16 电压写入，第 3 轮 PLC 开断，第 4 个物理快照负荷从 0.8 MW 变为 0 MW。显式 v2 基线与原格式基线一致。thermal_box 由独立配置插件加载，完成 10 个原生 PLC 控制周期、11 个物理样本，温度轨迹与解析 ODE 一致。

聚合结果：`output/upsi-regression/validation.json`，`status: passed`。

### 参考版本的首次失败

首次运行的未修改参考版本在共享 CSV 头部并发重写时出现 `_csv.Error: line contains NUL`，因此该次完整回归记为失败，未计为通过。失败数据和日志保存在 `output/upsi-hardening/failed-reference-first/` 与 `failed-reference-first.log`。

当前版本增加跨进程 CSV 锁，并用并行写入测试确认行数与各列值完整。未修改的参考版本重跑成功，随后完整对照通过。归档参考自身仍含原有并发记录问题，若再次复现应保留失败记录并重跑，不能把不完整结果当作通过。

## 真实质量策略实验

```bash
PYTHON_BIN="$PWD/output/modular-physics-venv/bin/python" bash scripts/run_upsi_quality_validation.sh
```

两组实验使用原有电压 MITM 配置，仅改变质量策略，输出在 `output/upsi-hardening/power-hold/` 与 `power-fail/`。

- **hold_last：通过。** 在第 4–9 轮共 6 次 unsupplied 电压中，物理 V_LOAD 和 P_LOAD 仍为零；实际 PLC 寄存器保留此前约 1.009958 pu 的值，SCADA 实际轮询也读到该保留值。写入元数据保留源时间 3 s，标为 stale；审计同时记录输入零值、保留值及原始/实际写入源时间。
- **fail_fast：按预期失败，检查通过。** 第 4 轮遇到 unsupplied V_LOAD 时记录拒绝并终止运行。求解成功的快照 4 仍是有效物理结果，但没有产生快照 5 或对应 ready marker，也没有将该控制周期的失败当作正常完成。

结果：`output/upsi-hardening/quality-validation.json`，`status: passed`。其中 fail_fast 的运行本身返回非零；检查器确认这是预期拒绝，不能将其当作普通成功运行。

## 数据变化与审计含义

旧物理数值、控制轨迹和历史 CSV 对照列未改变。新增或调整的主要是 `freshness`、质量/有效性元数据、嵌套 diagnostics、插件上下文以及 `signal_quality.csv`。不再把一般诊断提升到快照顶层。历史 C-Town 字段通过白名单保留。

`delivered_value` 表示 Modbus 客户端确认写入的值，不保证该值未被后续 MITM 修改；攻击后的接收行为仍需结合攻击事件、SCADA 观测与物理轨迹判断。网络超时缓存的新鲜度与源头物理质量分别记录。旧平面输入的未知采样时间明确标为 unknown，并可由 stale 策略拒绝；默认 pass_through 保持旧输入行为。

## 变更文件

| 范围 | 文件 |
| --- | --- |
| 快照、故障、类型 | `src/physics/session.py`, `types.py`, `legacy/ctown_compat.py` |
| 插件作用域 | `src/physics/registry.py`, `config.py`, `factory.py` |
| 质量、单位、时间 | `src/physics/quality.py`, `units.py`, `signal_mapper.py`, `time_coordinator.py`, `normalization.py` |
| 数据与传输边界 | `src/core/config.py`, `src/run/config_info.py`, `src/plc/adapter.py`, `src/scada/client.py` |
| 输出与生命周期 | `src/io/csv.py`, `physics_export.py`, `export_results.py`, `src/runtime/persistent_closed_loop.py` |
| SDK、测试与脚本 | `src/physics/contract.py`, `tests/test_upsi.py`, `tests/test_upsi_hardening.py`, `scripts/run_upsi_quality_validation.sh`, `scripts/check_upsi_quality_results.py` |
| 文档 | `docs/upsi.md`, `docs/adapter-sdk.md`, 本记录 |

## 复现与结果路径

- 完整回归日志：`output/upsi-regression/*-console.log`；每个案例有阶段状态、物理/SCADA/PLC 日志与报告。
- 质量实验日志：`output/upsi-hardening/power-{hold,fail}-console.log`。
- 单元日志：`output/upsi-hardening/baseline-tests.log`, `p0-tests.log`, `tests.log`, `legacy-tests.log`。
- 只检查已有结果：`python scripts/check_upsi_results.py` 和 `python scripts/check_upsi_quality_results.py`。
- 生成数据、备份和本地 Python 环境均在 Git 忽略目录，不纳入提交。

## 未测试事项与边界

- **NOT RUN：** 未单独实网注入 SCADA 网络超时、丢包和部分 Modbus 写入失败；缓存陈旧、跨 PLC 元数据及写入失败通过无需 Modbus 的夹具验证。真实 hold/fail 策略与默认完整闭环已执行。
- **NOT RUN：** 未注入真实原生库崩溃/segfault；关闭异常与部分初始化失败在 Python 契约夹具中覆盖，EPANET 原有本地句柄清理保护策略保持不变。
- 场景并行测试验证 Registry 隔离和独立纯 Python 适配器，不保证第三方本地求解器的线程安全。隐式旧 SDK 上下文仅为兼容，交错使用时应显式传 registry。
- 未知/领域单位只能保持同单位恒等映射；pu 不作无模型上下文的 SI 换算。时间协调仍是单物理后端模式。
- FMI/FMU、多物理后端耦合和通用模拟量 PLC 编码仍为后续版本工作，不在本轮实现。
