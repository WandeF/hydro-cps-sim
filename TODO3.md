# Hydro-CPS-Sim 论文补充实验 TODO

## 1. 实验目标

在现有量化实验基础上补齐以下证据：

1. 验证两条目标链路时延如何累积为 SCADA–PLC4 端到端时延；
2. 验证链路时延如何反映到 Modbus TCP 请求 RTT；
3. 使用 20 个正常/渐进退化丢包率和 1 个 50% 极端丢包率，验证随机丢包对 TCP、Modbus、控制和物理过程的影响；
4. 构造能够明确观测队列占用和队列丢包的 DoS 拥塞实验；
5. 校验 DoS 配置强度和实际攻击流量；
6. 完善 MITM、DoS 和 PLC 逻辑注入的跨层事件时间戳；
7. 验证闭环一致性指标能够检测非零差异；
8. 可选增加独立 EPANET 实现复核；
9. 汇总 C-Town 大型水网的平台运行能力。

---

# 2. 全局执行规则

## 2.1 单次运行

每个唯一配置只执行 1 次，不执行重复实验，不计算跨运行的：

* 均值；
* 标准差；
* 置信区间；
* 误差棒；
* 统计显著性。

单次运行内部仍然计算：

* mean；
* median；
* P95；
* P99；
* minimum；
* maximum。

## 2.2 现有结果复用

优先复用现有原始数据、PCAP、CSV 和 JSON。

不要重新执行：

* 原有 35 次时延重复矩阵；
* 原有 15 组带宽–DoS 矩阵；
* 原有 5 组丢包实验；
* 单 bot DoS；
* 现有无攻击基线。

新丢包实验使用本文件重新定义的 21 个丢包率，建立独立归档，不覆盖已有结果。

## 2.3 结果表述

单次实验使用：

```text
在该配置下观测到……
该次运行显示……
本次参数扫描中首次观测到……
```

禁止使用：

```text
始终……
稳定地……
平均而言……
具有统计显著性……
证明存在普遍临界点……
```

## 2.4 失败和重试

如果因以下基础设施原因失败，允许重试：

* namespace 或进程残留；
* 端口占用；
* OpenPLC 启动失败；
* ns-3 启动失败；
* 指标写入器未启动；
* screen 会话冲突。

要求：

* 保留失败目录；
* 记录失败原因；
* 使用新的 attempt 目录；
* 不覆盖失败结果；
* 只采用一个最终有效运行；
* 成功运行但未产生控制或物理偏差，不得因此重跑。

---

# 3. TODO-A：端到端时延验证

## A1. 目标路径

SCADA 与 PLC4 的通信路径包含两条被配置时延的目标链路：

```text
SCADA
→ r_scada
→ r0
→ r4
→ PLC4
```

目标链路：

```text
r_scada ↔ r0
r0 ↔ r4
```

两条链路均设置相同的单向传播时延：

```text
0, 2, 5, 10, 20, 50, 100 ms
```

必须区分：

```text
单条链路单向时延
SCADA–PLC4 端到端单向时延
Modbus 请求–响应 RTT
```

## A2. Modbus 请求追踪字段

为 SCADA–PLC4 Modbus 请求增加：

```text
experiment_id
iteration
plc_id
request_id
modbus_transaction_id
function_code
register_or_coil
request_send_monotonic_ns
request_arrive_plc_monotonic_ns
response_send_plc_monotonic_ns
response_arrive_scada_monotonic_ns
status
```

时间基准使用：

```python
time.monotonic_ns()
```

## A3. 时延计算

```text
request_one_way_ms
= request_arrive_plc - request_send_scada

plc_processing_ms
= response_send_plc - request_arrive_plc

response_one_way_ms
= response_arrive_scada - response_send_plc

modbus_rtt_ms
= response_arrive_scada - request_send_scada
```

## A4. 四个链路方向

分别保留：

```text
r_scada → r0
r0 → r_scada
r0 → r4
r4 → r0
```

每个方向输出：

```text
configured_delay_ms
measured_delay_mean_ms
measured_delay_median_ms
measured_delay_p95_ms
measured_delay_max_ms
absolute_error_ms
relative_error
packet_count
```

## A5. 理论累积关系

以 0 ms 配置为基准：

```text
ΔD_link(d) = D_link(d) - D_link(0)
ΔD_e2e(d) = D_e2e(d) - D_e2e(0)
ΔRTT(d) = RTT(d) - RTT(0)
```

