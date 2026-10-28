
#!/usr/bin/env bash
set -Eeuo pipefail

# Run from fir-filter-cross-design project root.
# Configure config/sky130_example.env before running.

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
export NETLIST_JSON="fir_project/synth/fir_filter_netlist.json"

mkdir -p "$RUN_DIR" fir_project/synth

echo "===== Configuration ====="
echo "LIBERTY       = $LIBERTY"
echo "TECH_LEF      = $TECH_LEF"
echo "CELL_LEF      = $CELL_LEF"
echo "UTIL          = $UTIL"
echo "CLK_PERIOD    = $CLK_PERIOD"
echo "PLACE_DENSITY = $PLACE_DENSITY"
echo "RUN_DIR       = $RUN_DIR"

for f in "$LIBERTY" "$TECH_LEF" "$CELL_LEF"; do
    if [[ ! -f "$f" ]]; then
        echo "ERROR: PDK file not found: $f" >&2
        exit 2
    fi
done

echo "===== Generate synthesis script ====="
sed "s|__LIB_FILE__|${LIBERTY}|g" \
    scripts/synth_fir.ys \
    > fir_project/synth/synth_fir.generated.ys

echo "===== Run Yosys synthesis ====="
yosys -l "$RUN_DIR/synthesis.log" \
    -s fir_project/synth/synth_fir.generated.ys

echo "===== Remove signed keywords for OpenROAD compatibility ====="
sed -i 's/signed //g' "$NETLIST"
    
echo "===== Generated netlist: lines 1-25 ====="
nl -ba fir_project/synth/fir_filter_netlist.v | sed -n '1,25p'

echo "===== Netlist file size ====="
wc -l fir_project/synth/fir_filter_netlist.v

echo "===== Generated netlist: lines 1-40 ====="
nl -ba fir_project/synth/fir_filter_netlist.v | sed -n '1,40p'

for f in "$NETLIST" "$NETLIST_JSON"; do
    if [[ ! -s "$f" ]]; then
        echo "ERROR: Expected synthesis output is missing or empty: $f" >&2
        echo "Check $RUN_DIR/synthesis.log" >&2
        exit 3
    fi
done

echo "===== Inspect synthesized Verilog ====="
nl -ba "$NETLIST" | sed -n '1,40p'

echo "===== Generate timing constraints ====="
sed "s|__CLK_PERIOD__|${CLK_PERIOD}|g" \
    constraints/fir_filter.sdc.in > "$SDC"

echo "===== Run OpenROAD ====="
if ! openroad -exit scripts/flow_fir.tcl \
    2>&1 | tee "$RUN_DIR/openroad_log.txt"; then
    echo "ERROR: OpenROAD failed." >&2
    echo "Inspect $RUN_DIR/openroad_log.txt" >&2
    echo "Inspect the numbered netlist above, especially the reported line." >&2
    exit 4
fi


echo "===== Extract physical-design features and generate report ====="

python3 scripts/extract_fir_features.py \
    --netlist-json "$NETLIST_JSON" \
    --run-dir "$RUN_DIR" \
    --outdir "$RUN_DIR" \
    --design-id "fir_filter_8tap" \
    --run-id "$RUN_TAG" \
    --taps 8 \
    --data-width 8 \
    --coeff-width 8 \
    --acc-width 24 \
    --utilization "$UTIL" \
    --place-density "$PLACE_DENSITY" \
    --clock-period "$CLK_PERIOD"

echo "===== Generated reports ====="
for f in \
    "$RUN_DIR/run_report.md" \
    "$RUN_DIR/features.csv" \
    "$RUN_DIR/physical_targets.csv" \
    "$RUN_DIR/fir_features.csv"
do
    if [[ -s "$f" ]]; then
        echo "FOUND: $f"
    else
        echo "WARNING: Missing or empty: $f"
    fi
done

echo "FIR flow finished."
echo "Review outputs under: $RUN_DIR"

