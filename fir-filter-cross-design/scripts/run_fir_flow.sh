#!/usr/bin/env bash
set -euo pipefail

# Run from the project root. Edit config/sky130_example.env first.
source config/sky130_example.env
: "${LIBERTY:?Set LIBERTY in config/sky130_example.env}"
: "${TECH_LEF:?Set TECH_LEF in config/sky130_example.env}"
: "${CELL_LEF:?Set CELL_LEF in config/sky130_example.env}"
: "${UTIL:=40}"
: "${CLK_PERIOD:=10.0}"
: "${PLACE_DENSITY:=0.40}"
: "${RUN_TAG:=fir_8tap_w8_util40_clk10}"
export RUN_DIR="fir_project/runs/${RUN_TAG}"
export NETLIST="fir_project/synth/fir_filter_netlist.v"
export SDC="fir_project/synth/fir_filter.sdc"
mkdir -p "$RUN_DIR" fir_project/synth

for f in "$LIBERTY" "$TECH_LEF" "$CELL_LEF"; do
  if [[ ! -f "$f" ]]; then echo "ERROR: PDK file not found: $f" >&2; exit 2; fi
done

sed "s|__LIB_FILE__|${LIBERTY}|g" scripts/synth_fir.ys > fir_project/synth/synth_fir.generated.ys
yosys -l "$RUN_DIR/synthesis.log" -s fir_project/synth/synth_fir.generated.ys
test -s "$NETLIST"
test -s "fir_project/synth/fir_filter_netlist.json"

sed "s|__CLK_PERIOD__|${CLK_PERIOD}|g" constraints/fir_filter.sdc.in > "$SDC"
export SDC
openroad -exit scripts/flow_fir.tcl 2>&1 | tee "$RUN_DIR/openroad_log.txt"

python3 scripts/extract_fir_features.py \
  --netlist-json fir_project/synth/fir_filter_netlist.json \
  --run-dir "$RUN_DIR" \
  --outdir "$RUN_DIR" \
  --utilization "$UTIL" --clock-period "$CLK_PERIOD"

echo "FIR flow finished. Review $RUN_DIR/features.json and $RUN_DIR/features.csv."