理论参考：

```text
ΔD_link ≈ d
ΔD_e2e ≈ 2d
ΔRTT ≈ 4d
```

## A6. 线性拟合

拟合：

```text
D_link = α1 + β1d
D_e2e = α2 + β2d
ΔRTT = α3 + β3d
```

输出：

```text
slope
intercept
R_squared
MAE
maximum_absolute_error
```

理论参考：

```text
β1 ≈ 1
β2 ≈ 2
β3 ≈ 4
```

不得为了符合理论值筛选或修改数据。

## A7. 是否重跑

优先检查现有 PCAP 和 Modbus 日志。

* 数据足够：只做后处理；
* 缺少端到端时间戳：重新运行 7 个配置，每个配置 1 次。

固定：

```text
无攻击
100 Mbps
无随机丢包
logic-wait = 0.1 s
PLC4
SCADA
```

## A8. 输出

```text
delay_link_direction_per_run.csv
delay_end_to_end_per_run.csv
delay_modbus_rtt_per_run.csv
delay_regression_summary.json
```

诊断图：

```text
configured_vs_measured_link_delay.png
configured_vs_end_to_end_one_way_delay.png
configured_vs_modbus_rtt_increment.png
delay_components_stacked.png
```

---

# 4. TODO-B：21 级随机丢包率实验

## B1. 实验目的

验证：

```text
配置随机丢包率
→ ns-3 实测链路丢包
→ TCP 重传和确认行为
→ Modbus RTT、成功率、超时率和数据陈旧度
→ 控制和物理过程可能发生偏差
```

该实验用于证明平台能够：

* 准确施加随机丢包；
* 保留 TCP 对底层丢包的重传和恢复机制；
* 测量网络异常对 Modbus TCP 的影响；
* 观测通信异常是否进一步传播到控制与水力过程。

## B2. 目标链路

随机丢包作用于：

```text
r_scada ↔ r0
r0 ↔ r4
```

默认双向配置：

```yaml
direction: both
```

必须在 manifest 中记录：

```text
link_name
direction
source_device
receive_device
error_model_type
error_unit
configured_error_rate
random_stream
```

不得在 Python、SCADA 或 Modbus 客户端中直接随机丢弃请求。

## B3. 丢包率配置

### 正常/渐进退化范围：20 个配置

```text
0.0%
0.5%
1.0%
1.5%
2.0%
2.5%
3.0%
3.5%
4.0%
4.5%
5.0%
5.5%
6.0%
6.5%
7.0%
7.5%
8.0%
8.5%
9.0%
9.5%
```

### 极端压力测试：1 个配置

```text
50.0%
```

共计：

```text
20 个正常/渐进退化配置
+ 1 个极端配置
= 21 次正式实验
```

其中：

* 0% 为无丢包基准；
* 0.5%–9.5% 用于观察渐进变化；
* 50% 仅作为极端通信破坏压力测试；
* 50% 不得描述为典型工业网络丢包率。

## B4. 固定实验条件

```text
attacks.enabled = false
target_plc = PLC4
target_scada = SCADA
link_delay = 2 ms
link_bandwidth = 100 Mbps
logic-wait = 0.1 s
queue = 当前无拥塞基线队列
hydraulic_model = C-Town
initial_state = 与正确性基线相同
control_logic = 正常 OpenPLC 程序
```

除丢包率外，不得改变其他参数。

## B5. 仿真轮数和样本量

先使用 0% 或 0.5% 运行进行样本量预检。

计算：

```text
expected_drops
= target_packets × 0.005
```

建议最低满足：

```text
expected_drops >= 20
```

如果 100 轮不足，则全部 21 个配置统一增加到：

```text
300 轮
```

仍不足时统一增加到：

```text
500 轮
```

所有配置必须使用相同轮数。

不得增加与控制无关的高频 UDP 流量来扩大丢包样本。核心分析必须基于真实 Modbus TCP 流量。

## B6. 随机种子

每个配置保存：

```text
experiment_id
ns3_seed
ns3_run
error_model_stream
```

每个配置只运行 1 次，但应使用不同的 `ns3_run` 或随机流编号。

相同配置和相同随机种子应能够复现。

## B7. 网络层指标

每次运行输出：

