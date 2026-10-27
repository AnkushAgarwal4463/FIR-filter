
#!/usr/bin/env python3
"""
Extract FIR-filter netlist features and physical-design targets.

Flow:
    Yosys JSON netlist
        -> preplacement_features.csv / .json

    OpenROAD run directory
        -> physical_targets.csv / .json

    Combined output
        -> features.csv / .json

Important:
- Missing metrics remain null/empty; they are never silently set to zero.
- Pre-placement predictors are kept separate from physical targets.
- DEF routing counts are approximate structural counts, not exact
  physical wire-segment counts.
- Timing and congestion are extracted only when recognizable metrics
  are found in the available reports/logs.
- The presence of final.odb or final.def alone does not prove that
  detailed routing completed successfully.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys

from collections import Counter
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


# ---------------------------------------------------------------------
# Regular expressions
# ---------------------------------------------------------------------

NUM = (
    r"[-+]?"
    r"(?:\d+(?:\.\d*)?|\.\d+)"
    r"(?:[eE][-+]?\d+)?"
)

# These patterns are heuristic cell-name classifiers.
# For research-quality classification, use Liberty cell-function metadata
# or a validated technology-specific cell map.
SEQ_RE = re.compile(
    r"(?:df|ed|sd|dfr|dfrtp|dfrbp|dfrtn|sdf|ff|latch|"
    r"sky130_fd_sc_hd__df|sky130_fd_sc_hd__sd)",
    re.IGNORECASE,
)

BUF_RE = re.compile(
    r"(?:buf|inv|clkbuf)",
    re.IGNORECASE,
)

COMB_RE = re.compile(
    r"(?:and|or|xor|xnor|mux|maj|fa|ha|a[oi]i|o[ai]|"
    r"nand|nor|add|sub|mul|alu|logic|cmp)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------

def read_text(path: Path) -> str:
    """Read a text file. Return an empty string if unavailable."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeError):
        return ""


def finite_float(value: Any) -> float | None:
    """Convert a value to a finite float; otherwise return None."""
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def json_safe(value: Any) -> Any:
    """Recursively convert non-finite floats to JSON null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None

    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]

    return value


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write formatted JSON, converting non-finite floats to null."""
    with path.open("w", encoding="utf-8") as f:
        json.dump(json_safe(data), f, indent=2, allow_nan=False)
        f.write("\n")


def write_csv(path: Path, data: dict[str, Any]) -> None:
    """
    Write a one-row CSV.
    Nested dictionaries/lists are omitted from CSV but remain available
    in the corresponding JSON output.
    """
    flat = {
        key: value
        for key, value in data.items()
        if not isinstance(value, (dict, list, tuple))
    }

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(flat.keys()))
        writer.writeheader()
        writer.writerow({
            key: json_safe(value)
            for key, value in flat.items()
        })


def find_first_existing(
    run_dir: Path,
    candidates: list[str],
) -> tuple[Path | None, str]:
    """Return the first existing candidate file and its text."""
    for filename in candidates:
        path = run_dir / filename
        if path.is_file():
            content = read_text(path)
            if content.strip():
                return path, content

    return None, ""


# ---------------------------------------------------------------------
# Netlist feature extraction
# ---------------------------------------------------------------------

def classify_cell(cell_type: str) -> str:
    """
    Classify a cell using name-based heuristics.

    This is not a substitute for reading the actual Liberty function
    or sequential-cell attributes.
    """
    name = str(cell_type).lower().lstrip("\\")

    if SEQ_RE.search(name):
        return "sequential"

    if BUF_RE.search(name):
        return "buffer_inverter"

    if COMB_RE.search(name):
        return "combinational"

    return "other"


def load_yosys_json(path: Path) -> dict[str, Any]:
    """Load and validate the Yosys JSON file."""
    if not path.is_file():
        raise FileNotFoundError(f"Yosys JSON netlist not found: {path}")

    try:
        design = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON netlist at {path}: {exc}"
        ) from exc

    if not isinstance(design, dict):
        raise ValueError("Yosys JSON root must be an object.")

    if not isinstance(design.get("modules"), dict):
        raise ValueError("Yosys JSON does not contain a valid 'modules' object.")

    return design


