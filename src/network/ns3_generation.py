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
import yaml
import ipaddress
from pathlib import Path


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


def cidr_to_network_and_mask(cidr: str):
    net = ipaddress.ip_network(cidr, strict=False)
    return str(net.network_address), str(net.netmask)


def ip_only(addr_with_prefix: str):
    return str(ipaddress.ip_interface(addr_with_prefix).ip)


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


def emit_header():
    return r'''#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/csma-module.h"
#include "ns3/tap-bridge-module.h"
#include "ns3/bridge-module.h"
#include "ns3/ipv4-global-routing-helper.h"

#include <iostream>
#include <map>
#include <string>

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
'''


def emit_main_begin(network_cfg):
    lines = []
    lines.append("int")
    lines.append("main (int argc, char *argv[])")
    lines.append("{")

    if network_cfg.get("scheduler") == "realtime":
        lines.append('  GlobalValue::Bind ("SimulatorImplementationType",')
        lines.append('                     StringValue ("ns3::RealtimeSimulatorImpl"));')

    lines.append('  GlobalValue::Bind ("ChecksumEnabled", BooleanValue (true));')
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


def emit_backbone_links(network_cfg):
    lines = []
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
        lines.append(
            f'  NetDeviceContainer dev_{lvar} = p2p_{lvar}.Install (nodes["{a}"], nodes["{b}"]);'
        )
        lines.append(
            f'  AssignIpv4Exact (nodes["{a}"], dev_{lvar}.Get (0), "{ip_a}", "{mask}");'
        )
        lines.append(
            f'  AssignIpv4Exact (nodes["{b}"], dev_{lvar}.Get (1), "{ip_b}", "{mask}");'
        )

        if pcap_enabled:
            lines.append(f'  p2p_{lvar}.EnablePcap ("ns3_network-{lname}-0", dev_{lvar}.Get (0), true);')
            lines.append(f'  p2p_{lvar}.EnablePcap ("ns3_network-{lname}-1", dev_{lvar}.Get (1), true);')

        lines.append("")
    return "\n".join(lines)


def emit_lans(network_cfg):
    lines = []
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

        if pcap_enabled:
            lines.append(f'  csma_{lvar}.EnablePcap ("ns3_network-{lname}-router", dev_{lvar}_rs.Get (0), true);')
            lines.append(f'  csma_{lvar}.EnablePcap ("ns3_network-{lname}-switch-r", dev_{lvar}_rs.Get (1), true);')
            for endpoint_name in endpoint_list:
                ep_var = ident(endpoint_name)
                pcap_safe = ident(endpoint_name).lower()
                lines.append(f'  csma_{lvar}.EnablePcap ("ns3_network-{lname}-{pcap_safe}-endpoint", dev_{lvar}_{ep_var}_es.Get (0), true);')
                lines.append(f'  csma_{lvar}.EnablePcap ("ns3_network-{lname}-{pcap_safe}-switch", dev_{lvar}_{ep_var}_es.Get (1), true);')

        lines.append("")
    return "\n".join(lines)


def emit_routing_and_end(network_cfg):
    lines = []
    if network_cfg.get("routing") == "global":
        lines.append("  Ipv4GlobalRoutingHelper::PopulateRoutingTables ();")
        lines.append("")

    lines.append('  NS_LOG_UNCOND ("ns3 network started.");')
    lines.append('  NS_LOG_UNCOND ("Topology loaded from generated config.");')
    lines.append("")
    lines.append("  Simulator::Stop (Seconds (3600));")
    lines.append("  Simulator::Run ();")
    lines.append("  Simulator::Destroy ();")
    lines.append("  return 0;")
    lines.append("}")
    return "\n".join(lines)


def generate_cc(config):
    if "network" not in config:
        raise ValueError("config file does not contain top-level 'network' section")

    network_cfg = config["network"]

    parts = [
        emit_header(),
        emit_main_begin(network_cfg),
        emit_nodes(network_cfg),
        emit_internet_stack(network_cfg),
        emit_backbone_links(network_cfg),
        emit_lans(network_cfg),
        emit_routing_and_end(network_cfg),
    ]
    return "\n".join(parts)


def main():
    if len(sys.argv) != 2:
        print("Usage: python src/ns3_generation.py config.yaml")
        sys.exit(1)

    config_path = Path(sys.argv[1]).resolve()
    config = load_yaml(str(config_path))
    cc_code = generate_cc(config)

    output_dir = resolve_output_dir(config_path, config)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_cc = output_dir / "ns3_network.cc"
    output_cc.write_text(cc_code, encoding="utf-8")

    print(f"[OK] Generated {output_cc}")

if __name__ == "__main__":
    main()