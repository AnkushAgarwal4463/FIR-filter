
#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# FIR SYNTHESIS + PHYSICAL DESIGN FLOW
# Parameterized configuration for multi-run dataset generation
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "$PROJECT_DIR"

# ------------------------------------------------------------
# 1. Load configuration
# ------------------------------------------------------------

CONFIG_FILE="config/sky130_example.env"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: Configuration file not found: $CONFIG_FILE"
    exit 1
fi

export TAPS="${TAPS:-8}"
export DATA_WIDTH="${DATA_WIDTH:-8}"
export COEFF_WIDTH="${COEFF_WIDTH:-8}"
export ACC_WIDTH="${ACC_WIDTH:-$((DATA_WIDTH + COEFF_WIDTH + 8))}"

export UTIL="${UTIL:-40}"
export CLK_PERIOD="${CLK_PERIOD:-10}"
export PLACE_DENSITY="${PLACE_DENSITY:-$(awk "BEGIN {print ${UTIL}/100}")}"

source "$CONFIG_FILE"

export TAPS="${TAPS:-8}"
export DATA_WIDTH="${DATA_WIDTH:-8}"
export COEFF_WIDTH="${COEFF_WIDTH:-8}"
export ACC_WIDTH="${ACC_WIDTH:-$((DATA_WIDTH + COEFF_WIDTH + 8))}"
export UTIL="${UTIL:-40}"
export CLK_PERIOD="${CLK_PERIOD:-10}"
export PLACE_DENSITY="${PLACE_DENSITY:-$(awk "BEGIN {print ${UTIL}/100}")}"

# ------------------------------------------------------------
# 2. Validate configuration
# ------------------------------------------------------------

for value in TAPS DATA_WIDTH COEFF_WIDTH ACC_WIDTH UTIL; do
    if ! [[ "${!value}" =~ ^[0-9]+$ ]]; then
        echo "ERROR: $value must be a positive integer."
        exit 1
    fi
done