def select_top_module(
    modules: dict[str, Any],
    requested_top: str,
) -> str:
    """Select the requested top, or use a detectable fallback."""
    if requested_top in modules:
        return requested_top

    # Yosys top attributes can be represented as strings or bit strings.
    marked = []

    for name, module in modules.items():
        attributes = module.get("attributes", {})
        top_attr = str(attributes.get("top", "")).strip().strip("'")

        if top_attr in ("1", "00000000000000000000000000000001"):
            marked.append(name)

    if len(marked) == 1:
        print(
            f"WARNING: requested top '{requested_top}' was not found; "
            f"using marked top '{marked[0]}'.",
            file=sys.stderr,
        )
        return marked[0]

    if len(modules) == 1:
        only_module = next(iter(modules))
        print(
            f"WARNING: requested top '{requested_top}' was not found; "
            f"using the only module '{only_module}'.",
            file=sys.stderr,
        )
        return only_module

    raise ValueError(
        f"Top module '{requested_top}' was not found. "
        f"Available modules: {list(modules.keys())}"
    )


def netlist_features(
    path: Path,
    requested_top: str,
    design_id: str,
) -> dict[str, Any]:
    """Extract structural features from a Yosys JSON netlist."""
    design = load_yosys_json(path)
    modules = design["modules"]

    top = select_top_module(modules, requested_top)
    module = modules[top]

    cells = module.get("cells", {})
    ports = module.get("ports", {})
    netnames = module.get("netnames", {})

    if not isinstance(cells, dict):
        raise ValueError("The selected module has an invalid 'cells' object.")

    cell_types = Counter(
        str(cell.get("type", "unknown"))
        for cell in cells.values()
    )

    class_counts = Counter()

    for cell_type, count in cell_types.items():
        class_counts[classify_cell(cell_type)] += count

    # Collect integer bit IDs from net names, ports, and cell connections.
    # Constants in Yosys JSON are commonly represented as strings and
    # are deliberately excluded from integer bit-ID fanout statistics.
    all_bits: set[int] = set()
    bit_sinks: Counter[int] = Counter()

    for net in netnames.values():
        for bit in net.get("bits", []):
            if isinstance(bit, int) and not isinstance(bit, bool):
                all_bits.add(bit)

    for port in ports.values():
        for bit in port.get("bits", []):
            if isinstance(bit, int) and not isinstance(bit, bool):
                all_bits.add(bit)

    for cell in cells.values():
        connections = cell.get("connections", {})
        directions = cell.get("port_directions", {})

        for port_name, bits in connections.items():
            for bit in bits:
                if isinstance(bit, int) and not isinstance(bit, bool):
                    all_bits.add(bit)

            # Only count sinks when the Yosys JSON explicitly identifies
            # the connection as an input. Do not guess unknown directions.
            if directions.get(port_name) == "input":
                for bit in bits:
                    if isinstance(bit, int) and not isinstance(bit, bool):
                        bit_sinks[bit] += 1

    # Fanout statistics include represented integer bits with at least
    # one input sink. They are logical-netlist statistics, not routed
    # physical fanout measurements.
    fanouts = [
        bit_sinks[bit]
        for bit in all_bits
        if bit_sinks[bit] > 0
    ]

    port_bit_count = sum(
        len(port.get("bits", []))
        for port in ports.values()
    )

    return {
        "design_id": design_id,
        "top_module": top,
        "netlist_json": str(path),
        "cell_count": len(cells),
        "netname_count": len(netnames),
        "port_count": len(ports),
        "port_bit_count": port_bit_count,
        "sequential_cell_count": class_counts["sequential"],
        "combinational_cell_count": class_counts["combinational"],
        "buffer_inverter_count": class_counts["buffer_inverter"],
        "other_cell_count": class_counts["other"],
        "unique_cell_type_count": len(cell_types),
        "avg_bit_fanout": mean(fanouts) if fanouts else None,
        "max_bit_fanout": max(fanouts) if fanouts else None,
        "std_bit_fanout": (
            pstdev(fanouts)
            if len(fanouts) > 1
            else (0.0 if fanouts else None)
        ),
        "total_net_bits": sum(
            len([
                bit for bit in net.get("bits", [])
                if isinstance(bit, int) and not isinstance(bit, bool)
            ])
            for net in netnames.values()
        ),
        "unique_cell_types": dict(cell_types),
        "feature_stage": "post-synthesis / pre-placement",
        "cell_classification_method": "heuristic cell-name matching",
    }