```text
configured_loss_rate
target_link
direction
target_tx_packets
target_rx_packets
target_error_model_drops
target_queue_drops
target_pending_packets_at_stop
target_other_losses
measured_loss_rate
loss_absolute_error
loss_relative_error
network_delay_mean_ms
network_delay_median_ms
network_delay_p95_ms
network_delay_p99_ms
network_jitter_ms
```

误差：

```text
loss_absolute_error
= abs(measured_loss_rate - configured_loss_rate)
```

低丢包率下必须同时报告：

* 配置率；
* 实测率；
* 绝对误差；
* TX 包数；
* 实际丢包数。

不能只报告相对误差。

## B8. 包守恒

建立：

```text
tx
=
rx
+ error_model_drops
+ queue_drops
+ pending_packets
+ other_classified_losses
```

要求：

* pending 不计为丢包；
* queue drop 不重复计入 error model drop；
* 无法分类的损失进入 `other_classified_losses`；
* 仿真结束前增加 drain/grace period；
* 明确记录 drain period 长度。

## B9. TCP 层指标

通过 PCAP 和 `tshark` 提取：

```text
tcp_packets
tcp_retransmissions
tcp_fast_retransmissions
tcp_spurious_retransmissions
tcp_retransmission_rate
tcp_duplicate_acks
tcp_out_of_order_packets
tcp_zero_window_events
tcp_connection_resets
tcp_syn_retries
```

至少必须可靠获得：

```text
tcp_retransmissions
tcp_retransmission_rate
tcp_duplicate_acks
tcp_connection_resets
```

只统计 SCADA–PLC4 的 Modbus TCP 流。

不得统计整个网络全部 TCP 报文后直接作为 PLC4 结果。

## B10. Modbus TCP 指标

```text
modbus_request_count
modbus_success_count
modbus_timeout_count
modbus_exception_count
modbus_connection_error_count
modbus_other_failure_count
modbus_success_rate
modbus_timeout_rate
modbus_connection_error_rate
modbus_rtt_mean_ms
modbus_rtt_median_ms
modbus_rtt_p95_ms
modbus_rtt_p99_ms
modbus_rtt_max_ms
maximum_consecutive_failures
```

守恒：

```text
requests
=
success
+ timeout
+ exception
+ connection_error
+ other_failure
```

以下请求单独记录，不进入正式请求分母：

```text
warmup requests
connection probes
cleanup requests
```

## B11. 数据陈旧度

计算：

```text
data_age_ms
=
当前控制读取时刻
-
该变量最近一次成功更新时刻
```

输出：

```text
mean_data_age_ms
median_data_age_ms
p95_data_age_ms
p99_data_age_ms
maximum_data_age_ms
maximum_consecutive_stale_cycles
```

重点统计：

```text
PLC4
T7
PU10 相关控制输入
```

## B12. 控制层指标

```text
completed_control_cycles
control_cycle_mean_ms
control_cycle_p95_ms
control_cycle_p99_ms
control_deadline_miss_count
first_control_deviation_iteration
actuator_mismatch_count
actuator_mismatch_rate
abnormal_switch_count
```

如果没有正式控制 deadline，则以 0% 基线控制周期 P99 作为诊断阈值：

```text
deadline_source = zero_loss_baseline_p99
```

必须说明该阈值只是实验比较阈值，不是工业 PLC 的正式实时要求。

## B13. 物理层指标

逐水箱输出：

```text
RMSE
MAE
peak_absolute_deviation
first_deviation_iteration
last_deviation_iteration
AUC_absolute_deviation
recovery_status
recovery_iteration
```

总体输出：

```text
tank_pooled_rmse
tank_mean_rmse
overall_peak_absolute_deviation
overall_first_physical_deviation
overall_recovery_iteration
```

物理偏差阈值：

```text
physical_tolerance = 0.01 m
```

恢复条件：

```text
所有受影响水箱回到 0.01 m 阈值以内
并连续保持 3 个水力状态点
```

## B14. 50% 极端实验

50% 丢包可能导致：

* TCP 长时间重传；
* Modbus 大量超时；
* 连接重建；
* 控制周期阻塞；
* 实验墙钟时间显著增加。

必须设置：

```text
per_request_timeout
maximum_connection_retries
maximum_experiment_wall_clock
graceful_abort
```

达到墙钟上限时：

* 正常终止实验；
* 清理进程和 namespace；
* 保留全部已生成指标；
* 标记为 `completed_with_limit`；
* 记录完成控制周期数；
* 记录终止原因；
* 不因没有完成全部轮次而删除结果。

