
#!/usr/bin/env bash
set -Eeuo pipefail

# Run from fir-filter-cross-design project root.
# Configure config/sky130_example.env before running.

# ------------------------------------------------------------
# 1. Load PDK configuration
# ------------------------------------------------------------
source config/sky130_example.env

: "${LIBERTY:?Set LIBERTY in config/sky130_example.env}"
: "${TECH_LEF:?Set TECH_LEF in config/sky130_example.env}"
: "${CELL_LEF:?Set CELL_LEF in config/sky130_example.env}"

# ------------------------------------------------------------
# 2. Set and export run configuration
# ------------------------------------------------------------
export UTIL="${UTIL:-40}"
export CLK_PERIOD="${CLK_PERIOD:-10.0}"
export PLACE_DENSITY="${PLACE_DENSITY:-0.40}"
export RUN_TAG="${RUN_TAG:-fir_8tap_w8_util40_clk10}"

# Tcl flow may use either CLK_PERIOD_NS or CLK_PERIOD.
export CLK_PERIOD_NS="$CLK_PERIOD"

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
echo "CLK_PERIOD_NS = $CLK_PERIOD_NS"
echo "PLACE_DENSITY = $PLACE_DENSITY"
echo "RUN_TAG       = $RUN_TAG"
echo "RUN_DIR       = $RUN_DIR"

# ------------------------------------------------------------
# 3. Validate input files and tools
# ------------------------------------------------------------
for f in \
    "$LIBERTY" \
    "$TECH_LEF" \
    "$CELL_LEF" \
    scripts/synth_fir.ys \
    scripts/flow_fir.tcl \
    scripts/extract_fir_features.py \
    scripts/extract_fir_features_readable.py \
    constraints/fir_filter.sdc.in
do
    if [[ ! -f "$f" ]]; then
        echo "ERROR: Required input file not found: $f" >&2
        exit 2
    fi
done

for tool in yosys openroad python3 sed; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: Required tool is not installed or not in PATH: $tool" >&2
        exit 2
    fi
done

# ------------------------------------------------------------
# 4. Generate Yosys synthesis script
# ------------------------------------------------------------
echo "===== Generate synthesis script ====="

sed "s|__LIB_FILE__|${LIBERTY}|g" \
    scripts/synth_fir.ys \
    > fir_project/synth/synth_fir.generated.ys

if [[ ! -s fir_project/synth/synth_fir.generated.ys ]]; then
    echo "ERROR: Generated Yosys script is empty." >&2
    exit 3
fi

# ------------------------------------------------------------
# 5. Run synthesis
# ------------------------------------------------------------
echo "===== Run Yosys synthesis ====="

yosys -l "$RUN_DIR/synthesis.log" \
    -s fir_project/synth/synth_fir.generated.ys

# Check synthesis outputs before modifying the Verilog.
for f in "$NETLIST" "$NETLIST_JSON"; do
    if [[ ! -s "$f" ]]; then
        echo "ERROR: Expected synthesis output is missing or empty: $f" >&2
        echo "Check $RUN_DIR/synthesis.log" >&2
        exit 3
    fi
done

# ------------------------------------------------------------
# 6. Prepare and inspect synthesized Verilog
# ------------------------------------------------------------
echo "===== Remove signed keywords for OpenROAD compatibility ====="

sed -i 's/signed //g' "$NETLIST"

echo "===== Synthesized netlist: first 40 lines ====="
nl -ba "$NETLIST" | sed -n '1,40p'

echo "===== Netlist file size ====="
wc -l "$NETLIST"

# ------------------------------------------------------------
# 7. Generate timing constraints
# ------------------------------------------------------------
echo "===== Generate timing constraints ====="

sed "s|__CLK_PERIOD__|${CLK_PERIOD}|g" \
    constraints/fir_filter.sdc.in \
    > "$SDC"

if [[ ! -s "$SDC" ]]; then
    echo "ERROR: Generated SDC file is missing or empty." >&2
    exit 3
fi

echo "===== Generated SDC ====="
cat "$SDC"

# ------------------------------------------------------------
# 8. Run OpenROAD physical-design flow
# ------------------------------------------------------------
echo "===== Run OpenROAD ====="

if ! openroad -exit scripts/flow_fir.tcl \
    2>&1 | tee "$RUN_DIR/openroad_log.txt"; then
    echo "ERROR: OpenROAD failed." >&2
    echo "Inspect: $RUN_DIR/openroad_log.txt" >&2
    echo "Inspect: $RUN_DIR/flow_diagnostics.log" >&2
    exit 4
fi

echo "===== OpenROAD process completed ====="

# ------------------------------------------------------------
# 9. Extract features and physical-design targets
# ------------------------------------------------------------
echo "===== Extract physical-design features ====="

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

echo "===== Feature extraction completed ====="

# ------------------------------------------------------------
# 10. Validate expected output files
# ------------------------------------------------------------
echo "===== Check generated reports and CSV files ====="

expected_files=(
    "$RUN_DIR/run_report.md"
    "$RUN_DIR/features.csv"
    "$RUN_DIR/physical_targets.csv"
    "$RUN_DIR/preplacement_features.csv"
    "$RUN_DIR/fir_features.csv"
    "$RUN_DIR/fir_features_status.csv"
    "$RUN_DIR/openroad_log.txt"
)

missing_count=0

for f in "${expected_files[@]}"; do
    if [[ -s "$f" ]]; then
        echo "FOUND: $f"
    else
        echo "WARNING: Missing or empty: $f"
        missing_count=$((missing_count + 1))
    fi
done

# ------------------------------------------------------------
# 11. Report flow status
# ------------------------------------------------------------
echo
echo "===== Final run summary ====="
echo "Run ID: $RUN_TAG"
echo "Run directory: $RUN_DIR"

if [[ -s "$RUN_DIR/fir_features.csv" ]]; then
    echo "----- fir_features.csv -----"
    cat "$RUN_DIR/fir_features.csv"
fi

if [[ -s "$RUN_DIR/fir_features_status.csv" ]]; then
    echo "----- fir_features_status.csv (last 10 lines) -----"
    tail -n 10 "$RUN_DIR/fir_features_status.csv"
fi

if (( missing_count > 0 )); then
    echo "WARNING: $missing_count expected output file(s) are missing or empty."
    echo "Inspect the logs and extraction report before treating this run as complete."
else
    echo "All expected output files are present and non-empty."
fi

echo "FIR flow finished."
echo "Review outputs under: $RUN_DIR"