# ---------------------------------------------------------------------
# Report metric extraction
# ---------------------------------------------------------------------

def parse_labeled_number(
    text: str,
    patterns: list[str],
) -> float | None:
    """
    Extract a number only when a supplied label pattern matches.

    No generic fallback to an arbitrary number is performed, to reduce
    the chance of recording an unrelated timing or congestion value.
    """
    for pattern in patterns:
        matches = re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE)

        for match in matches:
            value = finite_float(match.group(1))
            if value is not None:
                return value

    return None


def timing_patterns() -> dict[str, list[str]]:
    """Known label patterns; actual report formats must be verified."""
    return {
        "wns_setup_ns": [
            rf"\bworst\s+slack(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
            rf"\bwns(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
            rf"\bworst\s+negative\s+slack\s*[:=]\s*({NUM})",
        ],
        "tns_setup_ns": [
            rf"\btotal\s+negative\s+slack(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
            rf"\btns(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
        ],
        "wns_hold_ns": [
            rf"\bworst\s+slack(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
            rf"\bwns(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
            rf"\bworst\s+negative\s+slack\s*[:=]\s*({NUM})",
        ],
    }


def parse_def_routing(def_text: str) -> dict[str, Any]:
    """
    Extract approximate counts from the DEF NETS section.

    route_segment_count counts ROUTED/NEW routing clauses, not necessarily
    individual geometric wire segments. DEF wildcard coordinates, vias,
    special nets, and geometry semantics are not fully decoded here.
    """
    match = re.search(
        r"\bNETS\s+\d+\s*;(.*?)\bEND\s+NETS\b",
        def_text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not match:
        return {
            "def_nets_section_found": False,
            "routed_net_count": None,
            "route_segment_count": None,
        }

    net_section = match.group(1)

    # Split entries beginning with a DEF net-entry hyphen.
    entries = re.findall(
        r"(?ms)^[ \t]*-[ \t]+.*?;",
        net_section,
    )

    routed_net_count = 0
    route_clause_count = 0

    for entry in entries:
        clauses = re.findall(
            r"\b(?:ROUTED|FIXED|COVER|NEW)\s+[A-Za-z0-9_.$/\[\]-]+",
            entry,
            flags=re.IGNORECASE,
        )

        if clauses:
            routed_net_count += 1

        route_clause_count += len(re.findall(
            r"\b(?:ROUTED|NEW)\s+[A-Za-z0-9_.$/\[\]-]+",
            entry,
            flags=re.IGNORECASE,
        ))

    return {
        "def_nets_section_found": True,
        "routed_net_count": routed_net_count,
        "route_segment_count": route_clause_count,
    }


# ---------------------------------------------------------------------
# Physical-design targets
# ---------------------------------------------------------------------

def physical_features(run_dir: Path) -> dict[str, Any]:
    """Extract available physical targets from an OpenROAD run directory."""
    if not run_dir.is_dir():
        raise FileNotFoundError(
            f"OpenROAD run directory not found: {run_dir}"
        )

    final_def_path = run_dir / "final.def"
    final_odb_path = run_dir / "final.odb"

    def_text = read_text(final_def_path)

    # Timing report candidates. These filenames are possibilities, not
    # guarantees; the Tcl flow must generate the corresponding reports.
    setup_path, setup_text = find_first_existing(run_dir, [
        "timing_setup.rpt",
        "timing_setup_global_route.rpt",
        "setup_timing.rpt",
        "setup.rpt",
    ])

    hold_path, hold_text = find_first_existing(run_dir, [
        "timing_hold.rpt",
        "timing_hold_global_route.rpt",
        "hold_timing.rpt",
        "hold.rpt",
    ])

    congestion_path, congestion_text = find_first_existing(run_dir, [
        "congestion.rpt",
        "global_route_congestion.rpt",
        "routing_congestion.rpt",
        "grt_congestion.rpt",
    ])

    # Logs may contain summary metrics when separate reports are absent.
    log_candidates = [
        "openroad_log.txt",
        "flow_diagnostics.log",
        "5_1_grt.log",
        "5_2_route.log",
        "global_route.log",
        "detailed_route.log",
    ]

    log_texts = {}
    for filename in log_candidates:
        path = run_dir / filename
        if path.is_file():
            content = read_text(path)
            if content.strip():
                log_texts[filename] = content

    all_log_text = "\n".join(log_texts.values())

    setup_source = setup_text or all_log_text
    hold_source = hold_text or all_log_text
    congestion_source = congestion_text or all_log_text

    patterns = timing_patterns()

    wns_setup = parse_labeled_number(
        setup_source,
        patterns["wns_setup_ns"],
    )

    tns_setup = parse_labeled_number(
        setup_source,
        patterns["tns_setup_ns"],
    )

    wns_hold = parse_labeled_number(
        hold_source,
        patterns["wns_hold_ns"],
    )

    # Do not parse arbitrary numbers from routing reports. Only accept
    # recognizable overflow labels or an explicit zero-overflow message.
    congestion_overflow = parse_labeled_number(
        congestion_source,
        [
            rf"\btotal\s+(?:routing\s+)?overflow\s*[:=]\s*({NUM})",
            rf"\b(?:routing\s+)?overflow\s*[:=]\s*({NUM})",
            rf"\bcongestion\s+overflow\s*[:=]\s*({NUM})",
        ],
    )

    if congestion_overflow is None:
        zero_overflow_patterns = [
            r"\bno\s+overflowing\s+gcell(?:s)?\b",
            r"\bzero\s+routing\s+overflow\b",
            r"\btotal\s+(?:routing\s+)?overflow\s*[:=]\s*0+(?:\.0+)?\b",
        ]

        if any(
            re.search(
                pattern,
                congestion_source,
                flags=re.IGNORECASE,
            )
            for pattern in zero_overflow_patterns
        ):
            congestion_overflow = 0.0

    def_counts = parse_def_routing(def_text) if def_text else {
        "def_nets_section_found": False,
        "routed_net_count": None,
        "route_segment_count": None,
    }

    # This extractor intentionally does not claim a wirelength measurement.
    # A reliable value needs a validated OpenROAD report or a DEF geometry
    # parser that handles database units and route geometry correctly.
    wirelength_um = None

    # Artifact existence is reported separately from flow completion.
    artifacts_present = final_def_path.is_file() and final_odb_path.is_file()

    return {
        "final_def_available": final_def_path.is_file(),
        "final_odb_available": final_odb_path.is_file(),
        "final_def_nonempty": bool(def_text.strip()),
        "artifacts_present": artifacts_present,
        **def_counts,
        "wns_setup_ns": wns_setup,
        "tns_setup_ns": tns_setup,
        "wns_hold_ns": wns_hold,
        "congestion_overflow": congestion_overflow,
        "wirelength_um": wirelength_um,
        "setup_report_source": (
            str(setup_path) if setup_path else (
                "logs" if not setup_text and all_log_text else None
            )
        ),
        "hold_report_source": (
            str(hold_path) if hold_path else (
                "logs" if not hold_text and all_log_text else None
            )
        ),
        "congestion_report_source": (
            str(congestion_path) if congestion_path else (
                "logs" if not congestion_text and all_log_text else None
            )
        ),
        "log_files_found": list(log_texts.keys()),
        "target_measurement_status": {
            "wns_setup_ns": "measured" if wns_setup is not None else "unavailable",
            "tns_setup_ns": "measured" if tns_setup is not None else "unavailable",
            "wns_hold_ns": "measured" if wns_hold is not None else "unavailable",
            "congestion_overflow": (
                "measured"
                if congestion_overflow is not None
                else "unavailable"
            ),
            "wirelength_um": "unavailable_not_implemented",
        },
        "note": (
            "Routing counts are approximate DEF clause counts. "
            "Timing and congestion values are populated only when a "
            "recognized metric label is found. Artifact existence alone "
            "does not prove successful detailed routing. "
            "wirelength_um remains null until measured with a validated "
            "OpenROAD report or DEF geometry parser."
        ),
        "target_feature_stage": (
            "post-placement/routing targets; do not use as predictors "
            "when predicting these same outcomes"
        ),
    }


# ---------------------------------------------------------------------
# Combined output and command-line interface
# ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract FIR netlist features and physical-design targets "
            "from Yosys JSON and an OpenROAD run directory."
        )
    )

    parser.add_argument(
        "--netlist-json",
        default="fir_project/synth/fir_filter_netlist.json",
        help="Path to the synthesized Yosys JSON netlist.",
    )

    parser.add_argument(
        "--run-dir",
        default="fir_project/runs/fir_8tap_w8_util40_clk10",
        help="Path to the specific OpenROAD run directory.",
    )

    parser.add_argument(
        "--outdir",
        default=None,
        help="Output directory. Defaults to --run-dir.",
    )

    parser.add_argument(
        "--top",
        default="fir_filter",
        help="Top-module name in the Yosys JSON netlist.",
    )

    parser.add_argument(
        "--design-id",
        default="fir_filter_8tap",
        help="Identifier written to the feature files.",
    )

    parser.add_argument(
        "--utilization",
        type=float,
        default=None,
        help="Utilization constraint in percent, e.g. 40.",
    )

    parser.add_argument(
        "--clock-period",
        type=float,
        default=None,
        help="Clock period in nanoseconds, e.g. 10.0.",
    )

    args = parser.parse_args()

    netlist_path = Path(args.netlist_json)
    run_dir = Path(args.run_dir)
    outdir = Path(args.outdir) if args.outdir else run_dir

    if args.utilization is not None:
        if not math.isfinite(args.utilization) or not (
            0.0 < args.utilization <= 100.0
        ):
            parser.error("--utilization must be greater than 0 and at most 100.")

    if args.clock_period is not None:
        if not math.isfinite(args.clock_period) or args.clock_period <= 0:
            parser.error("--clock-period must be a positive finite number.")

    try:
        outdir.mkdir(parents=True, exist_ok=True)

        pre = netlist_features(
            path=netlist_path,
            requested_top=args.top,
            design_id=args.design_id,
        )

        pre["utilization_constraint_percent"] = args.utilization
        pre["clock_period_ns"] = args.clock_period

        post = physical_features(run_dir)

        # Save predictor and target datasets separately.
        write_json(outdir / "preplacement_features.json", pre)
        write_csv(outdir / "preplacement_features.csv", pre)

        write_json(outdir / "physical_targets.json", post)
        write_csv(outdir / "physical_targets.csv", post)

        # Keep the pre-placement feature_stage field intact in the
        # combined CSV; target stage has its own explicit field.
        combined = {**pre, **post}

        write_json(outdir / "features.json", combined)
        write_csv(outdir / "features.csv", combined)

        # Also provide a small extraction status file for automation.
        status = {
            "design_id": args.design_id,
            "netlist_json": str(netlist_path),
            "run_dir": str(run_dir),
            "output_dir": str(outdir),
            "final_def_available": post["final_def_available"],
            "final_odb_available": post["final_odb_available"],
            "artifacts_present": post["artifacts_present"],
            "target_measurement_status": post["target_measurement_status"],
        }

        write_json(outdir / "extraction_status.json", status)

    except (OSError, ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("\n===== FIR Feature Extraction =====")
    print(f"Netlist JSON : {netlist_path}")
    print(f"OpenROAD run : {run_dir}")
    print(f"Output dir   : {outdir}")
    print(f"Cells        : {pre['cell_count']}")
    print(f"Sequential   : {pre['sequential_cell_count']}")
    print(f"Combinational: {pre['combinational_cell_count']}")
    print(f"DEF available: {post['final_def_available']}")
    print(f"ODB available: {post['final_odb_available']}")
    print(f"Artifacts present: {post['artifacts_present']}")

    print("\n===== Extracted Physical Targets =====")
    for key in (
        "wns_setup_ns",
        "tns_setup_ns",
        "wns_hold_ns",
        "congestion_overflow",
        "wirelength_um",
    ):
        print(f"{key}: {post.get(key)}")

    print("\n===== Generated Files =====")
    for filename in (
        "preplacement_features.csv",
        "preplacement_features.json",
        "physical_targets.csv",
        "physical_targets.json",
        "features.csv",
        "features.json",
        "extraction_status.json",
    ):
        print(f"  {outdir / filename}")

    unavailable = [
        key
        for key, value in post["target_measurement_status"].items()
        if value != "measured"
    ]

    if unavailable:
        print(
            "\nWARNING: Some physical targets remain unavailable: "
            + ", ".join(unavailable)
        )
        print(
            "Inspect the actual OpenROAD reports/logs and validate the "
            "metric formats before using these targets for ML training."
        )

    print("\nExtraction finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