if ! [[ "$CLK_PERIOD" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "ERROR: CLK_PERIOD must be numeric."
    exit 1
fi

if ! [[ "$PLACE_DENSITY" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "ERROR: PLACE_DENSITY must be numeric."
    exit 1
fi

if (( TAPS < 1 || DATA_WIDTH < 1 || COEFF_WIDTH < 1 )); then
    echo "ERROR: TAPS, DATA_WIDTH and COEFF_WIDTH must be positive."
    exit 1
fi

if (( ACC_WIDTH < DATA_WIDTH + COEFF_WIDTH )); then
    echo "ERROR: ACC_WIDTH must be >= DATA_WIDTH + COEFF_WIDTH."
    exit 1
fi

if (( UTIL < 1 || UTIL > 100 )); then
    echo "ERROR: UTIL must be between 1 and 100."
    exit 1
fi

# ------------------------------------------------------------
# 3. Create unique run identifiers
# ------------------------------------------------------------

CLK_TAG="${CLK_PERIOD//./p}"

export RUN_TAG="fir_t${TAPS}_dw${DATA_WIDTH}_cw${COEFF_WIDTH}_aw${ACC_WIDTH}_u${UTIL}_clk${CLK_TAG}"

export RUN_DIR="fir_project/runs/${RUN_TAG}"
export SYNTH_DIR="fir_project/synth"

export NETLIST="${SYNTH_DIR}/fir_filter_netlist.v"
export NETLIST_JSON="${SYNTH_DIR}/fir_filter_netlist.json"
export SDC="${RUN_DIR}/fir_filter.sdc"

mkdir -p "$SYNTH_DIR" "$RUN_DIR"

# ------------------------------------------------------------
# 4. Print configuration
# ------------------------------------------------------------

echo "============================================================"
echo "FIR PHYSICAL DESIGN CONFIGURATION"
echo "============================================================"
echo "TAPS          : $TAPS"
echo "DATA_WIDTH    : $DATA_WIDTH"
echo "COEFF_WIDTH   : $COEFF_WIDTH"
echo "ACC_WIDTH     : $ACC_WIDTH"
echo "UTIL          : $UTIL"
echo "PLACE_DENSITY : $PLACE_DENSITY"
echo "CLK_PERIOD    : $CLK_PERIOD ns"
echo "RUN_TAG       : $RUN_TAG"
echo "RUN_DIR       : $RUN_DIR"
echo "============================================================"

# ------------------------------------------------------------
# 5. Check required tools
# ------------------------------------------------------------

for tool in yosys openroad python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: Required tool not found: $tool"
        exit 1
    fi
done

# ------------------------------------------------------------
# 6. Check required project files
# ------------------------------------------------------------

required_files=(
    "rtl/fir_filter.v"
    "scripts/synth_fir.ys"
    "scripts/flow_fir.tcl"
    "scripts/extract_fir_features.py"
    "constraints/fir_filter.sdc.in"
)

for file in "${required_files[@]}"; do
    if [[ ! -f "$file" ]]; then
        echo "ERROR: Required file missing: $file"
        exit 1
    fi
done

# ------------------------------------------------------------
# 7. Check Liberty configuration
# ------------------------------------------------------------

if [[ -z "${LIBERTY:-}" ]]; then
    echo "ERROR: LIBERTY is not defined in $CONFIG_FILE"
    exit 1
fi

if [[ ! -f "$LIBERTY" ]]; then
    echo "ERROR: Liberty file not found: $LIBERTY"
    exit 1
fi

# ------------------------------------------------------------
# 8. Generate parameterized Yosys script
# ------------------------------------------------------------

GENERATED_SYNTH="${SYNTH_DIR}/synth_fir_${RUN_TAG}.ys"

sed \
    -e "s|__LIB_FILE__|${LIBERTY}|g" \
    -e "s|__TAPS__|${TAPS}|g" \
    -e "s|__DATA_WIDTH__|${DATA_WIDTH}|g" \
    -e "s|__COEFF_WIDTH__|${COEFF_WIDTH}|g" \
    -e "s|__ACC_WIDTH__|${ACC_WIDTH}|g" \
    "scripts/synth_fir.ys" > "$GENERATED_SYNTH"

echo "Generated synthesis script: $GENERATED_SYNTH"

if grep -Eq '__[A-Z_]+__' "$GENERATED_SYNTH"; then
    echo "ERROR: Unresolved placeholders in generated Yosys script."
    grep -nE '__[A-Z_]+__' "$GENERATED_SYNTH"
    exit 1
fi

# ------------------------------------------------------------
# 9. Run Yosys synthesis
# ------------------------------------------------------------

echo
echo "===== RUNNING YOSYS SYNTHESIS ====="

# Remove old outputs to prevent stale netlists being reused.
rm -f "$NETLIST" "$NETLIST_JSON"

yosys -s "$GENERATED_SYNTH" \
    2>&1 | tee "${RUN_DIR}/yosys.log"

echo
echo "===== VERIFY YOSYS NETLIST OUTPUTS ====="

if [[ ! -s "$NETLIST" ]]; then
    echo "ERROR: Synthesized Verilog netlist is missing or empty."
    exit 1
fi

if [[ ! -s "$NETLIST_JSON" ]]; then
    echo "ERROR: Synthesized JSON netlist is missing or empty."
    ls -lh "$SYNTH_DIR"
    exit 1
fi

python3 - "$NETLIST_JSON" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])

try:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
except (json.JSONDecodeError, OSError) as exc:
    raise SystemExit(f"ERROR: Invalid Yosys JSON netlist: {exc}")

if not isinstance(data, dict) or not isinstance(data.get("modules"), dict):
    raise SystemExit("ERROR: JSON does not contain a valid Yosys modules object.")

if not data["modules"]:
    raise SystemExit("ERROR: Yosys JSON contains no modules.")

print("JSON validation: PASSED")
print("JSON size:", path.stat().st_size, "bytes")
print("Modules:", list(data["modules"].keys()))
PY

echo "Verilog netlist: $NETLIST"
echo "JSON netlist   : $NETLIST_JSON"

# ------------------------------------------------------------
# 10. Generate SDC
# ------------------------------------------------------------

sed \
    -e "s|__CLK_PERIOD__|${CLK_PERIOD}|g" \
    "constraints/fir_filter.sdc.in" > "$SDC"

echo "Generated SDC: $SDC"

# ------------------------------------------------------------
# 11. Run OpenROAD physical design
# ------------------------------------------------------------

echo
echo "===== RUNNING OPENROAD PHYSICAL DESIGN ====="

export NETLIST
export NETLIST_JSON
export SDC
export RUN_DIR
export RUN_TAG
export UTIL
export PLACE_DENSITY
export CLK_PERIOD
export TAPS
export DATA_WIDTH
export COEFF_WIDTH
export ACC_WIDTH

openroad -exit scripts/flow_fir.tcl \
    2>&1 | tee "${RUN_DIR}/openroad.log"

# ------------------------------------------------------------
# 12. Extract features and physical targets
# ------------------------------------------------------------

echo
echo "===== EXTRACTING FEATURES AND PHYSICAL TARGETS ====="

python3 scripts/extract_fir_features.py \
    --run-dir "$RUN_DIR" \
    --netlist-json "$NETLIST_JSON" \
    --taps "$TAPS" \
    --data-width "$DATA_WIDTH" \
    --coeff-width "$COEFF_WIDTH" \
    --acc-width "$ACC_WIDTH" \
    2>&1 | tee "${RUN_DIR}/feature_extraction.log"

# ------------------------------------------------------------
# 13. Verify physical_targets.csv
# ------------------------------------------------------------

echo
echo "===== VERIFY PHYSICAL TARGETS CSV ====="

python3 - "$RUN_DIR/physical_targets.csv" <<'PY'
import csv
import sys
from pathlib import Path

path = Path(sys.argv[1])

print("CSV path:", path.resolve())
print("File exists:", path.is_file())

if not path.is_file():
    raise SystemExit("ERROR: physical_targets.csv was not generated.")

with path.open(newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    print("Columns:", reader.fieldnames)

    rows = list(reader)
    print("Row count:", len(rows))

    if not rows:
        raise SystemExit("ERROR: physical_targets.csv contains no data rows.")

    for key, value in rows[0].items():
        if "congestion" in key or "routing" in key:
            print(f"{key} = {value}")
PY

# ------------------------------------------------------------
# 14. Verify expected outputs
# ------------------------------------------------------------

echo
echo "===== FINAL OUTPUT CHECK ====="

for file in \
    "$NETLIST" \
    "$NETLIST_JSON" \
    "$SDC" \
    "${RUN_DIR}/physical_targets.csv" \
    "${RUN_DIR}/preplacement_features.csv" \
    "${RUN_DIR}/run_report.md"
do
    if [[ -f "$file" ]]; then
        echo "FOUND: $file"
    else
        echo "WARNING: Not found: $file"
    fi
done

echo
echo "============================================================"
echo "FIR FLOW COMPLETED"
echo "RUN_TAG: $RUN_TAG"
echo "RUN_DIR: $RUN_DIR"
echo "============================================================"
