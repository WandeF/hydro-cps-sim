# 量化实验执行次数修订

## 一、全局执行规则

所有实验配置仅执行1次，不进行重复实验。

删除或忽略上一版任务中以下要求：

* 每个配置执行5次；
* 使用5个不同随机种子；
* 计算重复实验均值和标准差；
* 失败后补足5次成功重复；
* 对传播轮次进行5次一致性比较；
* 聚合表要求每组包含5条记录。

每个参数组合只需生成一个唯一实验：

```text
一个配置参数组合 = 一次实验运行
```

manifest中仍需保存随机种子。随机种子的作用是保证单次实验可复现，而不是用于重复实验统计。

---

# TODO 1：随机丢包实验矩阵

执行以下丢包等级：

```text
0%
1%
2%
5%
10%
50%
```

每个等级执行1次：

```text
5个丢包等级 × 1次 = 5次实验
```

固定条件：

```text
无攻击
链路时延：2 ms
带宽：100 Mbps
logic-wait：0.1 s
目标路径：SCADA–PLC4
```

如果100轮产生的包数量不足以观察1%丢包，可将该实验统一增加至300轮或500轮，但每个丢包等级仍只执行1次。

---

# TODO 2：带宽—DoS拥塞实验矩阵

带宽等级：

```text
5 Mbps
10 Mbps
20 Mbps
```

DoS归一化强度：

```text
rho = 0
rho = 0.8
rho = 1.0
rho = 1.2
rho = 1.5
```

每个参数组合只执行1次：

```text
3个带宽 × 5个DoS强度 = 15次实验
```

如果需要先进行预检，先运行：

```text
10 Mbps × 5个DoS强度 = 5次实验
```

确认攻击流量、队列和Modbus指标正常后，再执行5 Mbps和20 Mbps的10次实验。

不要重复执行已成功的参数组合。

---

# TODO 3：DoS跨层传播实验

执行：

```text
单bot DoS × 1次
三bot DoS × 1次
```

共2次正式跨层实验。

如果单bot和三bot场景均未产生通信、控制或物理异常，从带宽—DoS矩阵中选择一个已经产生明显拥塞的强DoS场景，额外执行1次跨层分析。

不要为了获得非零结果反复运行同一配置。

---

# TODO 4：PLC逻辑注入跨层传播实验

使用现有正式PLC逻辑注入场景执行1次。

保存：

```text
原始ST文件
恶意ST文件
注入前后源文件哈希
注入前后可执行程序哈希
编译日志
部署日志
攻击时间线
传播摘要
```

不需要执行多次逻辑注入实验。

---

# TODO 5：现有MITM实验

现有MITM实验已经完成1次，本轮不重复执行。

直接复用现有结果：

```text
实际篡改次数：21
tA=tC=tU=20
tP=22
攻击结束边界：41
观察窗口内未恢复
```

MITM、DoS和PLC逻辑注入最终统一生成相同格式的：

```text
propagation_summary.json
```

---

# TODO 6：失败和重试规则

“每个配置只执行1次”指每个配置只保留一个最终有效实验结果。

如果实验因为以下非实验性原因失败：

```text
进程启动失败
端口占用
namespace残留
screen会话残留
临时文件冲突
指标写入器未启动
ns-3启动失败
```

允许重试，但必须：

* 保留首次失败目录；
* 记录失败原因；
* 清理残留进程和namespace；
* 使用新的attempt目录；
* 不把失败尝试计入正式结果；
* 同一配置最多重试2次。

如果实验成功完成，但结果为：

```text
无丢包
无Modbus超时
无控制偏差
无物理偏差
```

则该实验视为有效结果，不得因为结果“不明显”而重复运行。

---

# TODO 7：聚合与统计方式调整

由于每个配置只有一次实验，不计算：

```text
跨重复实验均值
跨重复实验标准差
置信区间
重复实验中位数
重复实验P95
```

仍然需要计算**单次实验内部样本**的统计量，例如：

```text
包时延平均值
包时延中位数
包时延P95
包时延P99
Modbus RTT平均值
Modbus RTT中位数
Modbus RTT P95
Modbus RTT P99
队列长度平均值
队列长度最大值
```

这些指标来自一次运行中的多条报文或多个控制周期，与重复实验统计不同。

---

# TODO 8：结果表调整

## 随机丢包逐配置表

```text
packet_loss_per_run.csv
```