## B15. 输出文件

```text
packet_loss_21_levels_per_run.csv
packet_loss_link_direction.csv
packet_loss_tcp_metrics.csv
packet_loss_modbus_metrics.csv
packet_loss_control_physical_metrics.csv
packet_loss_experiment_summary.json
```

`packet_loss_21_levels_per_run.csv` 应包含 21 行。

建议核心字段：

```text
experiment_id
configured_loss_rate
measured_loss_rate
loss_absolute_error
target_tx_packets
target_rx_packets
target_error_model_drops
target_pending_packets
tcp_retransmission_rate
modbus_success_rate
modbus_timeout_rate
modbus_rtt_mean_ms
modbus_rtt_p95_ms
maximum_data_age_ms
actuator_mismatch_rate
tank_pooled_rmse
overall_peak_absolute_deviation
simulation_end
termination_reason
```

## B16. 诊断图

```text
configured_vs_measured_loss_rate.png
loss_rate_absolute_error.png
loss_rate_vs_tcp_retransmission_rate.png
loss_rate_vs_modbus_rtt_mean.png
loss_rate_vs_modbus_rtt_p95.png
loss_rate_vs_modbus_success_rate.png
loss_rate_vs_modbus_timeout_rate.png
loss_rate_vs_maximum_data_age.png
loss_rate_vs_actuator_mismatch_rate.png
loss_rate_vs_tank_pooled_rmse.png
```

绘图要求：

* 0%–9.5% 作为主范围；
* 50% 使用单独标记、断轴或独立图；
* 不使用跨运行误差棒；
* 连线仅用于视觉连接；
* 不将连线解释为单调规律；
* 横轴统一使用 `%`；
* RTT 和时延统一使用 `ms`；
* 水位误差统一使用 `m`。

## B17. 观测退化点

计算：

```text
first_observed_tcp_retransmission_loss_rate
first_observed_modbus_rtt_degradation_loss_rate
first_observed_modbus_timeout_loss_rate
first_observed_control_deviation_loss_rate
first_observed_physical_deviation_loss_rate
```

Modbus RTT 退化建议定义为：

```text
current_modbus_rtt_p95
>
zero_loss_modbus_rtt_p95 × 1.20
```

输出必须写为：

```text
本次 21 级参数扫描中首次观测到……
```

不得写为：

```text
系统的确定临界丢包率是……
```

## B18. 验收标准

* 21 个唯一配置均有独立实验 ID；
* 每个配置有独立输出目录；
* 每个配置有独立随机流信息；
* 丢包模型实际绑定到目标链路；
* 0% 无 error model 丢包；
* 50% 明确标记为极端压力测试；
* 网络守恒通过；
* Modbus 守恒通过；
* 每个结果保留 PCAP；
* 配置值与实测值能够对应；
* TCP、Modbus、控制和物理指标可通过实验 ID 关联；
* 失败尝试不进入正式结果行；
* 单次结果不表述为统计规律。

---

# 5. TODO-C：明确队列拥塞实验

## C1. 目标

建立：

```text
DoS offered load
→ 目标链路队列占用
→ 队列丢包
→ TCP 重传
→ Modbus 性能退化
```

## C2. 瓶颈链路

优先使用：

```text
r0 → r4
```

正式实验前确认：

* DoS 流量经过该方向；
* SCADA→PLC4 Modbus 请求经过该方向；
* 攻击流量未绕过该链路；
* 其他链路没有提前成为瓶颈。

## C3. 配置

```text
bandwidth = 10 Mbps
delay = 2 ms
bot_count = 3
queue_type = DropTail
queue_size = 20 packets
attack_window = 20–40
logic-wait = 0.1 s
```

正式执行：

```text
rho = 0
rho = 1.0
rho = 1.5
rho = 2.0
```

共 4 次。

## C4. 校准

先运行：

```text
10 Mbps
20 packets
rho = 2.0
```

至少满足：

```text
maximum_queue_occupancy >= 90%
```

优先满足：

```text
queue_drop_count > 0
```

未满足时依次：

1. 检查实际流量路径；
2. 检查 source offered load；
3. 将 rho 增加到 3.0；
4. 将队列减小到 10 packets；
5. 延长攻击稳定窗口。

## C5. 队列指标

