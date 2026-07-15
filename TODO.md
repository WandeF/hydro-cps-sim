下面给出一套可以直接落地到 `hydro-cps-sim` 的量化实验实现方案。重点不是再增加攻击功能，而是建立统一的**实验采集、指标计算、批量运行和论文出图流水线**。

从当前仓库看，C-Town 配置已经包含 8 个 PLC、1 个 SCADA、Linux namespace、ns-3 实时调度、PCAP 开关，以及链路带宽和时延配置；基础场景、单 bot DoS、三 bot DoS 和多个 MITM 场景也已经存在，因此不需要重构平台主体，只需要增加量化采集层。([GitHub][1])

---

# 一、量化工作的总体目标

建议围绕四个研究问题组织实验。

## RQ1：闭环联合仿真的结果是否正确

回答：

> 加入 OpenPLC、Modbus TCP、Linux namespace 和 ns-3 后，水力状态和控制状态是否仍然与参考闭环模型一致？

主要指标：

* 水箱水位 RMSE；
* MAE；
* 最大绝对误差；
* 执行器状态不一致率；
* 控制切换时刻误差；
* 多次重复实验的标准差。

---

## RQ2：配置的网络条件是否被准确实现

回答：

> YAML 中配置的带宽、时延、丢包和队列条件，是否实际作用到了 SCADA–PLC 通信路径？

主要指标：

* 配置时延与实测 RTT；
* 单向网络时延；
* 配置丢包率与实测丢包率；
* 吞吐量；
* Modbus 请求成功率；
* Modbus 超时率；
* 重传数；
* 队列丢包数。

当前配置已经使用 ns-3 实时调度，启用了 PCAP，并为骨干链路配置了 `100Mbps` 和 `2ms` 等参数，适合直接构造网络准确性实验。

---

## RQ3：攻击如何跨层传播

回答：

> MITM、DoS 和 PLC 逻辑注入分别从哪里进入系统，又经过多长时间影响控制状态和物理过程？

主要指标：

* 攻击实际开始时间；
* 第一条异常通信事件时间；
* 第一条控制异常时间；
* 第一条物理偏差时间；
* 最大物理偏差；
* 攻击期间 RMSE；
* 攻击后恢复时间；
* 异常控制持续时间；
* 超限持续时间。

---

## RQ4：平台运行开销和扩展性如何

回答：

> 随着 PLC 数量、仿真轮数和攻击节点数增加，平台的执行时间和资源消耗如何变化？

主要指标：

* 初始化耗时；
* OpenPLC 编译耗时；
* namespace 创建耗时；
* ns-3 启动耗时；
* 单轮闭环耗时；
* 总运行时间；
* CPU 使用率；
* 峰值内存；
* 每轮通信次数；
* 每轮日志量；
* 攻击模块额外开销。

---

# 二、建议新增的代码结构

建议不要把指标代码散落到 SCADA、物理求解器和攻击模块中，而是增加独立的 `metrics` 与 `experiments` 模块。

```text
hydro-cps-sim/
├── src/
│   ├── metrics/
│   │   ├── __init__.py
│   │   ├── event_logger.py
│   │   ├── runtime_monitor.py
│   │   ├── network_metrics.py
│   │   ├── control_metrics.py
│   │   ├── physical_metrics.py
│   │   └── propagation_metrics.py
│   │
│   └── experiment/
│       ├── __init__.py
│       ├── runner.py
│       ├── manifest.py
│       └── config_generator.py
│
├── scripts/
│   ├── run_experiment.py
│   ├── run_experiment_matrix.py
│   ├── analyze_experiment.py
│   ├── analyze_batch.py
│   └── plot_paper_figures.py
│
├── experiments/
│   ├── accuracy/
│   ├── network_validation/
│   ├── attacks/
│   └── scalability/
│
└── results/
    └── <experiment_id>/
```

每个实验目录采用统一输出格式：

```text
results/<experiment_id>/
├── manifest.json
├── config_resolved.yaml
├── events.csv
├── physical.csv
├── control.csv
├── communication.csv
├── network.csv
├── resources.csv
├── summary_metrics.json
├── summary_metrics.csv
├── logs/
└── pcap/
```

---

# 三、第一阶段：建立统一事件时间线

这是量化工作的核心。现在不同层的日志很可能各自记录自己的内容，但跨层分析必须使用统一字段。

## 3.1 统一事件格式

新增：

