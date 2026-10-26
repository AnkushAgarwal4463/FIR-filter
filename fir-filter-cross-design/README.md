# FIR Filter Cross-Design Physical-Design Test

Independent FIR project intended as a **second design family** for testing a placement/timing/congestion model trained on the existing systolic-array data. It does not modify or replace the systolic-array project.

## Included

- `rtl/fir_filter.v`: 8-tap signed FIR, 8-bit samples/coefficients, 24-bit accumulator.
- `tb/tb_fir_filter.v`: basic functional smoke test.
- `scripts/synth_fir.ys`: Yosys synthesis template, including Sky130 Liberty mapping and netlist/JSON output.
- `scripts/flow_fir.tcl`: separate OpenROAD floorplan, placement, timing repair, global/detailed routing, ODB/DEF and report generation.
- `scripts/run_fir_flow.sh`: end-to-end synthesis, SDC generation, OpenROAD and extraction runner.
- `scripts/extract_fir_features.py`: separates pre-placement netlist predictors from post-placement/routing targets; unavailable measurements remain null.
- `constraints/fir_filter.sdc.in`: timing-constraint template.
- `config/sky130_example.env`: paths/settings to edit for your runner.
- GitHub Actions workflow for RTL simulation only; it does not require or run a PDK/OpenROAD flow.

## 1. Run functional simulation

Install Icarus Verilog, then from the project root:

```bash
make sim
```

## 2. Configure Sky130A paths

Edit `config/sky130_example.env`. Verify all three files exist:

- Sky130A standard-cell Liberty, typically `sky130_fd_sc_hd__tt_025C_5v00.lib`
- Sky130A technology LEF (`sky130_fd_sc_hd.tlef` or the correct tech LEF for your install)
- Sky130 HD standard-cell LEF (`sky130_fd_sc_hd.lef`)

The example paths are illustrative. Different PDK distributions place files differently. The `TECH_LEF` path in particular must point to a real technology LEF, not a cell LEF.

## 3. Run synthesis + OpenROAD

Use the same PDK version, Liberty corner, floorplan settings, placement density, clock period, timing margins and routing setup as the systolic-array experiment wherever possible. From the project root:

```bash
./scripts/run_fir_flow.sh
```

The default run is `fir_project/runs/fir_8tap_w8_util40_clk10/`. Change `UTIL`, `CLK_PERIOD`, `PLACE_DENSITY`, and `RUN_TAG` in the environment config to run additional cases. Check that the clock period and constraint semantics match the systolic training data before comparing results.

Outputs include `final.def`, `final.odb`, setup/hold timing reports when supported by your OpenROAD build, routing congestion report, logs, and feature files. Commands and report names can vary between OpenROAD builds; inspect `openroad_log.txt` and do not treat a missing report as a valid zero measurement.

## 4. Feature extraction and ML-safe use

`preplacement_features.csv/json` contains features intended to be available before placement (synthesized netlist structure and known constraints). `physical_targets.csv/json` contains results after placement/routing, such as slack and congestion. Use the former as model inputs and the latter as targets for those same quantities. Do not use post-placement timing, congestion or wirelength as inputs when predicting those metrics.

The extractor is deliberately separate from your existing systolic feature extractor. Its definitions are not automatically identical to your existing 20-column dataset. Before inference or combined training, create a documented schema mapping and verify units/definitions. Do not insert fabricated zeros for unavailable fields. The lightweight script does not claim to measure final routed wirelength; `wirelength_um` remains null unless you add and validate a matching geometry parser or an OpenROAD wirelength report.

## Important limitations

- The coefficient function currently defines exactly eight coefficient values. Keep `TAPS=8` unless you update the RTL and testbench accordingly.
- This is a starter physical-design flow, not an assertion that every OpenROAD version will accept every command/report exactly as written. Validate against the OpenROAD/PDK build used by the systolic project.
- A single FIR run is only a pipeline smoke test, not proof of cross-design generalization. Evaluate multiple FIR configurations and keep the trained systolic model frozen during the FIR test.