```text
queue_capacity_packets
queue_enqueue_count
queue_dequeue_count
queue_drop_count
queue_max_observed_packets
queue_mean_observed_packets
queue_occupancy_ratio_max
queue_occupancy_ratio_mean
first_queue_nonzero_time
first_queue_full_time
first_queue_drop_time
```

队列时间序列：

```text
monotonic_ns
simulation_time
iteration
link
direction
queue_packets
queue_capacity
```

## C6. 输出

```text
controlled_congestion_per_run.csv
controlled_congestion_queue_timeseries.csv
controlled_congestion_summary.json
```

图：

```text
rho_vs_queue_occupancy.png
rho_vs_queue_drops.png
rho_vs_tcp_retransmissions.png
rho_vs_modbus_rtt_p95.png
rho_vs_modbus_timeout_rate.png
queue_occupancy_timeline.png
```

## C7. 验收

至少一个正式攻击配置满足：

```text
queue_occupancy_ratio_max >= 0.9
queue_drop_count > 0
```

未产生物理偏差仍属于有效实验结果。

---

# 6. TODO-D：DoS 强度校验

## D1. 计算实测强度

对现有和新增 DoS 实验计算：

```text
configured_rho
source_offered_rho
bottleneck_ingress_rho
bottleneck_received_rho
```

使用攻击稳定窗口，排除启动、结束和排空阶段。

## D2. 区分流量概念

分别报告：

```text
source_offered_attack_load
bottleneck_ingress_attack_load
received_attack_goodput
```

不得使用接收 goodput 代替攻击强度。

## D3. 检查原 20 Mbps、rho=0.8 结果

检查：

* 实际 offered load；
* 三个 bot 的发送速率；
* 报文长度和包间隔；
* 实际路径；
* TCP 重传发生位置；
* socket/TAP 缓冲区；
* Modbus 退化阈值；
* 是否将启动异常计入攻击窗口。

优先复用现有 PCAP 和日志。

只有缺少证据时重跑：

```text
20 Mbps, rho=0.8
20 Mbps, rho=1.0
```

各 1 次。

## D4. 输出

```text
bandwidth_dos_measured_rho.csv
bandwidth_dos_path_diagnostics.json
bandwidth_dos_20mbps_diagnostic.md
```

---

# 7. TODO-E：统一跨层时间戳

## E1. 公共事件字段

```text
experiment_id
scenario
event_id
event_type
event_source
iteration
hydraulic_time_sec
monotonic_ns
epoch_ns
request_id
modbus_transaction_id
plc_id
variable
value_before
value_after
```

## E2. MITM

```text
tA_enable
tI_intercept
tM_modify
tC_receive
tU_control
tP_physical
tE_attack_end
tR_recovery
```

`tA` 和 `tC` 必须来自不同事件。

## E3. DoS

```text
tA
tN_queue
tN_drop
tC_latency
tC_failure
tU
tP
tE
tR
```

`tC_latency` 和 `tC_failure` 分别记录。

## E4. PLC 逻辑注入

```text
tCompileStart
tCompileEnd
tDeployStart
tA
tU
tP
tE
tR
```

通信异常通常记录为：

```text
communication_anomaly_status = not_applicable
tC = null
```

## E5. 代表性重跑

各运行 1 次：

```text
MITM PLC4/T7
三 bot 强 DoS
PLC4 逻辑注入
```

DoS 优先使用明确发生队列拥塞的配置。

## E6. 输出

```text
event_timeline.csv
propagation_summary_v2.json
attack_propagation_comparison.csv
```

---

# 8. TODO-F：正确性指标敏感性检查

## F1. 目的

验证：

```text
pooled RMSE = 0
actuator mismatch rate = 0
```

不是由路径错误、文件复用或指标固定输出导致。

## F2. 诊断配置

执行 1 次：

```text
PLC4 阈值 4.8 → 4.7
```

或者：

```text
PU10 输出延迟 1 个控制周期
```

不要使用 999 等强攻击值。

## F3. 预期非零指标

```text
tank_rmse
tank_mae
tank_max_absolute_error
actuator_mismatch_count
actuator_mismatch_rate
switch_iteration_error
first_deviation_iteration
```

## F4. 输出

```text
correctness_metric_sensitivity.json
correctness_metric_sensitivity.csv
```

---

# 9. TODO-G：独立 EPANET 复核（可选）

## G1. 目标

验证 DHALSIM 适配的 epynet 没有改变标准 EPANET 水力结果。

## G2. 方法