```python
# src/metrics/event_logger.py

from dataclasses import dataclass, asdict
from pathlib import Path
from threading import Lock
from typing import Any
import csv
import json
import time


@dataclass
class MetricEvent:
    wall_time_ns: int
    monotonic_ns: int
    iteration: int
    layer: str
    component: str
    event_type: str
    source: str = ""
    target: str = ""
    variable: str = ""
    value: Any = ""
    status: str = ""
    request_id: str = ""
    attack_id: str = ""
    details: str = ""


class EventLogger:
    def __init__(self, output_file: Path):
        self.output_file = output_file
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialized = self.output_file.exists()

    def log(self, event: MetricEvent) -> None:
        row = asdict(event)

        if not isinstance(row["value"], (str, int, float, bool)):
            row["value"] = json.dumps(
                row["value"],
                ensure_ascii=False,
                sort_keys=True,
            )

        with self._lock:
            write_header = not self._initialized

            with self.output_file.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=row.keys())
                if write_header:
                    writer.writeheader()
                    self._initialized = True
                writer.writerow(row)


def make_event(**kwargs) -> MetricEvent:
    return MetricEvent(
        wall_time_ns=time.time_ns(),
        monotonic_ns=time.monotonic_ns(),
        **kwargs,
    )
```

必须同时保存：

* `wall_time_ns`：便于人类查看；
* `monotonic_ns`：用于精确计算时差；
* `iteration`：用于和水力轮次对齐。

跨进程分析中，优先使用 `monotonic_ns` 的差值；跨 namespace 不影响系统单调时钟。

---

## 3.2 必须记录的事件

### 物理层

每轮至少记录：

```text
physics_iteration_start
physics_sensor_value
physics_actuator_input
physics_iteration_end
```

例如：

```csv
iteration,layer,component,event_type,variable,value
20,physical,epanet,physics_sensor_value,T7,3.82
20,physical,epanet,physics_actuator_input,PU10,1
```

### SCADA层

记录：

```text
scada_iteration_start
modbus_read_start
modbus_read_success
modbus_read_timeout
modbus_write_start
modbus_write_success
modbus_write_timeout
scada_iteration_end
```

### PLC层

记录：

```text
plc_input_received
plc_scan_start
plc_output_changed
plc_scan_end
```

### 攻击层

记录：

```text
attack_triggered
attack_packet_sent
attack_value_modified
attack_logic_loaded
attack_stopped
```

### 网络层

记录：

```text
packet_tx
packet_rx
packet_drop
queue_drop
flow_summary
```

不建议为每个 ns-3 内部包都写 Python CSV，否则可能影响实时性能。网络层详细包事件保留在 PCAP 和 ns-3 FlowMonitor 中，统一事件文件只记录关键摘要。

---

# 四、第二阶段：为 Modbus 请求增加关联ID

目前做跨层传播时，必须知道一条 SCADA 请求对应哪条 PLC 响应。

## 4.1 生成请求ID

SCADA 每次读写前生成：

```python
request_id = (
    f"iter-{iteration:05d}-"
    f"{plc_name.lower()}-"
    f"{operation}-"
    f"{sequence:06d}"
)
```

例如：

```text
iter-00020-plc4-read-000315
```

记录：

```python
start_ns = time.monotonic_ns()

event_logger.log(make_event(
    iteration=iteration,
    layer="communication",
    component="scada",
    event_type="modbus_read_start",
    source="scada",
    target="PLC4",
    variable="T7",
    request_id=request_id,
))
```

完成后：

```python
end_ns = time.monotonic_ns()

event_logger.log(make_event(
    iteration=iteration,
    layer="communication",
    component="scada",
    event_type="modbus_read_success",
    source="PLC4",
    target="scada",
    variable="T7",
    value=value,
    status="success",
    request_id=request_id,
    details=json.dumps({
        "latency_ms": (end_ns - start_ns) / 1e6,
    }),
))
```

超时时：

```python
event_type="modbus_read_timeout"
status="timeout"
```

## 4.2 不修改 Modbus 协议报文

不需要自行向 Modbus TCP 报文中增加字段。

请求ID只用于本地日志关联，底层仍然使用 Modbus 的 Transaction Identifier 和 socket 五元组。分析 PCAP 时可以使用：

* 源IP；
* 目的IP；
* 源端口；
* 目的端口；
* Modbus Transaction ID；
* 时间戳。

实现 SCADA 日志和 PCAP 的二次对齐。

---

# 五、闭环正确性实验

## 5.1 参考组设置

至少准备两个输出：

