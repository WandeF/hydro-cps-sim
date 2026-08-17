"""
模块说明：
    本脚本用于根据 YAML 配置自动生成 ns-3 C++ 网络仿真代码，是项目中“配置文件 →
    可编译 ns-3 拓扑程序”的代码生成器。

主要功能：
    1. 读取 config.yaml 中的节点、主干链路、局域网和 Tap 配置；
    2. 生成路由器、交换机、端点等节点创建代码；
    3. 生成主干 PointToPoint 链路配置代码；
    4. 生成局域网 CSMA、Bridge 以及 TapBridge 接入代码；
    5. 配置 IP 地址、掩码、路由和可选的 PCAP 抓包；
    6. 输出完整的 ns-3 C++ 源文件，供后续编译运行。

输入：
    YAML 网络配置文件，包含 routers、switches、endpoints、backbone_links、lans 等定义。

输出：
    一份可用于 ns-3 编译的 C++ 网络拓扑源代码。

适用场景：
    用于在多 PLC / SCADA / 路由器组成的 CPS 网络中自动生成实验拓扑，减少手工编写
    ns-3 场景代码的工作量，并保证网络结构与项目配置保持一致。
"""
import sys
import re
import json
import yaml
import ipaddress
from pathlib import Path
from typing import Any


def ident(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def load_yaml(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_output_dir(config_path: Path, config: dict) -> Path:
    raw = config.get("output_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("config file missing valid top-level 'output_path' field")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        return (config_path.parent / p).resolve()
    if p.exists():
        return p.resolve()
    local_output = (config_path.parent / "output").resolve()
    if local_output.exists():
        return local_output
    return p.resolve()


def time_value_expr(s: str) -> str:
    s = str(s).strip()
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ns|us|ms|s)", s)
    if not m:
        raise ValueError(f"Unsupported time format: {s}")

    value = float(m.group(1))
    unit = m.group(2)

    factor = {
        "ns": 1e-9,
        "us": 1e-6,
        "ms": 1e-3,
        "s": 1.0,
    }[unit]

    seconds = value * factor
    return f"TimeValue (Seconds ({seconds:.9f}))"


def time_expr(s: str) -> str:
    """Return an ns-3 Time expression rather than an AttributeValue wrapper."""
    value = time_value_expr(s)
    return value[len("TimeValue (") : -1]


def cidr_to_network_and_mask(cidr: str):
    net = ipaddress.ip_network(cidr, strict=False)
    return str(net.network_address), str(net.netmask)


def ip_only(addr_with_prefix: str):
    return str(ipaddress.ip_interface(addr_with_prefix).ip)


def cpp_string(value: str | Path) -> str:
    """Return a C++ string literal with paths/labels safely escaped."""
    return json.dumps(str(value), ensure_ascii=True)


def measurement_options(network_cfg: dict[str, Any]) -> dict[str, Any]:
    """Normalize optional network.measurement feature flags."""
    raw = network_cfg.get("measurement", {})
    if not isinstance(raw, dict) or not bool(raw.get("enabled", False)):
        return {
            "enabled": False,
            "flow_monitor": False,
            "link_metrics": False,
            "pcap": False,
            "pcap_links": None,
            "interval": "1s",
            "queue_timeseries": False,
            "queue_interval": "10ms",
        }

    link_raw = raw.get("link_metrics", False)
    link_enabled = bool(link_raw.get("enabled", False)) if isinstance(link_raw, dict) else bool(link_raw)
    interval = (
        link_raw.get("interval", "1s")
        if isinstance(link_raw, dict)
        else raw.get(
            "link_metrics_interval",
            raw.get("snapshot_interval", raw.get("snapshot_interval_sec", "1s")),
        )
    )
    if isinstance(interval, (int, float)):
        interval = f"{interval}s"
    # Validate eagerly so malformed experiment matrices fail during generation.
    interval_expr = time_value_expr(str(interval))
    match = re.search(r"Seconds \(([0-9.]+)\)", interval_expr)
    if match is None or float(match.group(1)) <= 0:
        raise ValueError("network.measurement link metric interval must be positive")

    raw_pcap_links = raw.get("pcap_links")
    if raw_pcap_links is None:
        pcap_links = None
    elif isinstance(raw_pcap_links, (list, tuple)):
        pcap_links = {str(item).strip() for item in raw_pcap_links if str(item).strip()}
    else:
        raise ValueError("network.measurement.pcap_links must be a list")

    queue_raw = raw.get("queue_timeseries", False)
    queue_enabled = bool(queue_raw.get("enabled", False)) if isinstance(queue_raw, dict) else bool(queue_raw)
    queue_interval = queue_raw.get("interval", "10ms") if isinstance(queue_raw, dict) else "10ms"
    if isinstance(queue_interval, (int, float)):
        queue_interval = f"{queue_interval}s"
    queue_interval_expr = time_value_expr(str(queue_interval))
    queue_match = re.search(r"Seconds \(([0-9.]+)\)", queue_interval_expr)
    if queue_match is None or float(queue_match.group(1)) <= 0:
        raise ValueError("network.measurement queue timeseries interval must be positive")

    return {
        "enabled": True,
        "flow_monitor": bool(raw.get("flow_monitor", False)),
        "link_metrics": link_enabled,
        "pcap": bool(raw.get("pcap", False)),
        "pcap_links": pcap_links,
        "interval": str(interval),
        "queue_timeseries": queue_enabled,
        "queue_interval": str(queue_interval),
    }


def _queue_type(queue_cfg: dict[str, Any]) -> str:
    raw = str(queue_cfg.get("type", "DropTailQueue")).strip()
    supported = {"DropTailQueue", "ns3::DropTailQueue", "DropTailQueue<Packet>", "ns3::DropTailQueue<Packet>"}
    if raw not in supported:
        raise ValueError(f"Unsupported point-to-point queue type: {raw}")
    return "ns3::DropTailQueue<Packet>"


def _error_unit_expr(unit: str) -> str:
    normalized = str(unit).strip().lower()
    mapping = {
        "packet": "RateErrorModel::ERROR_UNIT_PACKET",
        "byte": "RateErrorModel::ERROR_UNIT_BYTE",
        "bit": "RateErrorModel::ERROR_UNIT_BIT",
    }
    if normalized not in mapping:
        raise ValueError(f"Unsupported rate error unit: {unit}")
    return mapping[normalized]


def build_endpoint_map(network_cfg):
    """
    从 network.nodes.endpoints 中读取 endpoint 的 tap / namespace / tap_mode 信息
    配置示例：
      - name: PLC1
        role: plc
        namespace: ns-plc1
        tap: tap-plc1
        tap_mode: use_bridge   # 可选，默认 use_bridge
    """
    endpoint_map = {}
    for item in network_cfg.get("nodes", {}).get("endpoints", []):
        name = item["name"]
        tap_name = item.get("tap")
        namespace = item.get("namespace", "")
        tap_mode = item.get("tap_mode", "use_bridge")

        endpoint_map[name] = {
            "tap_name": tap_name,
            "namespace": namespace,
            "mode": tap_mode,
            "role": item.get("role", ""),
        }
    return endpoint_map


def tap_mode_to_ns3(mode: str) -> str:
    mode = str(mode).strip().lower()
    if mode == "use_local":
        return "UseLocal"
    if mode == "use_bridge":
        return "UseBridge"
    raise ValueError(
        f"Unsupported tap mode: {mode}. Supported: 'use_local', 'use_bridge'."
    )


def emit_header(*, flow_monitor: bool = False, link_metrics: bool = False):
    includes = r'''#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/csma-module.h"
#include "ns3/tap-bridge-module.h"
#include "ns3/bridge-module.h"
#include "ns3/ipv4-global-routing-helper.h"
'''
    if flow_monitor:
        includes += '#include "ns3/flow-monitor-module.h"\n'
    includes += r'''
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iomanip>

#include <iostream>
#include <map>
#include <string>
#include <unordered_map>

using namespace ns3;

static void
AssignIpv4Exact (Ptr<Node> node,
                 Ptr<NetDevice> device,
                 const std::string &ip,
                 const std::string &mask,
                 uint32_t metric = 1)
{
  Ptr<Ipv4> ipv4 = node->GetObject<Ipv4> ();
  NS_ABORT_MSG_IF (ipv4 == nullptr, "Node has no IPv4 stack installed");

  int32_t ifIndex = ipv4->GetInterfaceForDevice (device);
  if (ifIndex == -1)
    {
      ifIndex = ipv4->AddInterface (device);
    }

  ipv4->AddAddress (
      ifIndex,
      Ipv4InterfaceAddress (Ipv4Address (ip.c_str ()), Ipv4Mask (mask.c_str ())));

  ipv4->SetMetric (ifIndex, metric);
  ipv4->SetUp (ifIndex);
}

static std::string g_stopFilePath;

static void
PollStopFile ()
{
  if (!g_stopFilePath.empty () && std::filesystem::exists (g_stopFilePath))
    {
      NS_LOG_UNCOND ("[METRICS] graceful ns-3 stop requested by " << g_stopFilePath);
      Simulator::Stop ();
      return;
    }
  Simulator::Schedule (MilliSeconds (100), &PollStopFile);
}
'''
    if not link_metrics:
        return includes

    return includes + r'''

// FlowMonitor observes packets created by an ns-3 IP stack.  Hydro-CPS-Sim's
// real Modbus packets enter and leave through TapBridge, so they can lack the
// FlowProbe tag.  These device-level counters deliberately do not depend on it.
struct LinkDirectionMetrics
{
  std::string link;
  std::string direction;
  std::string source;
  std::string target;
  std::string configuredDelay;
  std::string configuredDataRate;
  double configuredErrorRate {0.0};
  std::string configuredErrorUnit;
  uint64_t txPackets {0};
  uint64_t rxPackets {0};
  uint64_t txBytes {0};
  uint64_t rxBytes {0};
  uint64_t dropPackets {0};
  uint64_t errorModelDropPackets {0};
  uint64_t queueDropPackets {0};
  uint64_t queueEnqueuePackets {0};
  uint64_t queueDequeuePackets {0};
  uint64_t queueCapacityPackets {0};
  uint64_t queuePacketsCurrent {0};
  uint64_t queuePacketsMaximum {0};
  uint64_t queuePacketsSamples {1};
  uint64_t queuePacketsSum {0};
  uint64_t delaySamples {0};
  int64_t delaySumNs {0};
  int64_t maxDelayNs {0};
  int64_t firstQueueNonzeroNs {-1};
  int64_t firstQueueFullNs {-1};
  int64_t firstQueueDropNs {-1};
  std::unordered_map<uint64_t, int64_t> pendingTxNs;
};

static std::map<std::string, LinkDirectionMetrics> g_linkMetrics;
static std::string g_linkMetricsPath;
static Time g_linkMetricsInterval = Seconds (1.0);
static bool g_queueTimeseriesEnabled = false;
static std::string g_queueTimeseriesPath;
static Time g_queueTimeseriesInterval = MilliSeconds (10);

static std::string
CsvEscape (const std::string &value)
{
  if (value.find_first_of (",\"\n\r") == std::string::npos)
    {
      return value;
    }
  std::string escaped = "\"";
  for (char ch : value)
    {
      escaped += ch;
      if (ch == '\"')
        {
          escaped += '\"';
        }
    }
  return escaped + "\"";
}

static void
LinkTx (LinkDirectionMetrics *metrics, Ptr<const Packet> packet)
{
  metrics->txPackets++;
  metrics->txBytes += packet->GetSize ();
  metrics->pendingTxNs[packet->GetUid ()] = Simulator::Now ().GetNanoSeconds ();
}

static void
LinkRx (LinkDirectionMetrics *metrics, Ptr<const Packet> packet)
{
  metrics->rxPackets++;
  metrics->rxBytes += packet->GetSize ();
  auto found = metrics->pendingTxNs.find (packet->GetUid ());
  if (found == metrics->pendingTxNs.end ())
    {
      return;
    }
  int64_t delayNs = Simulator::Now ().GetNanoSeconds () - found->second;
  metrics->delaySamples++;
  metrics->delaySumNs += delayNs;
  metrics->maxDelayNs = std::max (metrics->maxDelayNs, delayNs);
  metrics->pendingTxNs.erase (found);
}

static void
LinkErrorDrop (LinkDirectionMetrics *metrics, Ptr<const Packet> packet)
{
  metrics->errorModelDropPackets++;
  metrics->dropPackets++;
  metrics->pendingTxNs.erase (packet->GetUid ());
}

static void
QueueEnqueue (LinkDirectionMetrics *metrics, Ptr<const Packet> packet)
{
  (void) packet;
  metrics->queueEnqueuePackets++;
  metrics->queuePacketsCurrent++;
  if (metrics->firstQueueNonzeroNs < 0 && metrics->queuePacketsCurrent > 0)
    {
      metrics->firstQueueNonzeroNs = Simulator::Now ().GetNanoSeconds ();
    }
  if (metrics->firstQueueFullNs < 0 && metrics->queueCapacityPackets > 0 &&
      metrics->queuePacketsCurrent >= metrics->queueCapacityPackets)
    {
      metrics->firstQueueFullNs = Simulator::Now ().GetNanoSeconds ();
    }
  metrics->queuePacketsMaximum = std::max (
      metrics->queuePacketsMaximum, metrics->queuePacketsCurrent);
  metrics->queuePacketsSum += metrics->queuePacketsCurrent;
  metrics->queuePacketsSamples++;
}

static void
QueueDequeue (LinkDirectionMetrics *metrics, Ptr<const Packet> packet)
{
  (void) packet;
  metrics->queueDequeuePackets++;
  if (metrics->queuePacketsCurrent > 0)
    {
      metrics->queuePacketsCurrent--;
    }
  metrics->queuePacketsSum += metrics->queuePacketsCurrent;
  metrics->queuePacketsSamples++;
}

static void
QueueDrop (LinkDirectionMetrics *metrics, Ptr<const Packet> packet)
{
  metrics->queueDropPackets++;
  metrics->dropPackets++;
  if (metrics->firstQueueDropNs < 0)
    {
      metrics->firstQueueDropNs = Simulator::Now ().GetNanoSeconds ();
    }
  metrics->pendingTxNs.erase (packet->GetUid ());
  metrics->queuePacketsSum += metrics->queuePacketsCurrent;
  metrics->queuePacketsSamples++;
}

static void
RegisterLinkDirection (const std::string &key,
                       const std::string &link,
                       const std::string &direction,
                       const std::string &sourceName,
                       const std::string &targetName,
                       const std::string &configuredDelay,
                       const std::string &configuredDataRate,
                       double configuredErrorRate,
                       const std::string &configuredErrorUnit,
                       Ptr<NetDevice> sourceDevice,
                       Ptr<NetDevice> targetDevice)
{
  LinkDirectionMetrics &metrics = g_linkMetrics[key];
  metrics.link = link;
  metrics.direction = direction;
  metrics.source = sourceName;
  metrics.target = targetName;
  metrics.configuredDelay = configuredDelay;
  metrics.configuredDataRate = configuredDataRate;
  metrics.configuredErrorRate = configuredErrorRate;
  metrics.configuredErrorUnit = configuredErrorUnit;
  sourceDevice->TraceConnectWithoutContext (
      "MacTx", MakeBoundCallback (&LinkTx, &metrics));
  targetDevice->TraceConnectWithoutContext (
      "MacRx", MakeBoundCallback (&LinkRx, &metrics));
  targetDevice->TraceConnectWithoutContext (
      "PhyRxDrop", MakeBoundCallback (&LinkErrorDrop, &metrics));
  Ptr<PointToPointNetDevice> pointToPoint =
      DynamicCast<PointToPointNetDevice> (sourceDevice);
  if (pointToPoint != nullptr && pointToPoint->GetQueue () != nullptr)
    {
      metrics.queueCapacityPackets = pointToPoint->GetQueue ()->GetMaxSize ().GetValue ();
      pointToPoint->GetQueue ()->TraceConnectWithoutContext (
          "Enqueue", MakeBoundCallback (&QueueEnqueue, &metrics));
      pointToPoint->GetQueue ()->TraceConnectWithoutContext (
          "Dequeue", MakeBoundCallback (&QueueDequeue, &metrics));
      pointToPoint->GetQueue ()->TraceConnectWithoutContext (
          "Drop", MakeBoundCallback (&QueueDrop, &metrics));
    }
}

static void
WriteLinkMetricsSnapshotOnce ()
{
  const std::string temporary = g_linkMetricsPath + ".tmp";
  std::ofstream output (temporary, std::ios::out | std::ios::trunc);
  if (!output)
    {
      NS_LOG_UNCOND ("[METRICS][WARN] cannot open " << temporary);
      return;
    }
  output << "simulation_time_s,link,direction,source,target,configured_delay,"
            "configured_data_rate,configured_error_rate,configured_error_unit,"
            "tx_packets,rx_packets,tx_bytes,rx_bytes,"
      "drop_packets,queue_drop_packets,queue_packets_mean,queue_packets_max,"
            "error_model_drop_packets,queue_enqueue_packets,queue_dequeue_packets,"
            "queue_capacity_packets,queue_packets_current,queue_samples,"
            "queue_occupancy_ratio_mean,queue_occupancy_ratio_max,"
            "first_queue_nonzero_time_s,first_queue_full_time_s,first_queue_drop_time_s,"
            "delay_samples,mean_delay_ms,"
            "max_delay_ms,pending_packets\n";
  output << std::fixed << std::setprecision (9);
  const double now = Simulator::Now ().GetSeconds ();
  for (const auto &entry : g_linkMetrics)
    {
      const LinkDirectionMetrics &m = entry.second;
      const double meanDelayMs = m.delaySamples == 0
          ? 0.0
          : static_cast<double> (m.delaySumNs) / static_cast<double> (m.delaySamples) / 1e6;
      const double meanQueuePackets = m.queuePacketsSamples == 0
          ? 0.0
          : static_cast<double> (m.queuePacketsSum) /
            static_cast<double> (m.queuePacketsSamples);
      const double meanQueueRatio = m.queueCapacityPackets == 0
          ? 0.0 : meanQueuePackets / static_cast<double> (m.queueCapacityPackets);
      const double maxQueueRatio = m.queueCapacityPackets == 0
          ? 0.0 : static_cast<double> (m.queuePacketsMaximum) /
            static_cast<double> (m.queueCapacityPackets);
      output << now << ',' << CsvEscape (m.link) << ',' << CsvEscape (m.direction) << ','
             << CsvEscape (m.source) << ',' << CsvEscape (m.target) << ','
             << CsvEscape (m.configuredDelay) << ',' << CsvEscape (m.configuredDataRate) << ','
             << m.configuredErrorRate << ',' << CsvEscape (m.configuredErrorUnit) << ','
             << m.txPackets << ',' << m.rxPackets << ',' << m.txBytes << ',' << m.rxBytes << ','
             << m.dropPackets << ',' << m.queueDropPackets << ',' << meanQueuePackets << ','
             << m.queuePacketsMaximum << ',' << m.errorModelDropPackets << ','
             << m.queueEnqueuePackets << ',' << m.queueDequeuePackets << ','
             << m.queueCapacityPackets << ',' << m.queuePacketsCurrent << ','
             << m.queuePacketsSamples << ',' << meanQueueRatio << ',' << maxQueueRatio << ','
             << (m.firstQueueNonzeroNs < 0 ? -1.0 : m.firstQueueNonzeroNs / 1e9) << ','
             << (m.firstQueueFullNs < 0 ? -1.0 : m.firstQueueFullNs / 1e9) << ','
             << (m.firstQueueDropNs < 0 ? -1.0 : m.firstQueueDropNs / 1e9) << ','
             << m.delaySamples << ',' << meanDelayMs << ','
             << static_cast<double> (m.maxDelayNs) / 1e6 << ',' << m.pendingTxNs.size () << '\n';
    }
  output.close ();
  std::error_code error;
  std::filesystem::rename (temporary, g_linkMetricsPath, error);
  if (error)
    {
      NS_LOG_UNCOND ("[METRICS][WARN] cannot publish link metrics: " << error.message ());
      std::filesystem::remove (temporary);
    }
}

static void
WriteQueueTimeseriesSnapshot ()
{
  if (!g_queueTimeseriesEnabled)
    {
      return;
    }
  const bool newFile = !std::filesystem::exists (g_queueTimeseriesPath);
  std::ofstream output (g_queueTimeseriesPath, std::ios::out | std::ios::app);
  if (!output)
    {
      NS_LOG_UNCOND ("[METRICS][WARN] cannot open " << g_queueTimeseriesPath);
      return;
    }
  if (newFile)
    {
      output << "monotonic_ns,simulation_time_s,link,direction,queue_packets,"
                "queue_capacity,queue_enqueue_count,queue_dequeue_count,queue_drop_count\n";
    }
  const auto monotonicNs = std::chrono::duration_cast<std::chrono::nanoseconds> (
      std::chrono::steady_clock::now ().time_since_epoch ()).count ();
  const double now = Simulator::Now ().GetSeconds ();
  output << std::fixed << std::setprecision (9);
  for (const auto &entry : g_linkMetrics)
    {
      const LinkDirectionMetrics &m = entry.second;
      output << monotonicNs << ',' << now << ',' << CsvEscape (m.link) << ','
             << CsvEscape (m.direction) << ',' << m.queuePacketsCurrent << ','
             << m.queueCapacityPackets << ',' << m.queueEnqueuePackets << ','
             << m.queueDequeuePackets << ',' << m.queueDropPackets << '\n';
    }
  output.close ();
  Simulator::Schedule (g_queueTimeseriesInterval, &WriteQueueTimeseriesSnapshot);
}

static void
WriteLinkMetricsSnapshot ()
{
  WriteLinkMetricsSnapshotOnce ();
  Simulator::Schedule (g_linkMetricsInterval, &WriteLinkMetricsSnapshot);
}
'''


def emit_main_begin(
    network_cfg: dict[str, Any],
    *,
    metrics_dir: Path,
    pcap_dir: Path,
    link_metrics: bool,
    link_metrics_interval: str,
    queue_timeseries: bool = False,
    queue_timeseries_interval: str = "10ms",
    random_seed: int | None = None,
    random_run: int = 1,
):
    lines = []
    lines.append("int")
    lines.append("main (int argc, char *argv[])")
    lines.append("{")

    if network_cfg.get("scheduler") == "realtime":
        lines.append('  GlobalValue::Bind ("SimulatorImplementationType",')
        lines.append('                     StringValue ("ns3::RealtimeSimulatorImpl"));')

    if random_seed is not None:
        if random_seed <= 0:
            raise ValueError("ns-3 random seed must be positive")
        lines.append(f"  RngSeedManager::SetSeed ({int(random_seed)});")
        lines.append(f"  RngSeedManager::SetRun ({max(1, int(random_run))});")

    lines.append('  GlobalValue::Bind ("ChecksumEnabled", BooleanValue (true));')
    lines.append("")
    lines.append(f"  const std::string metricsDir = {cpp_string(metrics_dir)};")
    lines.append("  std::filesystem::create_directories (metricsDir);")
    lines.append(f"  std::filesystem::create_directories ({cpp_string(pcap_dir)});")
    lines.append(f"  g_stopFilePath = {cpp_string(metrics_dir / 'ns3.stop')};")
    if link_metrics:
        lines.append(f"  g_linkMetricsPath = {cpp_string(metrics_dir / 'link-metrics.csv')};")
        lines.append(f"  g_linkMetricsInterval = {time_expr(link_metrics_interval)};")
        if queue_timeseries:
            lines.append("  g_queueTimeseriesEnabled = true;")
            lines.append(f"  g_queueTimeseriesPath = {cpp_string(metrics_dir / 'queue-timeseries.csv')};")
            lines.append(f"  g_queueTimeseriesInterval = {time_expr(queue_timeseries_interval)};")
    lines.append("")
    lines.append("  CommandLine cmd;")
    lines.append("  cmd.Parse (argc, argv);")
    lines.append("")
    lines.append("  std::map<std::string, Ptr<Node>> nodes;")
    lines.append("")
    return "\n".join(lines)


def emit_nodes(network_cfg):
    lines = []
    for group in ("routers", "switches", "endpoints"):
        for item in network_cfg["nodes"].get(group, []):
            name = item["name"]
            var = f"n_{ident(name)}"
            lines.append(f'  Ptr<Node> {var} = CreateObject<Node> ();')
            lines.append(f'  nodes["{name}"] = {var};')
    lines.append("")
    return "\n".join(lines)


def emit_internet_stack(network_cfg):
    routers = network_cfg["nodes"].get("routers", [])
    lines = []
    lines.append("  InternetStackHelper internet;")
    lines.append("  NodeContainer routerNodes;")
    for r in routers:
        lines.append(f'  routerNodes.Add (nodes["{r["name"]}"]);')
    lines.append("  internet.Install (routerNodes);")
    lines.append("")
    return "\n".join(lines)


def emit_backbone_links(
    network_cfg: dict[str, Any],
    *,
    link_metrics: bool = False,
    pcap_dir: Path | None = None,
    pcap_enabled: bool | None = None,
    pcap_links: set[str] | None = None,
):
    lines = []
    if pcap_enabled is None:
        pcap_enabled = bool(network_cfg.get("pcap", False))

    for link in network_cfg.get("backbone_links", []):
        lname = link["name"]
        lvar = ident(lname)

        a, b = link["endpoints"]

        _, mask = cidr_to_network_and_mask(link["subnet"])
        ip_a = ip_only(link["interfaces"][a]["ip"])
        ip_b = ip_only(link["interfaces"][b]["ip"])

        data_rate = link["data_rate"]
        delay_expr = time_value_expr(link["delay"])
        mtu = int(link["mtu"])

        lines.append(f"  // Backbone link: {lname}")
        lines.append(f"  PointToPointHelper p2p_{lvar};")
        lines.append(f'  p2p_{lvar}.SetDeviceAttribute ("DataRate", StringValue ("{data_rate}"));')
        lines.append(f'  p2p_{lvar}.SetDeviceAttribute ("Mtu", UintegerValue ({mtu}));')
        lines.append(f'  p2p_{lvar}.SetChannelAttribute ("Delay", {delay_expr});')

        queue_cfg = link.get("queue")
        if queue_cfg is not None:
            if not isinstance(queue_cfg, dict):
                raise ValueError(f"Backbone link {lname} queue must be a mapping")
            max_packets = int(queue_cfg.get("max_packets", 0))
            if max_packets <= 0:
                raise ValueError(f"Backbone link {lname} queue.max_packets must be positive")
            queue_type = _queue_type(queue_cfg)
            lines.append(
                f'  p2p_{lvar}.SetQueue ("{queue_type}", "MaxSize", '
                f'QueueSizeValue (QueueSize ("{max_packets}p")));'
            )

        lines.append(
            f'  NetDeviceContainer dev_{lvar} = p2p_{lvar}.Install (nodes["{a}"], nodes["{b}"]);'
        )

        configured_error_rate_a_to_b = 0.0
        configured_error_rate_b_to_a = 0.0
        configured_error_unit = "packet"
        error_cfg = link.get("error_model")
        if error_cfg is not None:
            if not isinstance(error_cfg, dict):
                raise ValueError(f"Backbone link {lname} error_model must be a mapping")
            error_type = str(error_cfg.get("type", "rate")).strip().lower()
            if error_type != "rate":
                raise ValueError(f"Unsupported error model type on {lname}: {error_type}")
            error_rate = float(error_cfg.get("error_rate", 0.0))
            if not 0.0 <= error_rate <= 1.0:
                raise ValueError(f"Backbone link {lname} error_rate must be between 0 and 1")
            unit_expr = _error_unit_expr(str(error_cfg.get("unit", "packet")))
            configured_error_unit = str(error_cfg.get("unit", "packet")).strip().lower()
            raw_direction = str(error_cfg.get("direction", "both")).strip().lower().replace("_", "-")
            direction_aliases = {
                "both": (0, 1),
                "a-to-b": (1,),
                "b-to-a": (0,),
                f"{str(a).lower()}-to-{str(b).lower()}": (1,),
                f"{str(b).lower()}-to-{str(a).lower()}": (0,),
            }
            if raw_direction not in direction_aliases:
                raise ValueError(
                    f"Unsupported error model direction on {lname}: {raw_direction}"
                )
            receive_device_indices = direction_aliases[raw_direction]
            configured_error_rate_a_to_b = error_rate if 1 in receive_device_indices else 0.0
            configured_error_rate_b_to_a = error_rate if 0 in receive_device_indices else 0.0
            stream_base = int(error_cfg.get("stream", 1))
            if stream_base < 0:
                raise ValueError(f"error_model.stream must be non-negative on {lname}")
            for offset, device_index in enumerate(receive_device_indices):
                error_var = f"error_{lvar}_{device_index}"
                lines.append(f"  Ptr<RateErrorModel> {error_var} = CreateObject<RateErrorModel> ();")
                lines.append(f'  {error_var}->SetAttribute ("ErrorRate", DoubleValue ({error_rate:.17g}));')
                lines.append(f'  {error_var}->SetAttribute ("ErrorUnit", EnumValue ({unit_expr}));')
                # This ns-3 version exposes deterministic stream assignment as
                # AssignStreams rather than SetStream on RateErrorModel.
                lines.append(f"  {error_var}->AssignStreams ({stream_base + offset});")
                lines.append(
                    f'  dev_{lvar}.Get ({device_index})->SetAttribute '
                    f'("ReceiveErrorModel", PointerValue ({error_var}));'
                )
        lines.append(
            f'  AssignIpv4Exact (nodes["{a}"], dev_{lvar}.Get (0), "{ip_a}", "{mask}");'
        )
        lines.append(
            f'  AssignIpv4Exact (nodes["{b}"], dev_{lvar}.Get (1), "{ip_b}", "{mask}");'
        )

        if pcap_enabled and (pcap_links is None or lname in pcap_links):
            prefix_dir = pcap_dir or Path.cwd()
            prefix0 = prefix_dir / f"ns3_network-{lname}-0"
            prefix1 = prefix_dir / f"ns3_network-{lname}-1"
            lines.append(f"  p2p_{lvar}.EnablePcap ({cpp_string(prefix0)}, dev_{lvar}.Get (0), true);")
            lines.append(f"  p2p_{lvar}.EnablePcap ({cpp_string(prefix1)}, dev_{lvar}.Get (1), true);")

        if link_metrics:
            lines.append(
                f"  RegisterLinkDirection ({cpp_string(lname + ':a-to-b')}, {cpp_string(lname)}, "
                f'"a-to-b", {cpp_string(a)}, {cpp_string(b)}, {cpp_string(link["delay"])}, '
                f'{cpp_string(data_rate)}, {configured_error_rate_a_to_b:.17g}, {cpp_string(configured_error_unit)}, '
                f'dev_{lvar}.Get (0), dev_{lvar}.Get (1));'
            )
            lines.append(
                f"  RegisterLinkDirection ({cpp_string(lname + ':b-to-a')}, {cpp_string(lname)}, "
                f'"b-to-a", {cpp_string(b)}, {cpp_string(a)}, {cpp_string(link["delay"])}, '
                f'{cpp_string(data_rate)}, {configured_error_rate_b_to_a:.17g}, {cpp_string(configured_error_unit)}, '
                f'dev_{lvar}.Get (1), dev_{lvar}.Get (0));'
            )

        lines.append("")
    return "\n".join(lines)


def emit_lans(
    network_cfg: dict[str, Any],
    *,
    pcap_dir: Path | None = None,
    pcap_enabled: bool | None = None,
    pcap_links: set[str] | None = None,
):
    lines = []
    if pcap_enabled is None:
        pcap_enabled = bool(network_cfg.get("pcap", False))
    endpoint_map = build_endpoint_map(network_cfg)

    router_names = {x["name"] for x in network_cfg["nodes"].get("routers", [])}
    switch_names = {x["name"] for x in network_cfg["nodes"].get("switches", [])}
    endpoint_names = {x["name"] for x in network_cfg["nodes"].get("endpoints", [])}

    for lan in network_cfg.get("lans", []):
        lname = lan["name"]
        lvar = ident(lname)
        members = lan["members"]

        router_name = None
        switch_name = None
        endpoint_list = []

        for m in members:
            if m in router_names:
                if router_name is not None:
                    raise ValueError(f"LAN {lname} has multiple routers; exactly one is supported")
                router_name = m
            elif m in switch_names:
                if switch_name is not None:
                    raise ValueError(f"LAN {lname} has multiple switches; exactly one bridge switch is supported")
                switch_name = m
            elif m in endpoint_names:
                endpoint_list.append(m)

        if not router_name or not switch_name or not endpoint_list:
            raise ValueError(f"LAN {lname} must contain one router, one switch, and at least one endpoint")

        _, mask = cidr_to_network_and_mask(lan["subnet"])
        router_ip = ip_only(lan["interfaces"][router_name]["ip"])

        data_rate = lan["data_rate"]
        delay_expr = time_value_expr(lan["delay"])
        mtu = int(lan["mtu"])

        lines.append(f"  // LAN: {lname}")
        lines.append(f"  // endpoints={','.join(endpoint_list)}")
        lines.append(f"  CsmaHelper csma_{lvar};")
        lines.append(f'  csma_{lvar}.SetChannelAttribute ("DataRate", StringValue ("{data_rate}"));')
        lines.append(f'  csma_{lvar}.SetChannelAttribute ("Delay", {delay_expr});')
        lines.append(f'  csma_{lvar}.SetDeviceAttribute ("Mtu", UintegerValue ({mtu}));')

        lines.append(
            f'  NetDeviceContainer dev_{lvar}_rs = csma_{lvar}.Install (NodeContainer (nodes["{router_name}"], nodes["{switch_name}"]));'
        )

        for endpoint_name in endpoint_list:
            ep_var = ident(endpoint_name)
            lines.append(
                f'  NetDeviceContainer dev_{lvar}_{ep_var}_es = csma_{lvar}.Install (NodeContainer (nodes["{endpoint_name}"], nodes["{switch_name}"]));'
            )

        lines.append(f"  BridgeHelper bridge_{lvar};")
        lines.append(f"  NetDeviceContainer bridgePorts_{lvar};")
        lines.append(f"  bridgePorts_{lvar}.Add (dev_{lvar}_rs.Get (1));")
        for endpoint_name in endpoint_list:
            ep_var = ident(endpoint_name)
            lines.append(f"  bridgePorts_{lvar}.Add (dev_{lvar}_{ep_var}_es.Get (1));")
        lines.append(f'  bridge_{lvar}.Install (nodes["{switch_name}"], bridgePorts_{lvar});')

        lines.append(
            f'  AssignIpv4Exact (nodes["{router_name}"], dev_{lvar}_rs.Get (0), "{router_ip}", "{mask}");'
        )

        for endpoint_name in endpoint_list:
            if endpoint_name not in endpoint_map:
                raise ValueError(f"Endpoint {endpoint_name} has no endpoint mapping in network.nodes.endpoints")

            ep_cfg = endpoint_map[endpoint_name]
            tap_name = ep_cfg.get("tap_name")
            namespace = ep_cfg.get("namespace", "")
            tap_mode = tap_mode_to_ns3(ep_cfg.get("mode", "use_bridge"))
            ep_var = ident(endpoint_name)

            if not tap_name:
                raise ValueError(f"Endpoint {endpoint_name} missing 'tap' field")

            lines.append(f"  // endpoint={endpoint_name}, tap={tap_name}, namespace={namespace}, mode={tap_mode}")
            lines.append(f"  TapBridgeHelper tap_{lvar}_{ep_var};")
            lines.append(f'  tap_{lvar}_{ep_var}.SetAttribute ("Mode", StringValue ("{tap_mode}"));')
            lines.append(f'  tap_{lvar}_{ep_var}.SetAttribute ("DeviceName", StringValue ("{tap_name}"));')
            lines.append(f'  tap_{lvar}_{ep_var}.Install (nodes["{endpoint_name}"], dev_{lvar}_{ep_var}_es.Get (0));')

        if pcap_enabled and (pcap_links is None or lname in pcap_links):
            prefix_dir = pcap_dir or Path.cwd()
            router_prefix = prefix_dir / f"ns3_network-{lname}-router"
            switch_prefix = prefix_dir / f"ns3_network-{lname}-switch-r"
            lines.append(f"  csma_{lvar}.EnablePcap ({cpp_string(router_prefix)}, dev_{lvar}_rs.Get (0), true);")
            lines.append(f"  csma_{lvar}.EnablePcap ({cpp_string(switch_prefix)}, dev_{lvar}_rs.Get (1), true);")
            for endpoint_name in endpoint_list:
                ep_var = ident(endpoint_name)
                pcap_safe = ident(endpoint_name).lower()
                endpoint_prefix = prefix_dir / f"ns3_network-{lname}-{pcap_safe}-endpoint"
                endpoint_switch_prefix = prefix_dir / f"ns3_network-{lname}-{pcap_safe}-switch"
                lines.append(f"  csma_{lvar}.EnablePcap ({cpp_string(endpoint_prefix)}, dev_{lvar}_{ep_var}_es.Get (0), true);")
                lines.append(f"  csma_{lvar}.EnablePcap ({cpp_string(endpoint_switch_prefix)}, dev_{lvar}_{ep_var}_es.Get (1), true);")

        lines.append("")
    return "\n".join(lines)


def emit_routing_and_end(
    network_cfg: dict[str, Any],
    *,
    flow_monitor: bool,
    link_metrics: bool,
    flow_monitor_path: Path,
):
    lines = []
    if network_cfg.get("routing") == "global":
        lines.append("  Ipv4GlobalRoutingHelper::PopulateRoutingTables ();")
        lines.append("")

    if flow_monitor:
        lines.append("  // TapBridge traffic originates in external Linux IP stacks.  Standard")
        lines.append("  // FlowMonitor may therefore contain no flows; link-metrics.csv remains authoritative.")
        lines.append("  FlowMonitorHelper flowHelper;")
        lines.append("  Ptr<FlowMonitor> flowMonitor = flowHelper.InstallAll ();")
        lines.append("")

    if link_metrics:
        lines.append("  WriteLinkMetricsSnapshot ();")
        lines.append("  WriteQueueTimeseriesSnapshot ();")
    lines.append("  Simulator::Schedule (MilliSeconds (100), &PollStopFile);")

    lines.append('  NS_LOG_UNCOND ("ns3 network started.");')
    lines.append('  NS_LOG_UNCOND ("Topology loaded from generated config.");')
    lines.append("")
    lines.append("  Simulator::Stop (Seconds (3600));")
    lines.append("  Simulator::Run ();")
    if link_metrics:
        lines.append("  WriteLinkMetricsSnapshotOnce ();")
    if flow_monitor:
        lines.append("  flowMonitor->CheckForLostPackets ();")
        lines.append("  if (flowMonitor->GetFlowStats ().empty ())")
        lines.append("    {")
        lines.append('      NS_LOG_UNCOND ("[METRICS][WARN] FlowMonitor is empty; this is expected for external TapBridge traffic. Use link-metrics.csv and application RTT.");')
        lines.append("    }")
        lines.append(
            f"  flowMonitor->SerializeToXmlFile ({cpp_string(flow_monitor_path)}, true, true);"
        )
    lines.append("  Simulator::Destroy ();")
    lines.append("  return 0;")
    lines.append("}")
    return "\n".join(lines)


def generate_cc(config: dict[str, Any], output_dir: Path | str | None = None):
    if "network" not in config:
        raise ValueError("config file does not contain top-level 'network' section")

    network_cfg = config["network"]
    options = measurement_options(network_cfg)
    if output_dir is None:
        raw_output = config.get("output_path", "output")
        output_dir = Path(str(raw_output)).expanduser().resolve()
    else:
        output_dir = Path(output_dir).expanduser().resolve()
    metrics_dir = output_dir / "runtime" / "network"
    pcap_dir = metrics_dir / "pcap"
    experiment = config.get("experiment", {}) or {}
    if not isinstance(experiment, dict):
        experiment = {}
    raw_seed = experiment.get("random_seed", config.get("random_seed"))
    random_seed = int(raw_seed) if raw_seed not in (None, "") else None
    random_run = int(experiment.get("ns3_run", experiment.get("repetition", 1)) or 1)
    pcap_enabled = bool(network_cfg.get("pcap", False) or options["pcap"])

    parts = [
        emit_header(flow_monitor=options["flow_monitor"], link_metrics=options["link_metrics"]),
        emit_main_begin(
            network_cfg,
            metrics_dir=metrics_dir,
            pcap_dir=pcap_dir,
            link_metrics=options["link_metrics"],
            link_metrics_interval=options["interval"],
            queue_timeseries=options["queue_timeseries"],
            queue_timeseries_interval=options["queue_interval"],
            random_seed=random_seed,
            random_run=random_run,
        ),
        emit_nodes(network_cfg),
        emit_internet_stack(network_cfg),
        emit_backbone_links(
            network_cfg,
            link_metrics=options["link_metrics"],
            pcap_dir=pcap_dir,
            pcap_enabled=pcap_enabled,
            pcap_links=options["pcap_links"],
        ),
        emit_lans(
            network_cfg,
            pcap_dir=pcap_dir,
            pcap_enabled=pcap_enabled,
            pcap_links=options["pcap_links"],
        ),
        emit_routing_and_end(
            network_cfg,
            flow_monitor=options["flow_monitor"],
            link_metrics=options["link_metrics"],
            flow_monitor_path=metrics_dir / "flow-monitor.xml",
        ),
    ]
    return "\n".join(parts)


def main():
    if len(sys.argv) != 2:
        print("Usage: python src/ns3_generation.py config.yaml")
        sys.exit(1)

    config_path = Path(sys.argv[1]).resolve()
    config = load_yaml(str(config_path))
    output_dir = resolve_output_dir(config_path, config)
    output_dir.mkdir(parents=True, exist_ok=True)

    cc_code = generate_cc(config, output_dir)

    output_cc = output_dir / "ns3_network.cc"
    output_cc.write_text(cc_code, encoding="utf-8")

    print(f"[OK] Generated {output_cc}")

if __name__ == "__main__":
    main()