使用固定执行器时序，将相同输入分别交给：

```text
DHALSIM-epynet
官方 EPANET Toolkit 或 WNTR EPANET backend
```

输入保持一致：

* C-Town INP；
* demand pattern；
* initial tank levels；
* hydraulic timestep；
* actuator schedule。

## G3. 比较

```text
tank level
selected node pressure
selected pipe flow
pump status
RMSE
MAE
maximum_absolute_error
normalized_L2_error
```

## G4. 输出

```text
epynet_vs_official_epanet.csv
epynet_vs_official_epanet_summary.json
epynet_vs_official_epanet.png
```

不得称为与完整 DHALSIM 平台对比。

---

# 10. TODO-H：大型水网能力汇总

基于 C-Town 生成：

```text
large_scale_capability_summary.json
```

字段：

```text
junction_count
tank_count
reservoir_count
pipe_count
pump_count
valve_count
plc_count
scada_count
network_node_count
router_count
link_count
sensor_mapping_count
actuator_mapping_count
completed_control_cycles
total_wall_clock_sec
mean_cycle_wall_clock_sec
peak_memory_mb
mean_cpu_percent
pcap_size_mb
log_size_mb
cleanup_status
```

该部分验证：

* 平台规模；
* 多 PLC 运行能力；
* 实验执行能力；
* 资源开销；
* 数据采集完整性。

不得使用水网规模本身证明高保真。

---

# 11. 新增实验数量

## 必须新增

```text
21 级丢包实验：21 次
明确队列拥塞：4 次
跨层时间戳重跑：3 次
正确性敏感性检查：1 次
```

合计：

```text
29 次新增正式或诊断实验
```

## 视现有数据决定

```text
端到端时延重跑：0 或 7 次
20 Mbps 诊断：0 或 2 次
独立 EPANET 复核：0 或 1 次
```

总新增运行数量：

```text
最少 29 次
最多 39 次
```

基础设施失败和校准尝试不计入正式配置数量。

---

# 12. 归档结构

```text
output/quantitative_supplement_<timestamp>_<branch>_<commit>/
```

目录：

```text
01_delay_path_validation/
02_packet_loss_21_levels/
03_controlled_queue_congestion/
04_dos_intensity_diagnostics/
05_cross_layer_timestamps/
06_correctness_sensitivity/
07_epanet_crosscheck/
08_large_scale_summary/
09_combined_statistics/
ARCHIVE_INDEX.md
FINAL_REPORT.md
VALIDATION_REPORT.md
```

每个正式运行保留：

```text
config.yaml
resolved_config.yaml
manifest.json
workspace.patch
run.log
lifecycle.csv
event_timeline.csv
network_metrics.csv
tcp_metrics.csv
modbus_metrics.csv
control_metrics.csv
physics.csv
pcap/
summary.json
quality_report.json
```

---

# 13. 最终报告要求

最终报告只陈述实验事实，不修改论文正文。

必须回答：

1. 两条目标链路四个方向的配置时延和实测时延；
2. SCADA–PLC4 单向时延是否体现两条链路累积；
3. Modbus RTT 增量与单链路配置时延的关系；
4. 21 个丢包配置的配置值和实测值；
5. 丢包率对 TCP 重传的观测影响；
6. 丢包率对 Modbus RTT、成功率、超时率和数据陈旧度的观测影响；
7. 本次扫描中首次出现 TCP 重传、Modbus 退化、控制偏差和物理偏差的配置；
8. 50% 极端丢包实验是否完整结束；
9. 50% 实验的完成控制周期和终止原因；
10. 哪个 DoS 配置首次产生队列高占用；
11. 哪个配置首次产生队列丢包；
12. 队列丢包是否对应 TCP 和 Modbus 退化；
13. 配置 rho 与实际 offered rho 是否一致；
14. 原 20 Mbps、rho=0.8 提前退化的原因；
15. MITM、DoS 和 PLC 逻辑注入的新事件时间线；
16. tA、tC、tU 和 tP 是否来自独立事件；
17. 正确性指标敏感性检查是否产生非零结果；
18. 独立 EPANET 复核是否执行；
19. C-Town 大型水网的规模和运行开销；
20. 所有 CSV、JSON、PCAP 和诊断图路径；
21. 所有失败尝试、受控终止和数据质量状态；
22. 所有结论的适用范围和限制。

不得为了符合理论关系修改、删除或选择性报告实验数据。
::: 
