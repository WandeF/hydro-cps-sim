# Model sources

- M. E. Baran and F. F. Wu, “Network reconfiguration in distribution systems for loss reduction and load balancing,” *IEEE Transactions on Power Delivery* 4(2), 1401–1407 (1989). [DOI: 10.1109/61.25627](https://doi.org/10.1109/61.25627).
- [MATPOWER case33bw source](https://matpower.org/docs/ref/matpower6.0/case33bw.html): original bus numbering, powers, impedances and branch states. Load values in the source are converted from kW/kVAr to MW/MVAr; line impedances are converted using the voltage and power bases.
- [pandapower case33bw](https://pandapower.readthedocs.io/en/v3.2.1/networks/power_system_test_cases.html#case-33-bw): the installed `pandapower.networks.case33bw()` supplies all model data. The experiments use pandapower 3.2.1, rather than a manually transcribed network.
- [Published comparison of the 33-bus baseline](https://pmc.ncbi.nlm.nih.gov/articles/PMC11001774/): commonly reported minimum voltage 0.9131 pu and active loss 202.66 kW. The acceptance window is 0.00005 pu and 0.05 kW; computed values are retained without adjustment.
- N. Husnoo et al., [False Data Injection Threats in Active Distribution Systems: A Comprehensive Survey](https://arxiv.org/abs/2111.14251). Background for measurement attacks; it is not the source of this example's PLC logic or network parameters.

## Standard model and experimental control

The public model supplies the 12.66 kV network, 33 buses, 37 lines, 32 loads, source voltage and original branch states. Five tie lines start out of service. No physical parameters are overridden when importing the model.

Hydro-CPS-Sim adds three software PLCs, SCADA forwarding, a BOOL input on the original Bus 24 load's `in_service` field, an experimental hysteresis rule, a communication topology and a Modbus MITM scenario. These are derived CPS experiments, not protection settings or cyber experiments from the Baran–Wu paper. The 0.420 MW / 0.200 MVAr load already exists in the source model.

The solver performs balanced, positive-sequence AC quasi-steady power flow at successive requested times. A one-second sample interval is a sampling clock, not an internal transient integration step. No frequency, electromagnetic, unbalanced three-phase or millisecond relay dynamics are modeled.

## Numbering and provenance

In pandapower 3.2.1 the imported bus **names** are integer labels `0` through `32` (their canonical text form is recorded in the mapping). The loader validates this complete domain and checks the source anchor, then maps source names to paper Bus 1–33. It never derives paper numbers from DataFrame indices. Unsupported naming conventions fail explicitly. `BUS_01` through `BUS_33` remain stable aliases if DataFrame indices change; controlled loads are resolved by their bus relationship, with exactly one matching load required.

`native/benchmark_reference.json` preserves every column and row of the source bus, line, load, ext-grid, switch, generator, static generator, transformer, shunt and impedance tables, plus system frequency and MVA base. Its canonical JSON hash covers those complete input tables. A second SHA-256 covers the entire installed `case33bw.json` source file. No alias assignment changes source tables. `bus_id_map.json` gives paper IDs, imported names and actual DataFrame indices.

The source uses `max_i_ka=99999` as a placeholder. Line loading percent is therefore omitted from this experiment's acceptance criteria; exported line power, current and losses use the solved values.
