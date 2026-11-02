
#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# FIR SYNTHESIS + PHYSICAL DESIGN + FEATURE EXTRACTION
# Supports the 20-configuration FIR experiment.
#
# Run from: fir-filter-cross-design/
# ============================================================

# ------------------------------------------------------------
# 1. Load environment and configuration
# ------------------------------------------------------------

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PLACE_DENSITY_OVERRIDE="${PLACE_DENSITY:-}"

source config/sky130_example.env

: "${LIBERTY:?LIBERTY is not configured}"
: "${TECH_LEF:?TECH_LEF is not configured}"
: "${CELL_LEF:?CELL_LEF is not configured}"

export TAPS="${TAPS:-8}"
export DATA_WIDTH="${DATA_WIDTH:-8}"
export COEFF_WIDTH="${COEFF_WIDTH:-8}"
export ACC_WIDTH="${ACC_WIDTH:-$((DATA_WIDTH + COEFF_WIDTH + 8))}"
export UTIL="${UTIL:-40}"
export CLK_PERIOD="${CLK_PERIOD:-10.0}"

# ------------------------------------------------------------
# 2. Validate configuration
# ------------------------------------------------------------

case "$TAPS" in
    4|8|12) ;;
    *)
        echo "ERROR: TAPS must be 4, 8, or 12."
        exit 2
        ;;
esac

case "$DATA_WIDTH" in
    8|16) ;;
    *)
        echo "ERROR: DATA_WIDTH must be 8 or 16."
        exit 2
        ;;
esac

if [[ "$COEFF_WIDTH" != "8" ]]; then
    echo "ERROR: COEFF_WIDTH must be 8 for this experiment."
    exit 2
fi

case "$UTIL" in
    20|30|40|50|60) ;;
    *)
        echo "ERROR: UTIL must be 20, 30, 40, 50, or 60."
        exit 2
        ;;
esac

if ! [[ "$ACC_WIDTH" =~ ^[0-9]+$ ]]; then
    echo "ERROR: ACC_WIDTH must be an integer."
    exit 2
fi

MIN_ACC_WIDTH=$((DATA_WIDTH + COEFF_WIDTH + 8))

if (( ACC_WIDTH < MIN_ACC_WIDTH )); then
    echo "ERROR: ACC_WIDTH=$ACC_WIDTH is below the configured minimum $MIN_ACC_WIDTH."
    exit 2
fi

if ! awk -v c="$CLK_PERIOD" \
    'BEGIN { exit !(c > 0) }'; then
    echo "ERROR: CLK_PERIOD must be a positive number."
    exit 2
fi

if [[ -n "$PLACE_DENSITY_OVERRIDE" ]]; then
    export PLACE_DENSITY="$PLACE_DENSITY_OVERRIDE"
else
    export PLACE_DENSITY="$(
        awk -v u="$UTIL" 'BEGIN { printf "%.2f", u / 100 }'
    )"
fi

if ! awk -v d="$PLACE_DENSITY" \
    'BEGIN { exit !(d > 0 && d <= 1) }'; then
    echo "ERROR: PLACE_DENSITY must be greater than 0 and at most 1."
    exit 2
fi

export CLK_PERIOD_NS="$CLK_PERIOD"

CLK_TAG="$(awk -v c="$CLK_PERIOD" 'BEGIN { printf "%g", c + 0 }')"

# ------------------------------------------------------------
# 3. Generate unique run identification
# ------------------------------------------------------------

export RUN_TAG="fir_t${TAPS}_dw${DATA_WIDTH}_cw${COEFF_WIDTH}_aw${ACC_WIDTH}_u${UTIL}_clk${CLK_TAG}"

# Do not permit a caller to silently override the run identity.
if [[ -n "${RUN_TAG_OVERRIDE:-}" ]]; then
    echo "ERROR: RUN_TAG_OVERRIDE is not supported."
    echo "RUN_TAG is generated from the actual configuration."
    exit 2
fi

export RUN_DIR="fir_project/runs/${RUN_TAG}"

export NETLIST="fir_project/synth/fir_filter_${RUN_TAG}.v"
export NETLIST_JSON="fir_project/synth/fir_filter_${RUN_TAG}.json"
export SDC="fir_project/synth/fir_filter_${RUN_TAG}.sdc"

export DESIGN_ID="fir_filter_t${TAPS}_dw${DATA_WIDTH}"

mkdir -p fir_project/synth

# ------------------------------------------------------------
# 4. Print configuration
# ------------------------------------------------------------

echo
echo "============================================================"
echo "FIR CONFIGURATION"
echo "============================================================"

