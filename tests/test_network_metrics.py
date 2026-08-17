from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from src.metrics.network_metrics import (
    analyze_network,
    parse_flow_monitor_xml,
    parse_link_metrics_csv,
    parse_data_rate_bps,
    parse_ns3_time,
)


FLOW_XML = """<?xml version="1.0" ?>
<FlowMonitor>
  <FlowStats>
    <Flow flowId="1" timeFirstTxPacket="+1000000000ns" timeFirstRxPacket="+1002000000ns"
          timeLastTxPacket="+1800000000ns" timeLastRxPacket="+2000000000ns"
          delaySum="+16000000ns" jitterSum="+7000000ns" maxDelay="+3000000ns"
          txBytes="1200" rxBytes="1000" txPackets="10" rxPackets="8"
          lostPackets="2" timesForwarded="16">
      <packetsDropped reasonCode="3" number="2" />
    </Flow>
  </FlowStats>
  <Ipv4FlowClassifier>
    <Flow flowId="1" sourceAddress="192.168.255.1" destinationAddress="192.168.1.1"
          protocol="6" sourcePort="40000" destinationPort="502" />
  </Ipv4FlowClassifier>
</FlowMonitor>
"""


class NetworkMetricsTests(unittest.TestCase):
    def test_time_and_flow_monitor_parser(self) -> None:
        self.assertAlmostEqual(parse_ns3_time("+2500us"), 0.0025)
        self.assertAlmostEqual(parse_data_rate_bps("10Mbps"), 10_000_000.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flow-monitor.xml"
            path.write_text(FLOW_XML, encoding="utf-8")
            rows = parse_flow_monitor_xml(path)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["direction"], "request")
        self.assertEqual(row["protocol"], "TCP")
        self.assertEqual(row["drop_packets"], 2)
        self.assertAlmostEqual(row["mean_delay_ms"], 2.0)
        self.assertAlmostEqual(row["mean_jitter_ms"], 1.0)
        self.assertAlmostEqual(row["throughput_bps"], 8000.0)
        self.assertAlmostEqual(row["packet_loss_rate"], 0.2)

    def test_link_snapshot_parser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "link-metrics.csv"
            path.write_text(
                "simulation_time_s,link,direction,source,target,configured_delay,configured_data_rate,"
                "configured_error_rate,configured_error_unit,tx_packets,rx_packets,tx_bytes,rx_bytes,"
                "drop_packets,queue_drop_packets,queue_packets_mean,queue_packets_max,"
                "queue_packets_current,queue_samples,delay_samples,mean_delay_ms,max_delay_ms,pending_packets\n"
                "2.0,r0-r1,a-to-b,r0,r1,20ms,10Mbps,0.01,packet,10,8,1200,1000,2,1,2.5,4,0,20,8,20.1,20.2,0\n",
                encoding="utf-8",
            )
            rows = parse_link_metrics_csv(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metric_source"], "link_trace")
        self.assertEqual(rows[0]["lost_packets"], 2)
        self.assertAlmostEqual(rows[0]["packet_loss_rate"], 0.2)
        self.assertAlmostEqual(rows[0]["throughput_bps"], 4000.0)
        self.assertAlmostEqual(rows[0]["delay_error_ms"], 0.1)
        self.assertAlmostEqual(rows[0]["delay_error_percent"], 0.5)
        self.assertAlmostEqual(rows[0]["loss_error"], 0.19)
        self.assertAlmostEqual(rows[0]["throughput_utilization"], 0.0004)
        self.assertEqual(rows[0]["queue_drop_packets"], 1)
        self.assertEqual(rows[0]["error_model_drop_packets"], 1)
        self.assertEqual(rows[0]["other_classified_losses"], 0)
        self.assertTrue(rows[0]["network_conservation_ok"])
        self.assertAlmostEqual(rows[0]["queue_packets_mean"], 2.5)
        self.assertEqual(rows[0]["queue_packets_max"], 4)

    def test_empty_flow_stats_is_reported_not_fabricated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flow = root / "flow-monitor.xml"
            flow.write_text(
                "<FlowMonitor><FlowStats/><Ipv4FlowClassifier/></FlowMonitor>",
                encoding="utf-8",
            )
            output = root / "network.csv"
            aggregate = root / "network-aggregate.json"
            summary = analyze_network(
                flow_monitor_xml=flow,
                link_metrics_csv=None,
                output_csv=output,
                aggregate_json=aggregate,
            )

            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            saved = json.loads(aggregate.read_text(encoding="utf-8"))

        self.assertEqual(rows, [])
        self.assertEqual(summary["status"], "no_data")
        self.assertEqual(saved["row_count"], 0)
        self.assertTrue(any("TapBridge" in warning for warning in saved["warnings"]))


if __name__ == "__main__":
    unittest.main()
