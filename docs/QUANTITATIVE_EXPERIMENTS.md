# Quantitative experiment pipeline

The `metric` branch adds a configuration-driven measurement and analysis layer
without changing the closed-loop control semantics. It targets the four
research questions in `NEED.md`: correctness, network fidelity, cross-layer
attack propagation, and performance/scalability.

## Runtime outputs

When `metrics.enabled: true`, one run writes the following additional files:

```text
output/runtime/
├── manifest.json
├── config_resolved.yaml
├── csv/
│   ├── events.csv
│   ├── communication.csv
│   ├── resources.csv
│   └── network.csv
├── raw/
│   └── metric_writer_stats/
└── network/
    ├── flow-monitor.xml
    ├── link-metrics.csv
    ├── network-aggregate.json
    └── pcap/
```

`events.csv` uses a shared nanosecond timeline across coordinator, SCADA, PLC
adapters, and attack processes. `communication.csv` contains one row per real
Modbus request with a request ID, latency, and status. Warm-up timeouts are
labelled separately and excluded from reported timeout rates. `resources.csv`
samples de-duplicated coordinator, SCADA, adapter, OpenPLC, ns-3, and attack
PIDs; the set is resolved on every sample, including controlled attack-launcher
descendants during OpenPLC compilation and restart. Asynchronous Modbus and
MITM writers use bounded, non-blocking queues. Their accepted, written, dropped,
failed, and unflushed counts are recorded under `raw/metric_writer_stats/` so a
telemetry bottleneck cannot silently invalidate an experiment. Shutdown checks
these snapshots after SCADA and attack processes exit. Any queue drop, sink
error, unflushed/pending row, live writer thread, malformed snapshot, or missing
expected writer changes the lifecycle result to
`simulation_end,status=cleanup_error`. The snapshots are also copied to
`reports/metric_writer_stats/`.

FlowMonitor is retained as an auxiliary ns-3 source. Linux namespace traffic
enters ns-3 through TapBridge and may not receive a FlowProbe tag, so an empty
FlowMonitor file is not treated as zero traffic. The generated topology also
measures each backbone direction at the net-device layer; `link-metrics.csv`
and Modbus RTT are the authoritative sources for real platform traffic.
`network.csv` and `network-aggregate.json` also calculate configured-versus-
measured delay error, packet-loss error (for packet RateErrorModel links), and
throughput utilization. PCAP files remain only under `runtime/network/pcap` to
avoid doubling large traces during report export.

## 1. Closed-loop correctness

Run the platform baseline, then compare it with a reference output:

```bash
bash scripts/run_all.sh examples/c_town/config.yaml

python3 scripts/analyze_experiment.py correctness \
  --baseline examples/c_town/baseline \
  --platform examples/c_town/output \
  --variables T1 T2 T3 T4 T5 T6 T7
```

The analysis excludes the DHALSIM dummy row 0 by default and aligns both runs
by iteration. It produces per-variable RMSE, MAE, and maximum absolute error;
per-actuator mismatch and switch timing metrics; and overall summary CSV/JSON.

## 2. Network-delay validation matrix

Generate isolated configurations. Every repetition gets a unique `output_path`,
so runs cannot overwrite one another:

```bash
python3 scripts/generate_experiment_matrix.py \
  --base-config examples/c_town/config.yaml \
  --link r0-r_scada \
  --link r0-r4 \
  --delays-ms 0 2 5 10 20 50 100 \
  --repetitions 5

python3 scripts/run_experiment_matrix.py \
  --config-dir experiments/network_validation/generated \
  --resume --stop-on-error
```

`--resume` skips only runs whose unified timeline contains
`simulation_end,status=success`; a manifest left by a failed run is rerun.

The two links in this example are the backbone portion of the SCADA–PLC4 path.
Change the target edge link for another PLC. Only one independent parameter
should be varied in a validation group.

`run_all.sh` gracefully stops ns-3 after the closed loop, flushes FlowMonitor
and link snapshots, and invokes `scripts/analyze_network.py` automatically. It
can also be run manually:

```bash
python3 scripts/analyze_network.py --config path/to/generated_config.yaml
```

## 3. Attack propagation and recovery

After running an attack configuration, compare it with its matching baseline:

```bash
python3 scripts/analyze_experiment.py propagation \
  --baseline examples/c_town/baseline \
  --attack examples/c_town/output \
  --variables T1 T2 T3 T4 T5 T6 T7 \
  --epsilon 0.01 \
  --hydraulic-step-sec 300 \
  --recovery-k 3
```

The output reports attack entry (`tA`), first communication anomaly (`tC`),
first control deviation (`tU`), first physical deviation (`tP`), propagation
delays in both execution and simulated hydraulic time, peak deviation, RMSE,
AUC, and recovery. A run that does not recover within the observation window is
reported as `not_recovered`; the simulation end is never substituted as a
recovery time.

## 4. Performance and batch statistics

Analyze execution time, resource usage, and communication overhead:

```bash
python3 scripts/analyze_performance.py examples/c_town/output
```

The one-click `run_all.sh` pipeline invokes this analyzer automatically after
a successful run and writes `reports/metrics/performance_summary.{json,csv}`.
The summary includes aggregate writer accepted/written/drop/error/unflushed
counts plus top-level `run_status`, `complete`, and `quality_complete` fields.

Collect per-run summaries and optionally calculate mean and sample standard
deviation across repetitions:

```bash
python3 scripts/analyze_batch.py results \
  --output results/all_summary_metrics.csv \
  --aggregate-by metric_type scenario
```

The experiment manifest records the exact config hash, Git commit and dirty
state, host/software metadata, random seed, hydraulic step, and iteration count.
When a result root contains both runtime and exported copies of the network
aggregate, the batch analyzer counts only the canonical runtime copy.
Summaries explicitly marked with a failed/incomplete lifecycle are excluded by
default; use `--include-incomplete` only for failure-diagnostics tables.
Generated configurations and `results/` are ignored by Git; keep the final
paper data in archival storage or a dedicated data release.