### Reference

直接使用 DHALSIM-EPANET 闭环逻辑运行：

```text
reference_physics.csv
reference_control.csv
```

### Platform

完整运行：

```text
EPANET/DHALSIM
→ SCADA
→ Modbus TCP
→ OpenPLC
→ Modbus TCP
→ SCADA
→ EPANET/DHALSIM
```

输出：

```text
platform_physics.csv
platform_control.csv
```

两者必须保证：

* 相同 INP；
* 相同初始水位；
* 相同仿真步长；
* 相同控制阈值；
* 相同执行器初始状态；
* `noise_scale: 0.0`；
* 无攻击。

当前 C-Town 配置中水力步长为 300 秒、100轮，并明确配置了各水箱初始值和执行器初始状态，适合作为一致性实验的固定条件。

---

## 5.2 指标计算

对于水箱 (j)：

[
\mathrm{RMSE}*j =
\sqrt{
\frac{1}{N}
\sum*{i=1}^{N}
(x_{i,j}^{platform}-x_{i,j}^{reference})^2
}
]

同时计算：

[
\mathrm{MAE}*j =
\frac{1}{N}
\sum*{i=1}^{N}
|x_{i,j}^{platform}-x_{i,j}^{reference}|
]

[
E_{\max,j} =
\max_i
|x_{i,j}^{platform}-x_{i,j}^{reference}|
]

执行器状态不一致率：

[
D_a =
\frac{1}{N}
\sum_{i=1}^{N}
\mathbf{1}
(u_{i,a}^{platform}\neq u_{i,a}^{reference})
]

总体执行器不一致率：

[
D_{all} =
\frac{
\sum_a\sum_i
\mathbf{1}
(u_{i,a}^{platform}\neq u_{i,a}^{reference})
}{
N \times A
}
]

---

## 5.3 控制切换时刻误差

只算状态不一致率还不够，应计算每个执行器的开关事件时刻。

例如参考组：

```text
PU10: iteration 15 open
PU10: iteration 48 closed
```

平台组：

```text
PU10: iteration 16 open
PU10: iteration 49 closed
```

切换误差：

[
E_{\mathrm{switch}} =
|i_{\mathrm{platform}}-i_{\mathrm{reference}}|
]

论文中可以报告：

* 平均切换误差；
* 最大切换误差；
* 完全匹配的切换事件比例。

---

## 5.4 重复实验

基线无噪声场景理论上应具有确定性，但由于平台使用 ns-3 `realtime` scheduler 和多个进程，运行耗时可能有波动。建议：

* 正确性实验重复 5 次；
* 性能实验重复 10 次；
* 攻击实验至少重复 5 次。

输出均值和标准差：

```text
mean ± standard deviation
```

对于确定性的物理状态，若5次输出完全一致，也可以明确写：

> All five repetitions produced identical hydraulic and actuator trajectories.

但必须由脚本验证，不能人工观察后声称一致。

---

# 六、网络准确性实验

## 6.1 网络实验配置维度

建议不要一次同时改变多个变量。

### 时延实验

固定：

```text
bandwidth = 100 Mbps
loss = 0
queue = sufficiently large
```

变化单链路时延：

```text
0 ms
2 ms
5 ms
10 ms
20 ms
50 ms
100 ms
```

### 丢包实验

固定时延 `2 ms`，变化丢包率：

```text
0%
0.1%
0.5%
1%
2%
5%
10%
```

### 带宽实验

固定无丢包，变化带宽：

```text
100 Mbps
20 Mbps
10 Mbps
5 Mbps
2 Mbps
1 Mbps
```

### 队列实验

固定带宽和时延，变化队列长度：

```text
10 packets
25 packets
50 packets
100 packets
500 packets
```

---

## 6.2 配置文件扩展

在链路中增加：

```yaml
network:
  measurement:
    enabled: true
    flow_monitor: true
    pcap: true
    ping_probe: true
    modbus_probe: true

  backbone_links:
    - name: r0-r2
      type: point_to_point
      endpoints: [r0, r2]
      data_rate: 10Mbps
      delay: 20ms
      mtu: 1500
      queue:
        type: DropTailQueue
        max_packets: 50
      error_model:
        type: rate
        unit: packet
        error_rate: 0.01
```

不要只支持全局网络参数。需要允许：

* 全局默认值；
* 单链路覆盖；
* 攻击窗口动态变化。

---

## 6.3 ns-3 FlowMonitor

