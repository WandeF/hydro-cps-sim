# Hydro-CPS-Sim V1.0 输出结果

输出根目录由 `output_path` 指定。配置副本工具会将其设置为本机绝对路径。运行脚本会清理该目录下的 `runtime/`、`reports/`、`st/` 和 `plcs/`，再次运行前应归档已有结果。

```text
<output_path>/
  st/                    每台 PLC 的生成 ST
  plc.st                 合并 ST
  plcs/                  编译后的 PLC 程序
  network.sh             Linux namespace/TAP 初始化脚本
  ns3_network.cc         生成的 ns-3 拓扑
  logs/                  run_all-*.log、ns3_build.log、ns3_network.log 等
  timing/run_all_timing.csv
  runtime/
    raw/                 physics、actuator_state、攻击等 JSONL
    json/                物理/执行器快照和运行映射等 JSON
    csv/                 运行及兼容 CSV
    logs/                子进程日志
    sync/                进程协调标记
  reports/csv/           规范导出表
```

目录随运行阶段逐步创建。ns-3 使用相对抓包前缀，PCAP 文件通常保存在 ns-3 工作目录中。

## 导出表

| 文件 | 字段与用途 |
| --- | --- |
| `physics.csv` | `iteration` 加配置推导的传感 tag（T、PU*F、J* 等）；水力数据的单位由 INP 单位制确定 |
| `actuator_state.csv` | `iteration` 加观察到的执行器名，列按名称排序；单元格保留输入记录的值表示方式，例如 `True`/`False` |
| `scada_observed_long.csv` | `iteration,plc,variable,value,source,direction,kind,timestamp_epoch`；本地归属传感观测 |
| `scada_observed_wide.csv` | `iteration` 加 `PLC名.变量名`；PLC 按数字编号排序 |
| `scada_observed_key.csv` | 宽表中的水箱变量子集 |
| `attack_events.csv` | 场景、事件、目标、协议、变量、修改前后值、发送统计等，完整表头见下文 |
| `attack_schedule.csv` | `iteration,scenario,target,event,active,timestamp_epoch,active_window,proxy_pid,message` |
| `scada_timeout_events.csv` | `timestamp_epoch,iteration,phase,plc,ip,status,warmup,used_previous,previous_iteration,message` |
| `cycle_timing.csv` | 周期索引、输入输出物理索引及分阶段秒数，可附加已有记录中的扩展列 |

攻击事件表固定表头：

```text
iteration,scenario,attack,event,rule,source,target,target_ip,target_port,protocol,rate,packet_size,packets,bytes,variable,direction,function_code,old_value,new_value,timestamp_epoch,transaction_id,message
```

`cycle_timing.csv` 基础列为 `iteration,input_physics_iteration,output_physics_iteration,wait_local_write_sec,wait_scada_downlink_sec,logic_wait_sec,signal_read_actuators_sec,wait_actuator_read_sec,merge_actuator_state_sec,physics_step_publish_sec,write_cycle_logs_sec,cycle_total_sec,local_plc_count,actuator_count,physics_value_count`。阶段计时表 `run_all_timing.csv` 列为 `stage,start_epoch_ns,end_epoch_ns,duration_sec,status`。

主入口使用 `dhalsim` 初始化方式。`iterations: 100` 对应控制周期 1–99 和物理状态记录 0–100，共 101 行物理数据。其中 `physics_0000` 为零值占位记录，`physics_0001` 为配置初始状态，后续记录为水力推进结果。

没有攻击事件时，事件表只包含表头。分析数据时应单独处理缺测值，并区分计算机实际运行耗时和模型中的物理时间。

## 单独导出

需保留原配置、对应的生成 ST 和 runtime：

```bash
python -m src.io.export_results --config local-configs/baseline.yaml \
  --runtime-dir output/v1-baseline/runtime --reports-dir output/v1-baseline/reports
```

导出程序优先读取 `raw/` 下的 JSONL，部分表也支持读取 JSON 或兼容 CSV。程序写入 `reports/csv/` 后会更新 `runtime/csv/`，因此重新导出时应使用已归档的运行副本。

运行 CSV 旁的 `.csv.lock` 文件用于协调多进程写入。内核在进程退出后释放锁，运行期间应保留这些文件。`examples/c_town/baseline/csv/` 保存历史实验样本。