printf 'TAPS          = %s\n' "$TAPS"
printf 'DATA_WIDTH    = %s\n' "$DATA_WIDTH"
printf 'COEFF_WIDTH   = %s\n' "$COEFF_WIDTH"
printf 'ACC_WIDTH     = %s\n' "$ACC_WIDTH"
printf 'UTIL          = %s\n' "$UTIL"
printf 'PLACE_DENSITY = %s\n' "$PLACE_DENSITY"
printf 'CLK_PERIOD    = %s ns\n' "$CLK_PERIOD"
printf 'RUN_TAG       = %s\n' "$RUN_TAG"
printf 'RUN_DIR       = %s\n' "$RUN_DIR"
printf 'NETLIST       = %s\n' "$NETLIST"
printf 'NETLIST_JSON  = %s\n' "$NETLIST_JSON"

echo "============================================================"

# ------------------------------------------------------------
# 5. Check required files and tools
# ------------------------------------------------------------

required_files=(
    "$LIBERTY"
    "$TECH_LEF"
    "$CELL_LEF"
    "rtl/fir_filter.v"
    "scripts/synth_fir.ys"
    "scripts/flow_fir.tcl"
    "scripts/extract_fir_features.py"
    "constraints/fir_filter.sdc.in"
)

for file in "${required_files[@]}"; do
    if [[ ! -f "$file" ]]; then
        echo "ERROR: Required file not found: $file"
        exit 2
    fi
done

for tool in yosys openroad python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: Required tool not found: $tool"
        exit 2
    fi
done

# ------------------------------------------------------------
# 6. Prepare clean run directory
# ------------------------------------------------------------

if [[ -d "$RUN_DIR" ]]; then
    echo "Removing previous results for this configuration."
    rm -rf "$RUN_DIR"
fi

mkdir -p "$RUN_DIR"

rm -f "$NETLIST" "$NETLIST_JSON" "$SDC"

# ------------------------------------------------------------
# 7. Save configuration metadata
# ------------------------------------------------------------

python3 - "$RUN_DIR/configuration.json" <<'PY'
import json
import os
import sys
from pathlib import Path

configuration = {
    "design_id": os.environ["DESIGN_ID"],
    "run_id": os.environ["RUN_TAG"],
    "top": "fir_filter",
    "taps": int(os.environ["TAPS"]),
    "data_width": int(os.environ["DATA_WIDTH"]),
    "coeff_width": int(os.environ["COEFF_WIDTH"]),
    "acc_width": int(os.environ["ACC_WIDTH"]),
    "utilization": int(os.environ["UTIL"]),
    "place_density": float(os.environ["PLACE_DENSITY"]),
    "clock_period_ns": float(os.environ["CLK_PERIOD"]),
}

path = Path(sys.argv[1])
path.write_text(
    json.dumps(configuration, indent=2) + "\n",
    encoding="utf-8"
)

print("Configuration metadata saved:", path)
PY

# ------------------------------------------------------------
# 8. Generate parameterized Yosys script
# ------------------------------------------------------------

echo
echo "===== GENERATE YOSYS SCRIPT ====="

export LIBERTY
export NETLIST
export NETLIST_JSON

python3 - <<'PY'
import os
import re
from pathlib import Path

template_path = Path("scripts/synth_fir.ys")
output_path = Path("fir_project/synth/synth_fir.generated.ys")

replacements = {
    "__DATA_WIDTH__": os.environ["DATA_WIDTH"],
    "__COEFF_WIDTH__": os.environ["COEFF_WIDTH"],
    "__TAPS__": os.environ["TAPS"],
    "__ACC_WIDTH__": os.environ["ACC_WIDTH"],
    "__LIB_FILE__": os.environ["LIBERTY"],
    "__NETLIST_JSON__": os.environ["NETLIST_JSON"],
    "__NETLIST_VERILOG__": os.environ["NETLIST"],
}

script = template_path.read_text(encoding="utf-8")

for placeholder, value in replacements.items():
    script = script.replace(placeholder, value)

remaining = re.findall(r"__[A-Z0-9_]+__", script)

if remaining:
    raise SystemExit(
        "ERROR: Unresolved Yosys placeholders: "
        + ", ".join(sorted(set(remaining)))
    )

output_path.write_text(script, encoding="utf-8")

print("Generated:", output_path)
print("All Yosys placeholders resolved.")
PY

test -s fir_project/synth/synth_fir.generated.ys

# ------------------------------------------------------------
# 9. Run Yosys synthesis
# ------------------------------------------------------------

echo
echo "===== RUN YOSYS SYNTHESIS ====="

yosys -l "$RUN_DIR/synthesis.log" \
    -s fir_project/synth/synth_fir.generated.ys

for file in "$NETLIST" "$NETLIST_JSON"; do
    if [[ ! -s "$file" ]]; then
        echo "ERROR: Synthesis output missing or empty: $file"
        exit 3
    fi
done

# ------------------------------------------------------------
# 10. Verify synthesized JSON
# ------------------------------------------------------------