在生成的 ns-3 C++ 文件中增加：

```cpp
#include "ns3/flow-monitor-module.h"
```

安装：

```cpp
FlowMonitorHelper flowHelper;
Ptr<FlowMonitor> flowMonitor = flowHelper.InstallAll();
```

仿真结束前：

```cpp
flowMonitor->CheckForLostPackets();
flowMonitor->SerializeToXmlFile(
    outputDir + "/flow-monitor.xml",
    true,
    true
);
```

解析字段：

* `txPackets`；
* `rxPackets`；
* `lostPackets`；
* `txBytes`；
* `rxBytes`；
* `delaySum`；
* `jitterSum`；
* `timeFirstTxPacket`；
* `timeLastRxPacket`。

计算：

[
PacketLossRate =
\frac{TxPackets-RxPackets}{TxPackets}
]

[
MeanDelay =
\frac{DelaySum}{RxPackets}
]

[
Throughput =
\frac{8 \times RxBytes}
{t_{lastRx}-t_{firstTx}}
]

注意 FlowMonitor 的平均时延是 IP 流级指标，不能直接等同于 Modbus 应用请求时延，所以必须同时采集 SCADA 请求耗时。

---

## 6.4 RTT 与单向时延的区分

一个 SCADA→PLC 请求和 PLC→SCADA 响应经过多条链路。

假设路径中共有 (m) 条链路，每条链路配置传播时延为 (d_k)，那么理论最小 RTT 为：

[
RTT_{theory}
\approx
2\sum_{k=1}^{m} d_k
]

还需加上：

* 序列化时延；
* 队列时延；
* namespace/TAP 转发开销；
* PLC 应用处理时间；
* Python Modbus 客户端处理时间。

因此论文中不应要求“实测 RTT 与配置单链路时延完全相等”，而应比较：

1. 理论网络传播下界；
2. ns-3 FlowMonitor 网络时延；
3. 应用层 Modbus RTT。

建议结果表：

| 配置单链路时延 | 理论路径RTT | FlowMonitor RTT估计 | Modbus RTT | 应用额外开销 |
| ------: | ------: | ----------------: | ---------: | -----: |
|    2 ms |    8 ms |            8.4 ms |    11.2 ms | 2.8 ms |
|   10 ms |   40 ms |           40.6 ms |    43.5 ms | 2.9 ms |

---

## 6.5 网络参数准确性误差

[
DelayError =
\frac{|D_{measured}-D_{expected}|}
{D_{expected}}
\times 100%
]

[
LossError =
|L_{measured}-L_{configured}|
]

丢包实验需要足够多的数据包。若只运行100轮，每轮数据包数量较少，配置 `0.1%` 丢包率时统计波动会很大。

因此网络准确性实验应单独增加高频测量流量：

```yaml
measurement:
  probe_interval_ms: 20
  probe_duration_sec: 120
  payload_bytes: 64
```

这样可以将“网络模型准确性实验”和“正常闭环控制实验”分开：

* Probe 流量用于验证网络参数；
* Modbus 流量用于验证业务影响。

---

# 七、跨层攻击传播量化

## 7.1 定义四个时间点

对每次攻击统一定义：

### (t_A)：攻击入口时间

例如：

* DoS 第一条攻击包进入网络；
* MITM 第一条响应被篡改；
* PLC 逻辑注入完成加载。

### (t_C)：通信异常时间

例如：

* 第一条 Modbus 超时；
* 第一条被篡改的 Modbus 响应到达 SCADA；
* 第一条异常寄存器值出现。

### (t_U)：控制异常时间

攻击场景执行器状态第一次偏离对应基线：

[
u_a^{attack}(i) \neq u_a^{baseline}(i)
]

### (t_P)：物理异常时间

水力变量首次超过容差：

[
|x_j^{attack}(i)-x_j^{baseline}(i)|>\epsilon_j
]

建议水位容差：

```text
epsilon = max(0.01 m, 3 × baseline numerical standard deviation)
```

因为当前无噪声时标准差可能为0，所以应设置最小工程容差，避免浮点数误差被当作物理异常。

---

## 7.2 传播时延

计算：

[
\Delta t_{A\rightarrow C}=t_C-t_A
]

[
\Delta t_{C\rightarrow U}=t_U-t_C
]

[
\Delta t_{U\rightarrow P}=t_P-t_U
]

[
\Delta t_{A\rightarrow P}=t_P-t_A
]

由于水力模型步长为300秒，而网络和控制事件使用真实墙钟时间，需要同时报告两种时间：