共5行，每行对应一个丢包率。

字段至少包括：

```text
configured_loss_rate
measured_loss_rate
loss_absolute_error
target_tx_packets
target_rx_packets
target_lost_packets
target_pending_packets
tcp_retransmissions
tcp_retransmission_rate
modbus_success_rate
modbus_timeout_rate
modbus_rtt_mean_ms
modbus_rtt_p95_ms
maximum_data_age_ms
actuator_mismatch_rate
tank_pooled_rmse
```

不再需要按丢包等级生成重复实验聚合均值。

可以保留：

```text
packet_loss_summary.csv
```

但它只是5个不同参数配置的汇总表，而不是重复实验统计表。

## 带宽—DoS逐配置表

```text
bandwidth_dos_per_run.csv
```

共15行，每行对应一个：

```text
带宽 + rho
```

组合。

## DoS传播表

```text
dos_propagation_per_run.csv
```

正常情况下2行：

```text
single_bot
three_bots
```

## PLC逻辑注入传播表

```text
plc_logic_injection_per_run.csv
```

正常情况下1行。

---

# TODO 9：诊断图调整

诊断图不绘制重复实验误差棒。

## 丢包实验

绘制：

```text
配置丢包率 vs 实测丢包率
配置丢包率 vs TCP重传率
配置丢包率 vs Modbus RTT P95
配置丢包率 vs Modbus成功率
配置丢包率 vs 最大数据陈旧度
```

每个丢包等级对应一个数据点。

## 带宽—DoS拥塞实验

绘制：

```text
rho vs 队列丢包数
rho vs 队列最大长度
rho vs TCP重传率
rho vs Modbus RTT P95
rho vs Modbus超时率
rho vs 最大数据陈旧度
rho vs 水箱pooled RMSE
```

5 Mbps、10 Mbps和20 Mbps分别形成一条曲线。

## 跨层攻击

绘制：

```text
MITM传播时间线
单bot DoS传播时间线
三bot DoS传播时间线
PLC逻辑注入传播时间线
```

---

# TODO 10：数据质量要求

虽然不做重复实验，每次单次实验仍必须通过完整质量检查：

```text
simulation_end = success
metrics_writer_status = ok
telemetry_drop_count = 0
conflict_count = 0
cleanup_status = success
```

网络守恒：

```text
tx - rx = lost + pending
```

Modbus守恒：

```text
requests
=
success
+ timeout
+ exception
+ connection_error
+ other_failure
```

攻击检查：

```text
attack_enabled = true
attack_window_triggered = true
actual_attack_event_count > 0
```

PLC逻辑注入检查：

```text
before_hash != after_hash
malicious_logic_deployed = true
```

如果某个攻击未造成后续控制或物理偏差，仍需记录：

```text
attack_executed = true
control_deviation_detected = false
physical_deviation_detected = false
```

---

# TODO 11：修订后的实验总量

本轮预计正式实验数量：

```text
随机丢包矩阵：5次
带宽—DoS矩阵：15次
DoS跨层传播：2次
PLC逻辑注入传播：1次
MITM：复用已有结果，不重跑
```

合计：

```text
23次新增正式实验
```

如果带宽—DoS矩阵中的单bot或三bot配置能够直接用于跨层传播分析，则DoS跨层实验可以复用对应运行，不额外执行。

此时最少新增实验数量可以降低为：

```text
21次
```

具体取决于现有DoS配置是否与带宽—拥塞矩阵中的配置完全一致。

---

# TODO 12：最终报告要求

实验完成后报告：

1. 新归档入口；
2. 5个丢包等级的逐次结果；
3. 配置丢包率与实测丢包率的偏差；
4. TCP重传和Modbus RTT随丢包率的变化；
5. 15个带宽—DoS配置的逐次结果；
6. 各带宽下开始出现拥塞、TCP重传和Modbus退化的 `rho`；
7. 单bot和三bot DoS的跨层传播结果；
8. PLC逻辑注入的跨层传播结果；
9. 主要CSV、JSON、PCAP和诊断图路径；
10. 所有实验的数据质量状态；
11. 成功实验中未出现预期影响的情况；
12. 失败尝试及其原因。

不得将单次实验结果表述为统计上的普遍规律。结果描述使用：

```text
“在本次实验配置下”
“该次运行显示”
“观测到”
```

避免使用：

```text
“稳定地”
“始终”
“平均而言”
“具有统计显著性”
```
