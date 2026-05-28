# C-Town MITM 攻击实验说明

`config_mitm.yaml` 是在原始 `config.yaml` 基线配置上的攻击实验配置。当前实现的逻辑是：

```text
攻击配置 -> 生成包含攻击节点的 network.sh/ns3_network.cc -> 启动常驻 MITM 代理和 DNAT -> 闭环运行时按轮次切换攻击状态
```

## 网络拓扑扩展

`config_mitm.yaml` 在 `network.nodes.endpoints` 中增加攻击端点：

```text
mitm / ns-attacker / tap-attacker / 192.168.255.100
```

并把它接入 `scada_lan`。因此：

- `network.sh` 会自动创建 `ns-attacker`、`tap-attacker`、bridge、veth 和默认路由；
- `ns3_network.cc` 会自动创建 ns-3 中的 `mitm` 节点，并通过 TapBridge 接入 SCADA 局域网；
- 后续复合攻击只需要继续在 `attacks.scenarios` 中追加攻击场景，或继续在 `network` 中增加新的攻击节点。

## 轮次触发

当前 MITM 攻击配置为第 20 轮到第 40 轮生效：

```yaml
attacks:
  enabled: true
  scenarios:
    - name: mitm_fake_T7
      enabled: true
      type: modbus_mitm
      trigger:
        type: iteration_window
        start_iteration: 20
        end_iteration: 40
```

这里的窗口是闭环控制轮次窗口。协调器会在释放 `physics_0020.ready` 之前启动攻击代理和 iptables 规则，在释放 `physics_0041.ready` 之前撤销攻击，因此攻击实际覆盖控制轮次 `20..40`。

## 当前 MITM 行为

攻击链路为：

```text
SCADA -> PLC9:502
    在 ns-scada 中被 iptables OUTPUT DNAT 重定向为：
SCADA -> mitm:15020
    攻击代理再连接真实：
mitm -> PLC9:502
```

攻击代理转发 Modbus/TCP 流量，并把 SCADA 从 PLC9 读取到的 `PLC9_T7` 响应值替换为 `5.2`。这样 SCADA 后续会把伪造后的高水位值下发到 PLC4 的跨 PLC 依赖变量，进而影响 PLC4 对 `PU10/PU11` 的控制判断。

MITM 代理和 DNAT 规则在 SCADA daemon 启动前就建立。第 1..19 轮代理透明转发，第 20..40 轮状态文件切换为 active 并篡改匹配响应，第 41 轮后继续透明转发。SCADA 默认保持持久 Modbus/TCP 连接。

## 运行

```bash
bash scripts/run_all.sh examples/c_town/config_mitm.yaml
```

## 结果文件

攻击调度事件：

```text
examples/c_town/output/runtime/csv/attack_schedule.csv
```

攻击篡改事件：

```text
examples/c_town/output/runtime/csv/attack_events.csv
```

`attack_schedule.csv` 用来确认攻击脚本在哪一轮启动/停止；`attack_events.csv` 用来确认具体哪些 Modbus 响应被篡改。

## 配置扩展模板

```yaml
attacks:
  enabled: true
  scenarios:
    - name: mitm_fake_T7
      enabled: true
      type: modbus_mitm
      trigger:
        type: iteration_window
        start_iteration: 20
        end_iteration: 40
      attacker:
        endpoint: mitm
        namespace: ns-attacker
        ip: 192.168.255.100
      intercept:
        source: scada
        targets: [PLC9]
        protocol: modbus_tcp
        port: 502
        listen_port_base: 15020
        redirect: iptables_output_dnat
      rules:
        - name: replace_plc9_t7_with_high_level
          target: PLC9
          variable: PLC9_T7
          direction: response
          function_codes: [3]
          operation: set
          value: 5.2
          window:
            start_after_sec: 0.0
```

支持的 `operation`：`set`、`add`/`bias`、`multiply`/`scale`。`trigger` 用于按仿真轮次切换攻击 active 状态；`rules[].window` 是攻击代理启动后的墙钟时间窗口，通常保持 `start_after_sec: 0.0` 即可。

## 结果对比

```bash
python3 scripts/compare_attack_results.py \
  --baseline examples/c_town/baseline \
  --attack examples/c_town/output/runtime/csv/physics.csv \
  --start 20 \
  --end 40 \
  --columns PU10 PU11 T7
```

脚本会输出攻击窗口 `20..40` 内 `PU10`、`PU11`、`T7` 的逐轮差异，并生成：

```text
examples/c_town/output/runtime/csv/attack_baseline_compare.csv
examples/c_town/output/runtime/csv/attack_baseline_summary.csv
```

如果 `runtime/csv` 由 sudo 运行产生且普通用户不可写，脚本会自动改写到：

```text
examples/c_town/output/check/attack_compare/
```