* 实际执行时间：ms 或 s；
* 水力仿真时间：iteration × 300 s。

例如：

```text
首次 Modbus 超时发生在 wall-clock 42 ms 后；
首次泵状态偏离发生在第21轮；
首次水位显著偏离发生在第23轮，
对应仿真时间延迟10 min。
```

不要把真实运行耗时和模拟水力时间混为一谈。

---

## 7.3 攻击期间指标

对于攻击窗口 ([i_s,i_e])：

[
RMSE^{attack}*j =
\sqrt{
\frac{1}{i_e-i_s+1}
\sum*{i=i_s}^{i_e}
(x^{attack}*{i,j}-x^{baseline}*{i,j})^2
}
]

最大偏差：

[
PeakDeviation_j =
\max_{i_s\le i\le i_{end}}
|x^{attack}*{i,j}-x^{baseline}*{i,j}|
]

累计偏差面积：

[
AUC_j =
\sum_i
|x^{attack}*{i,j}-x^{baseline}*{i,j}|
\Delta t_h
]

其中 (\Delta t_h=300) 秒。

AUC 很重要，因为：

* 峰值只能反映最坏瞬间；
* RMSE 会平均化；
* AUC 可以反映“偏差程度 × 持续时间”。

---

## 7.4 恢复时间

定义攻击结束轮次为 (i_e)。

水力变量进入容差范围并连续保持 (K) 轮：

[
|x_j^{attack}(i)-x_j^{baseline}(i)|\leq\epsilon_j
]

建议：

```text
K = 3 或 5轮
```

恢复时间：

[
T_{recovery}=(i_r-i_e)\Delta t_h
]

如果直到仿真结束都未恢复，则输出：

```text
not recovered within observation window
```

不能强行填入仿真结束时刻。

---

## 7.5 MITM特有指标

* 篡改响应数量；
* 篡改成功率；
* 原始值与伪造值平均差；
* SCADA 接收伪造值持续时间；
* 由伪造值引起的错误控制次数；
* 首次错误控制延迟。

```text
mitm_success_rate =
modified_responses / intercepted_responses
```

---

## 7.6 DoS特有指标

* 攻击包发送数；
* 攻击吞吐量；
* 合法流量吞吐量；
* Modbus 超时数；
* 请求成功率；
* 丢包率；
* 控制数据陈旧度；
* 连续失联最长时间。

数据陈旧度可定义为：

[
Age(i)=t_i-t_{\text{last successful update}}
]

对每个 PLC 统计：

* 平均 Age；
* 最大 Age；
* Age 超过一个控制周期的比例。

这比单纯报告丢包率更能说明 DoS 对控制系统的影响。

---

## 7.7 PLC逻辑注入特有指标

* 注入开始和完成时刻；
* 注入前后逻辑哈希；
* 注入后首次输出改变时间；
* 异常开关次数；
* 逻辑注入导致的控制状态偏离持续时间；
* 水力最大偏差和恢复时间。

编译或部署后的 PLC 程序建议保存 SHA-256：

```python
import hashlib

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
```

写入：

```json
{
  "plc": "PLC4",
  "logic_hash_before": "...",
  "logic_hash_after": "...",
  "injection_iteration": 20
}
```

---

# 八、平台性能与扩展性实验

## 8.1 资源监控器

新增：

```python
# src/metrics/runtime_monitor.py

import csv
import os
import time
from pathlib import Path

import psutil


class RuntimeMonitor:
    def __init__(self, output_file: Path, interval_sec: float = 0.5):
        self.output_file = output_file
        self.interval_sec = interval_sec

    def sample(self, process_names: dict[str, int]) -> None:
        timestamp_ns = time.time_ns()

        rows = []
        for component, pid in process_names.items():
            try:
                proc = psutil.Process(pid)
                memory = proc.memory_info()

                rows.append({
                    "timestamp_ns": timestamp_ns,
                    "component": component,
                    "pid": pid,
                    "cpu_percent": proc.cpu_percent(interval=None),
                    "rss_bytes": memory.rss,
                    "vms_bytes": memory.vms,
                    "num_threads": proc.num_threads(),
                    "read_bytes": proc.io_counters().read_bytes,
                    "write_bytes": proc.io_counters().write_bytes,
                })
            except psutil.Error:
                continue

        self._append(rows)
```

需要监控：

* coordinator；
* physics；
* SCADA；
* 每个 OpenPLC；
* ns-3；
* attacker；
* adapters。

