# FIR physical-design final fix

This package contains replacement files for the existing repository. Copy them into the repository root, preserving the folder paths, and commit the replacements.

## Included files

- `.github/workflows/fir-physical-design.yml`
- `fir-filter-cross-design/scripts/run_fir_flow.sh`
- `fir-filter-cross-design/scripts/flow_fir.tcl`
- `fir-filter-cross-design/scripts/extract_fir_features.py`

## What changed

1. The workflow no longer rewrites `fir_features.csv` after extraction. The extractor is the single owner of that CSV.
2. Clock-period tags use numeric formatting (`10.0` becomes `10`, while `20` stays `20`).
3. The runner exports `UTIL`, `PLACE_DENSITY`, `CLK_PERIOD`, `CLK_PERIOD_NS`, `RUN_TAG`, `RUN_DIR`, `NETLIST`, `NETLIST_JSON`, and `SDC` consistently.
4. The Tcl flow writes `fir_features_status.csv`, `flow_diagnostics.log`, a detailed-routing success marker, and final reports.
5. Fanout direction extraction uses Yosys JSON directions first, then the selected Liberty file for missing directions. Coverage is recorded for auditability.
6. Congestion parsing checks the congestion report first, then falls back to the routing/OpenROAD logs if the report does not contain a recognized overflow metric.
7. Effective placement density is estimated from placed cell areas in the final DEF and LEF macro dimensions divided by DEF placement-row area. It is not copied from the requested target density.
8. The extractor preserves nested measurement-status data in CSV as JSON text instead of silently dropping dictionary columns.
9. The GitHub summary shows the measurement-source and quality fields needed to diagnose unavailable metrics.

## Before committing

Run from the repository root:

```bash
bash -n fir-filter-cross-design/scripts/run_fir_flow.sh
python -m py_compile fir-filter-cross-design/scripts/extract_fir_features.py
```

The workflow file expects the repository layout shown in your current workflow: `environment.yml` at repository root and the project scripts under `fir-filter-cross-design/`.

## Re-extract an existing run without rerunning OpenROAD

From `fir-filter-cross-design/`, activate the same environment and make sure `config/sky130_example.env` points to the same Sky130HD Liberty and LEF files used by the run. Then run:

```bash
source config/sky130_example.env
RUN_TAG=fir_8tap_w8_util40_clk10
RUN_DIR="fir_project/runs/${RUN_TAG}"
python3 scripts/extract_fir_features.py \
  --netlist-json fir_project/synth/fir_filter_netlist.json \
  --run-dir "$RUN_DIR" \
  --outdir "$RUN_DIR" \
  --top fir_filter \
  --design-id fir_filter_8tap \
  --run-id "$RUN_TAG" \
  --taps 8 --data-width 8 --coeff-width 8 --acc-width 24 \
  --utilization 40 --place-density 0.40 --clock-period 10.0
```

This reuses the existing DEF/ODB, reports, logs, synthesized JSON, and configured LEF/Liberty. It does not launch OpenROAD. If the existing run lacks the congestion metric in both the report and logs, that metric will remain unavailable rather than being replaced with zero.
