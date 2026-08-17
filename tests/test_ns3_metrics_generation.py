from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.network.ns3_generation import generate_cc


def _config(output_dir: Path, *, enabled: bool = True) -> dict:
    return {
        "output_path": str(output_dir),
        "experiment": {"random_seed": 1003, "repetition": 3},
        "network": {
            "scheduler": "realtime",
            "routing": "global",
            "pcap": True,
            "measurement": {
                "enabled": enabled,
                "flow_monitor": True,
                "link_metrics": True,
                "link_metrics_interval": "250ms",
            },
            "nodes": {
                "routers": [{"name": "r0"}, {"name": "r1"}],
                "switches": [],
                "endpoints": [],
            },
            "backbone_links": [
                {
                    "name": "r0-r1",
                    "endpoints": ["r0", "r1"],
                    "data_rate": "10Mbps",
                    "delay": "20ms",
                    "mtu": 1500,
                    "subnet": "10.0.1.0/24",
                    "interfaces": {
                        "r0": {"ip": "10.0.1.1/24"},
                        "r1": {"ip": "10.0.1.2/24"},
                    },
                    "queue": {"type": "DropTailQueue", "max_packets": 50},
                    "error_model": {"type": "rate", "unit": "packet", "error_rate": 0.01},
                }
            ],
            "lans": [],
        },
    }


class Ns3MetricsGenerationTests(unittest.TestCase):
    def test_enabled_measurement_emits_flow_and_device_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp).resolve()
            source = generate_cc(_config(output_dir), output_dir)

            self.assertIn('#include "ns3/flow-monitor-module.h"', source)
            self.assertIn("FlowMonitorHelper flowHelper", source)
            self.assertIn("flowMonitor->CheckForLostPackets", source)
            self.assertIn(str(output_dir / "runtime/network/flow-monitor.xml"), source)
            self.assertIn(str(output_dir / "runtime/network/link-metrics.csv"), source)
            self.assertIn("RegisterLinkDirection", source)
            self.assertIn("LinkErrorDrop", source)
            self.assertIn("PhyRxDrop", source)
            self.assertIn("QueueEnqueue", source)
            self.assertIn("QueueDequeue", source)
            self.assertIn("QueueDrop", source)
            self.assertIn("error_model_drop_packets,queue_enqueue_packets", source)
            self.assertIn("queue_occupancy_ratio_mean,queue_occupancy_ratio_max", source)
            self.assertIn("g_linkMetricsInterval = Seconds (0.250000000)", source)
            self.assertIn('SetQueue ("ns3::DropTailQueue<Packet>"', source)
            self.assertIn('QueueSize ("50p")', source)
            self.assertIn("CreateObject<RateErrorModel>", source)
            self.assertIn("RateErrorModel::ERROR_UNIT_PACKET", source)
            self.assertIn("configured_error_rate,configured_error_unit", source)
            self.assertIn('0.01, "packet"', source)
            self.assertIn(str(output_dir / "runtime/network/pcap/ns3_network-r0-r1-0"), source)
            self.assertIn(str(output_dir / "runtime/network/ns3.stop"), source)
            self.assertIn("FlowMonitor may therefore contain no flows", source)
            self.assertIn("RngSeedManager::SetSeed (1003)", source)
            self.assertIn("RngSeedManager::SetRun (3)", source)

    def test_directional_error_stream_queue_timeseries_and_pcap_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp).resolve()
            config = _config(output_dir)
            config["network"]["measurement"].update({
                "pcap_links": [],
                "queue_timeseries": {"enabled": True, "interval": "20ms"},
            })
            error = config["network"]["backbone_links"][0]["error_model"]
            error.update({"direction": "a-to-b", "stream": 17})
            source = generate_cc(config, output_dir)

            self.assertIn("g_queueTimeseriesEnabled = true", source)
            self.assertIn("g_queueTimeseriesInterval = Seconds (0.020000000)", source)
            self.assertIn(str(output_dir / "runtime/network/queue-timeseries.csv"), source)
            self.assertIn("AssignStreams (17)", source)
            self.assertEqual(source.count("CreateObject<RateErrorModel>"), 1)
            self.assertNotIn("EnablePcap", source)

    def test_master_switch_disables_measurement_features(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp).resolve()
            source = generate_cc(_config(output_dir, enabled=False), output_dir)

            self.assertNotIn("flow-monitor-module.h", source)
            self.assertNotIn("FlowMonitorHelper", source)
            self.assertNotIn("RegisterLinkDirection", source)
            self.assertNotIn("link-metrics.csv", source)
            # Graceful stop polling is lifecycle infrastructure and stays active.
            self.assertIn("PollStopFile", source)


if __name__ == "__main__":
    unittest.main()