同时记录整个进程树，而不是只看父进程。

---

## 8.2 阶段耗时埋点

在 `run_all` 或其 Python 等价入口中记录：

```text
config_generation_start/end
plc_compile_start/end
namespace_setup_start/end
ns3_start/end
openplc_start/end
scada_start/end
physics_start/end
simulation_start/end
cleanup_start/end
```

输出：

```csv
phase,start_ns,end_ns,duration_ms
config_generation,...,...,183.2
plc_compile,...,...,5412.8
namespace_setup,...,...,902.5
simulation,...,...,48231.4
```

---

## 8.3 PLC数量实验

当前完整配置包含 PLC1、PLC2、PLC3、PLC4、PLC5、PLC7、PLC8 和 PLC9。

可以自动生成子场景：

```text
1 PLC
2 PLCs
4 PLCs
6 PLCs
8 PLCs
```

需要注意：不能随意删除 PLC 后仍宣称运行完整 C-Town 控制。

建议两种方式：

### 方法A：网络扩展性

保留一个真实 PLC 执行控制，其余 PLC 作为通信节点或镜像负载节点。

用于测量：

* namespace 数量；
* OpenPLC 进程数量；
* Modbus 连接数量；
* ns-3 节点数量。

### 方法B：C-Town功能子集

按控制依赖构造有效子系统。

例如只保留某个水箱、对应传感器和对应执行器。

论文中应称为：

> reduced C-Town configurations

不能称为完整 C-Town。

---

## 8.4 仿真轮数实验

设置：

```text
25
50
100
200
500
1000轮
```

分析：

[
MeanIterationTime =
\frac{
T_{simulation}
}{
N
}
]

实时系数：

[
RealTimeFactor =
\frac{
N \times HydraulicStep
}{
WallClockRuntime
}
]

例如：

* 100轮；
* 每轮300秒；
* 模拟时间30000秒；
* 实际运行时间50秒；

则：

[
RealTimeFactor=600
]

即平台以约600倍于真实水力时间的速度推进。

不过由于 ns-3 使用 `realtime` scheduler，此处应说明“实时调度”指 ns-3 事件与墙钟同步机制，不代表整个水力仿真必须按300秒真实等待。当前配置确实设置了 `scheduler: realtime`。

---

## 8.5 攻击节点扩展性

已有单 bot 和三 bot DoS 配置，可以继续生成：

```text
0
1
3
5
10
20 bots
```

测量：

* ns-3 CPU；
* 攻击吞吐量；
* 合法请求成功率；
* 单轮耗时；
* PCAP 大小；
* 峰值内存。

重点不是展示 bot 越多破坏越大，而是区分：

1. 攻击强度变化；
2. 平台计算开销变化。

如果每个 bot 的发送速率不变，总攻击速率会随着 bot 数变化。论文必须明确：

```text
per-bot rate
aggregate rate
packet size
start/end iteration
```

---

# 九、实验配置生成器

不要手工维护几十个 YAML。

建议建立模板：

```yaml
experiment:
  name: network_delay_020ms_run_01
  group: network_delay
  repetition: 1
  random_seed: 1001

metrics:
  enabled: true
  event_log: true
  resource_monitor: true
  flow_monitor: true
  pcap: true

network_overrides:
  target_links:
    - r0-r2
  delay: 20ms
  data_rate: 100Mbps
  loss_rate: 0.0

attacks:
  enabled: false
```

生成器：

```python
from copy import deepcopy
from pathlib import Path
import yaml


def set_named_link(config: dict, link_name: str, **updates) -> None:
    links = config["network"]["backbone_links"]

    for link in links:
        if link["name"] == link_name:
            link.update(updates)
            return

    raise KeyError(f"Unknown network link: {link_name}")


def generate_delay_configs(
    base_config: dict,
    output_dir: Path,
    delays_ms: list[int],
    repetitions: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    for delay_ms in delays_ms:
        for repetition in range(1, repetitions + 1):
            config = deepcopy(base_config)

            set_named_link(
                config,
                "r0-r2",
                delay=f"{delay_ms}ms",
            )

            config["experiment"] = {
                "group": "network_delay",
                "parameter": "delay_ms",
                "value": delay_ms,
                "repetition": repetition,
            }

            output = output_dir / (
                f"delay_{delay_ms:03d}ms_run_{repetition:02d}.yaml"
            )

            output.write_text(
                yaml.safe_dump(config, sort_keys=False),
                encoding="utf-8",
            )
```

