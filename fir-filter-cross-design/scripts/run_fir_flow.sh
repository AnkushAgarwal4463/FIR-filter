#!/usr/bin/env bash
set -Eeuo pipefail

# Run from fir-filter-cross-design project root.
# Configure config/sky130_example.env before running.

# Preserve whether density was explicitly supplied before the config file
# adds its default. If not supplied, derive density from UTIL.
PLACE_DENSITY_OVERRIDE="${PLACE_DENSITY:-}"
RUN_TAG_OVERRIDE="${RUN_TAG:-}"
source config/sky130_example.env

: "${LIBERTY:?Set LIBERTY in config/sky130_example.env}"
: "${TECH_LEF:?Set TECH_LEF in config/sky130_example.env}"
: "${CELL_LEF:?Set CELL_LEF in config/sky130_example.env}"

# Export every variable consumed by Tcl/OpenROAD and the Python extractor.
export UTIL="${UTIL:-40}"
export CLK_PERIOD="${CLK_PERIOD:-10.0}"
export CLK_PERIOD_NS="$CLK_PERIOD"
if [[ -n "$PLACE_DENSITY_OVERRIDE" ]]; then
  export PLACE_DENSITY="$PLACE_DENSITY_OVERRIDE"
else
  export PLACE_DENSITY="$(awk -v u="$UTIL" 'BEGIN { printf "%.2f", u / 100 }')"
fi
CLK_TAG="$(awk -v c="$CLK_PERIOD" 'BEGIN { printf "%g", c + 0 }')"
if [[ -n "$RUN_TAG_OVERRIDE" ]]; then
  export RUN_TAG="$RUN_TAG_OVERRIDE"
else
  export RUN_TAG="fir_8tap_w8_util${UTIL}_clk${CLK_TAG}"
fi

export RUN_DIR="fir_project/runs/${RUN_TAG}"
export NETLIST="fir_project/synth/fir_filter_netlist.v"
export NETLIST_JSON="fir_project/synth/fir_filter_netlist.json"
export SDC="fir_project/synth/fir_filter.sdc"

mkdir -p "$RUN_DIR" fir_project/synth

printf '%s\n' '===== FIR Configuration ====='
printf 'LIBERTY       = %s\n' "$LIBERTY"
printf 'TECH_LEF      = %s\n' "$TECH_LEF"
printf 'CELL_LEF      = %s\n' "$CELL_LEF"
printf 'UTIL          = %s\n' "$UTIL"
printf 'PLACE_DENSITY = %s\n' "$PLACE_DENSITY"
printf 'CLK_PERIOD    = %s\n' "$CLK_PERIOD"
printf 'CLK_PERIOD_NS = %s\n' "$CLK_PERIOD_NS"
printf 'RUN_TAG       = %s\n' "$RUN_TAG"
printf 'RUN_DIR       = %s\n' "$RUN_DIR"

for f in \
  "$LIBERTY" "$TECH_LEF" "$CELL_LEF" \
  scripts/synth_fir.ys scripts/flow_fir.tcl \
  scripts/extract_fir_features.py constraints/fir_filter.sdc.in
 do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: Required input file not found: $f" >&2
    exit 2
  fi
done

for tool in yosys openroad python3; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: Required tool not found in PATH: $tool" >&2
    exit 2
  fi
done

# ------------------------------------------------------------
# Synthesis
# ------------------------------------------------------------
echo "===== Generate Yosys synthesis script ====="
sed "s|__LIB_FILE__|${LIBERTY}|g" \
  scripts/synth_fir.ys > fir_project/synth/synth_fir.generated.ys

test -s fir_project/synth/synth_fir.generated.ys

echo "===== Run Yosys synthesis ====="
yosys -l "$RUN_DIR/synthesis.log" \
  -s fir_project/synth/synth_fir.generated.ys

for f in "$NETLIST" "$NETLIST_JSON"; do
  if [[ ! -s "$f" ]]; then
    echo "ERROR: Expected synthesis output missing or empty: $f" >&2
    echo "Inspect $RUN_DIR/synthesis.log" >&2
    exit 3
  fi
done

# Retained for compatibility with the current OpenROAD netlist flow.
echo "===== Prepare synthesized Verilog ====="
sed -i 's/signed //g' "$NETLIST"

# ------------------------------------------------------------
# Timing constraints
# ------------------------------------------------------------
echo "===== Generate timing constraints ====="
sed "s|__CLK_PERIOD__|${CLK_PERIOD}|g" \
  constraints/fir_filter.sdc.in > "$SDC"
test -s "$SDC"

# ------------------------------------------------------------
# OpenROAD physical design
# ------------------------------------------------------------
echo "===== Run OpenROAD ====="
if ! openroad -exit scripts/flow_fir.tcl \
  2>&1 | tee "$RUN_DIR/openroad_log.txt"; then
  echo "ERROR: OpenROAD failed." >&2
  echo "Inspect $RUN_DIR/openroad_log.txt" >&2
  echo "Inspect $RUN_DIR/flow_diagnostics.log" >&2
  exit 4
fi

# ------------------------------------------------------------
# Feature extraction
# ------------------------------------------------------------
echo "===== Extract FIR features and physical targets ====="
python3 scripts/extract_fir_features.py \
  --netlist-json "$NETLIST_JSON" \
  --run-dir "$RUN_DIR" \
  --outdir "$RUN_DIR" \
  --top fir_filter \
  --design-id fir_filter_8tap \
  --run-id "$RUN_TAG" \
  --taps 8 \
  --data-width 8 \
  --coeff-width 8 \
  --acc-width 24 \
  --utilization "$UTIL" \
  --place-density "$PLACE_DENSITY" \
  --clock-period "$CLK_PERIOD"

# ------------------------------------------------------------
# Validate output files (not the values inside them)
# ------------------------------------------------------------
echo "===== Validate generated outputs ====="
missing=0
for f in \
  "$RUN_DIR/run_report.md" \
  "$RUN_DIR/features.csv" \
  "$RUN_DIR/physical_targets.csv" \
  "$RUN_DIR/preplacement_features.csv" \
  "$RUN_DIR/fir_features.csv" \
  "$RUN_DIR/extraction_status.json" \
  "$RUN_DIR/final.def" \
  "$RUN_DIR/final.odb"; do
  if [[ -s "$f" ]]; then
    echo "FOUND: $f"
  else
    echo "WARNING: Missing or empty: $f"
    missing=$((missing + 1))
  fi
done

if (( missing > 0 )); then
  echo "WARNING: $missing expected output(s) are missing or empty." >&2
  exit 5
fi

echo "FIR flow finished. Review outputs under: $RUN_DIR"