echo
echo "===== VERIFY SYNTHESIZED NETLIST ====="

python3 - "$NETLIST_JSON" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))

modules = data.get("modules", {})

if "fir_filter" not in modules:
    raise SystemExit("ERROR: fir_filter top module missing from JSON.")

module = modules["fir_filter"]

print("Top module: fir_filter")
print("Cell count:", len(module.get("cells", {})))
print("Port count:", len(module.get("ports", {})))

if not module.get("cells"):
    raise SystemExit("ERROR: Synthesized module contains no cells.")

if not module.get("ports"):
    raise SystemExit("ERROR: Synthesized module contains no ports.")

print("Synthesized JSON validation passed.")
PY

# ------------------------------------------------------------
# 11. Generate timing constraints
# ------------------------------------------------------------

echo
echo "===== GENERATE TIMING CONSTRAINTS ====="

sed "s|__CLK_PERIOD__|${CLK_PERIOD}|g" \
    constraints/fir_filter.sdc.in > "$SDC"

if grep -q '__CLK_PERIOD__' "$SDC"; then
    echo "ERROR: Unresolved clock-period placeholder."
    exit 3
fi

test -s "$SDC"

# ------------------------------------------------------------
# 12. Run OpenROAD physical design
# ------------------------------------------------------------

echo
echo "===== RUN OPENROAD PHYSICAL DESIGN ====="

if ! openroad -exit scripts/flow_fir.tcl \
    2>&1 | tee "$RUN_DIR/openroad_log.txt"; then

    echo "ERROR: OpenROAD physical-design flow failed."
    echo "Check: $RUN_DIR/openroad_log.txt"
    echo "Check: $RUN_DIR/flow_diagnostics.log"
    exit 4
fi

# ------------------------------------------------------------
# 13. Verify final physical-design database
# ------------------------------------------------------------

echo
echo "===== VERIFY PHYSICAL-DESIGN OUTPUTS ====="

for file in "$RUN_DIR/final.def" "$RUN_DIR/final.odb"; do
    if [[ ! -s "$file" ]]; then
        echo "ERROR: Required physical-design output missing: $file"
        exit 4
    fi
done

# ------------------------------------------------------------
# 14. Extract netlist and physical-design features
# ------------------------------------------------------------

echo
echo "===== EXTRACT FEATURES AND PHYSICAL TARGETS ====="

python3 scripts/extract_fir_features.py \
    --netlist-json "$NETLIST_JSON" \
    --run-dir "$RUN_DIR" \
    --outdir "$RUN_DIR" \
    --top fir_filter \
    --design-id "$DESIGN_ID" \
    --run-id "$RUN_TAG" \
    --taps "$TAPS" \
    --data-width "$DATA_WIDTH" \
    --coeff-width "$COEFF_WIDTH" \
    --acc-width "$ACC_WIDTH" \
    --utilization "$UTIL" \
    --place-density "$PLACE_DENSITY" \
    --clock-period "$CLK_PERIOD"

# ------------------------------------------------------------
# 15. Validate extracted files
# ------------------------------------------------------------

echo
echo "===== VALIDATE EXTRACTED OUTPUTS ====="

required_outputs=(
    "$RUN_DIR/configuration.json"
    "$RUN_DIR/final.def"
    "$RUN_DIR/final.odb"
    "$RUN_DIR/features.csv"
    "$RUN_DIR/physical_targets.csv"
    "$RUN_DIR/preplacement_features.csv"
    "$RUN_DIR/fir_features.csv"
    "$RUN_DIR/fir_features_status.csv"
    "$RUN_DIR/extraction_status.json"
    "$RUN_DIR/run_report.md"
)

missing=0

for file in "${required_outputs[@]}"; do
    if [[ -s "$file" ]]; then
        echo "FOUND: $file"
    else
        echo "MISSING OR EMPTY: $file"
        missing=$((missing + 1))
    fi
done

if (( missing > 0 )); then
    echo "ERROR: $missing required output(s) are missing."
    exit 5
fi

# ------------------------------------------------------------
# 16. Final configuration summary
# ------------------------------------------------------------

echo
echo "============================================================"
echo "FIR PHYSICAL-DESIGN RUN COMPLETED"
echo "============================================================"

echo "Run ID:       $RUN_TAG"
echo "Run directory: $RUN_DIR"
echo "Netlist JSON:  $NETLIST_JSON"
echo "Netlist Verilog: $NETLIST"

echo
echo "Generated feature files:"
ls -lh \
    "$RUN_DIR/features.csv" \
    "$RUN_DIR/physical_targets.csv" \
    "$RUN_DIR/preplacement_features.csv" \
    "$RUN_DIR/extraction_status.json" \
    "$RUN_DIR/run_report.md"

echo
echo "FIR flow completed successfully."