---

# 十、实验清单与建议规模

## 10.1 最小可投稿版本

### 闭环正确性

```text
1个基线场景 × 5次重复 = 5次
```

### 网络时延

```text
7个时延等级 × 5次 = 35次
```

### DoS强度

```text
无攻击、单bot、三bot × 5次 = 15次
```

### 三类攻击

```text
MITM、DoS、PLC逻辑注入 × 5次 = 15次
```

### 性能

```text
5种规模 × 5次 = 25次
```

总计约95次。

---

## 10.2 完整版本

| 实验组   | 参数        | 水平数 | 重复数 | 总次数 |
| ----- | --------- | --: | --: | --: |
| 闭环一致性 | baseline  |   1 |  10 |  10 |
| 时延准确性 | 0–100ms   |   7 |  10 |  70 |
| 丢包准确性 | 0–10%     |   7 |  10 |  70 |
| 带宽影响  | 1–100Mbps |   6 |   5 |  30 |
| DoS强度 | 0–20 bots |   6 |   5 |  30 |
| MITM  | 不同篡改幅度    |   4 |   5 |  20 |
| PLC注入 | 不同阈值变化    |   4 |   5 |  20 |
| PLC规模 | 1–8 PLC   |   5 |   5 |  25 |
| 仿真轮数  | 25–1000   |   6 |   5 |  30 |

总计约305次。

会议论文没有必要全部做。优先顺序应是：

```text
闭环正确性
→ 网络时延准确性
→ 三类攻击统一指标
→ 基础性能
→ 丢包、带宽与大规模扩展
```

---

# 十一、统一结果分析脚本

建议最终的 `summary_metrics.csv` 一行对应一次实验：

```csv
experiment_id,group,repetition,delay_ms,loss_rate,bot_count,
runtime_sec,mean_iteration_ms,peak_rss_mb,
modbus_requests,modbus_success_rate,modbus_timeout_rate,
network_mean_delay_ms,network_loss_rate,
tank_rmse_mean,tank_max_deviation,
actuator_mismatch_rate,
attack_to_comm_ms,comm_to_control_iterations,
control_to_physics_iterations,recovery_iterations
```

批量汇总后：

```python
import pandas as pd


runs = pd.read_csv("all_summary_metrics.csv")

summary = (
    runs.groupby(["group", "delay_ms"], dropna=False)
    .agg(
        runtime_mean=("runtime_sec", "mean"),
        runtime_std=("runtime_sec", "std"),
        rtt_mean=("modbus_rtt_ms", "mean"),
        rtt_std=("modbus_rtt_ms", "std"),
        success_mean=("modbus_success_rate", "mean"),
        success_std=("modbus_success_rate", "std"),
    )
    .reset_index()
)
```

所有论文图都从该汇总文件生成，避免手工复制数据。

---

# 十二、论文应生成的图和表

## 图1：闭环正确性

两张并排：

* 参考组与平台组水位曲线；
* 水位绝对误差曲线。

## 表1：闭环误差

| 变量 | RMSE | MAE | 最大误差 |
| -- | ---: | --: | ---: |
| T1 |    … |   … |    … |
| T2 |    … |   … |    … |
| …  |    … |   … |    … |

另附执行器：

| 执行器 | 状态不一致率 | 平均切换误差 |
| --- | -----: | -----: |

---

## 图2：配置时延与实测时延

横轴：

```text
Configured delay
```

纵轴：

```text
Measured delay
```

两条曲线：

* 理想理论值；
* 实测值，带标准差误差棒。

这是导师要求“量化 ns-3 + namespace 优势”最重要的一张图。

---

## 图3：网络条件对控制通信的影响

横轴：

```text
Delay 或 loss rate
```

纵轴：

* Modbus RTT；
* 请求成功率；
* 每轮执行时间。

可以拆成三张图，不建议使用双纵轴堆在一张图中。

---

## 图4：三类攻击跨层传播

为每种攻击绘制一条时间线：

```text
attack
   ↓
communication anomaly
   ↓
control deviation
   ↓
physical deviation
   ↓
recovery
```

也可以用堆叠条形图表示各阶段耗时。

---

## 表2：攻击量化对比

| 指标      | MITM | DoS | PLC逻辑注入 |
| ------- | ---: | --: | ------: |
| 首次通信异常  |    … |   … |       … |
| 首次控制偏离  |    … |   … |       … |
| 首次物理偏离  |    … |   … |       … |
| 最大水位偏差  |    … |   … |       … |
| 水位RMSE  |    … |   … |       … |
| 执行器不一致率 |    … |   … |       … |
| 恢复时间    |    … |   … |       … |

---

## 图5：平台扩展性

横轴：

```text
PLC数量 或 仿真轮数
```

纵轴：

* 总运行时间；
* 单轮耗时；
* 峰值内存。

---

# 十三、实现顺序

## 第一步：先做日志，不改实验

完成：

```text
events.csv
communication.csv
resources.csv
manifest.json
```

验证基线运行结果不受影响。

---

## 第二步：完成基线分析器

输入：

```text
reference physics.csv
platform physics.csv
```

输出：

```text
RMSE
MAE
最大误差
执行器状态不一致率
切换时刻误差
```

这是最容易先完成并写进论文的部分。

---

## 第三步：接入 ns-3 FlowMonitor

输出：

```text
flow-monitor.xml
network.csv
```

然后完成时延实验。

---

## 第四步：统一攻击时间线

让 MITM、DoS 和 PLC 注入模块都调用相同的 `EventLogger`。

建立：

```text
tA
tC
tU
tP
tRecovery
```

---

## 第五步：增加资源监控和批量实验

完成：

```text
run_experiment_matrix.py
runtime_monitor.py
summary_metrics.csv
```

---

# 十四、最需要避免的问题

1. **不要只使用 ping 证明网络准确性。**
   Ping 可以做辅助验证，但主要业务是 Modbus TCP，应同时报告 FlowMonitor 和 Modbus 请求时延。

2. **不要把链路时延直接和应用 RTT 比较。**
   两者中间还有多跳路径、队列、协议和 PLC 处理开销。

3. **不要只跑一次实验。**
   实时调度、多进程和操作系统负载都会引入波动。

4. **不要只报告 RMSE。**
   RMSE 需要配合最大偏差、状态不一致率、恢复时间和传播时间。

5. **不要把真实运行时间与水力模拟时间混淆。**

6. **不要让详细日志改变实验结果。**
   高频包事件留在 PCAP/FlowMonitor；Python CSV 只记录应用层关键事件。

7. **不要在不同实验中覆盖同一个 output 目录。**
   每次运行必须使用唯一 `experiment_id`。

8. **必须保存解析后的最终配置。**
   论文数据必须能追溯到具体 YAML、代码提交和随机种子。

---

# 十五、每次实验的可复现元数据

`manifest.json` 至少保存：

```json
{
  "experiment_id": "dos_3bots_run_03",
  "timestamp": "2026-07-15T10:30:00+08:00",
  "git_commit": "abcdef123456",
  "config_file": "config_dos_plc2_three_bots.yaml",
  "config_sha256": "...",
  "host": {
    "os": "Ubuntu 24.04 LTS",
    "cpu": "Intel Core i9-14900K",
    "memory_gb": 128
  },
  "software": {
    "python": "...",
    "ns3": "...",
    "openplc_commit": "...",
    "epanet_backend": "DHALSIM-epynet"
  },
  "random_seed": 1003,
  "iterations": 100,
  "hydraulic_step_sec": 300
}
```

这部分最终可以作为论文“实验复现性”的支撑。

---

## 推荐的最先落地版本

第一轮代码只实现以下六项：

```text
1. EventLogger
2. Modbus请求时延与成功/超时记录
3. 物理状态和执行器状态统一输出
4. 基线RMSE、MAE、最大误差和状态不一致率
5. ns-3 FlowMonitor
6. 批量时延实验生成与汇总
```

完成这六项后，就已经能够支撑论文最关键的两组定量结论：

> 完整联合仿真闭环与参考模型保持一致；

> ns-3 配置的网络条件能够被实际测量，并真实影响 Modbus 控制通信。

仓库目前已经具备网络拓扑、PCAP、攻击配置和完整 C-Town 节点映射，因此上述工作主要是增加可观测性和实验自动化，而不需要重新设计已有仿真架构。([GitHub][2])

[1]: https://github.com/WandeF/hydro-cps-sim "GitHub - WandeF/hydro-cps-sim: A high-fidelity water CPS co-simulation platform for cyber attack evaluation, cross-layer impact analysis, and physics-aware defense research. · GitHub"
[2]: https://github.com/WandeF/hydro-cps-sim/tree/main/examples/c_town "hydro-cps-sim/examples/c_town at main · WandeF/hydro-cps-sim · GitHub"
