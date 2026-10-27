
#!/usr/bin/env python3
"""
FIR filter feature and physical-target extractor.

Outputs:
  preplacement_features.csv / .json
  physical_targets.csv / .json
  features.csv / .json
  fir_features.csv
  extraction_status.json

Missing metrics remain null. No unverified values are fabricated.
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

NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"

SEQ_RE = re.compile(
    r"(?:sky130_fd_sc_hd__(?:df|sd|ed|sdf)|"
    r"\b(?:dfrtp|dfrbp|dfrtn|dfr|dff|flipflop|latch)\b)",
    re.I,
)
BUF_RE = re.compile(r"(?:buf|inv|clkbuf)", re.I)
COMB_RE = re.compile(
    r"(?:and|or|xor|xnor|mux|maj|fa|ha|aoi|oai|nand|nor|"
    r"add|sub|mul|alu|logic|cmp)",
    re.I,
)


# ---------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------

def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def finite_float(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, allow_nan=False)
        f.write("\n")


def write_csv(path: Path, data: dict[str, Any]) -> None:
    flat = {
        k: v for k, v in data.items()
        if not isinstance(v, (dict, list, tuple))
    }
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(flat))
        writer.writeheader()
        writer.writerow(flat)


def write_rows_csv(
    path: Path,
    fieldnames: list[str],
    row: dict[str, Any],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({key: row.get(key) for key in fieldnames})


def last_number(text: str, patterns: list[str]) -> float | None:
    """Return the last value matching the first pattern that matches."""
    for pattern in patterns:
        matches = list(re.finditer(pattern, text, re.I | re.M))
        for match in reversed(matches):
            value = finite_float(match.group(1))
            if value is not None:
                return value
    return None


def first_existing_text(
    directory: Path,
    filenames: list[str],
) -> tuple[Path | None, str]:
    for filename in filenames:
        path = directory / filename
        if path.is_file():
            text = read_text(path)
            if text.strip():
                return path, text
    return None, ""


# ---------------------------------------------------------------------
# Netlist extraction
# ---------------------------------------------------------------------

def classify_cell(cell_type: str) -> str:
    name = str(cell_type).lower().lstrip("\\")
    if SEQ_RE.search(name):
        return "sequential"
    if BUF_RE.search(name):
        return "buffer_inverter"
    if COMB_RE.search(name):
        return "combinational"
    return "other"


def load_netlist(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Netlist JSON not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON netlist: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("modules"), dict):
        raise ValueError("Expected a Yosys JSON file containing 'modules'.")

    return data


def select_top(
    modules: dict[str, Any],
    requested_top: str,
) -> str:
    if requested_top in modules:
        return requested_top

    marked = []
    for name, module in modules.items():
        value = str(module.get("attributes", {}).get("top", "")).strip("'")
        if value.lstrip("0") == "1":
            marked.append(name)

    if len(marked) == 1:
        print(f"WARNING: using detected top module {marked[0]}", file=sys.stderr)
        return marked[0]

    if len(modules) == 1:
        return next(iter(modules))

    raise ValueError(
        f"Top '{requested_top}' not found. Available: {list(modules)}"
    )


def extract_netlist_features(
    path: Path,
    requested_top: str,
    design_id: str,
) -> dict[str, Any]:
    design = load_netlist(path)
    top = select_top(design["modules"], requested_top)
    module = design["modules"][top]

    cells = module.get("cells", {})
    ports = module.get("ports", {})
    netnames = module.get("netnames", {})

    if not isinstance(cells, dict):
        raise ValueError("Invalid cells object in Yosys JSON.")

    cell_types = Counter(
        str(cell.get("type", "unknown")) for cell in cells.values()
    )
    class_counts = Counter()
    for cell_type, count in cell_types.items():
        class_counts[classify_cell(cell_type)] += count

    # Use actual cell input-port directions to calculate logical fanout.
    # If directions are missing, leave fanout null rather than inventing it.
    all_bits: set[int] = set()
    sink_counts: Counter[int] = Counter()
    direction_metadata_found = False

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

        if isinstance(directions, dict) and directions:
            direction_metadata_found = True

        for port_name, bits in connections.items():
            direction = str(directions.get(port_name, "")).lower()

            for bit in bits:
                if isinstance(bit, int) and not isinstance(bit, bool):
                    all_bits.add(bit)
                    if direction == "input":
                        sink_counts[bit] += 1

    fanouts = (
        [sink_counts[b] for b in all_bits if sink_counts[b] > 0]
        if direction_metadata_found
        else []
    )

    net_bit_total = sum(
        sum(
            isinstance(bit, int) and not isinstance(bit, bool)
            for bit in net.get("bits", [])
        )
        for net in netnames.values()
    )

    return {
        "design_id": design_id,
        "top_module": top,
        "netlist_json": str(path),
        "cell_count": len(cells),
        "netname_count": len(netnames),
        "port_count": len(ports),
        "port_bit_count": sum(len(p.get("bits", [])) for p in ports.values()),
        "sequential_cell_count": class_counts["sequential"],
        "combinational_cell_count": class_counts["combinational"],
        "buffer_inverter_count": class_counts["buffer_inverter"],
        "other_cell_count": class_counts["other"],
        "unique_cell_type_count": len(cell_types),
        "avg_bit_fanout": mean(fanouts) if fanouts else None,
        "max_bit_fanout": max(fanouts) if fanouts else None,
        "std_bit_fanout": (
            pstdev(fanouts) if len(fanouts) > 1
            else 0.0 if fanouts
            else None
        ),
        "fanout_direction_metadata_found": direction_metadata_found,
        "fanout_method": (
            "Yosys cell input-port directions"
            if direction_metadata_found
            else "unavailable: cell port directions missing"
        ),
        "total_net_bits": net_bit_total,
        "unique_cell_types": dict(cell_types),
        "feature_stage": "post-synthesis / pre-placement",
        "cell_classification_method": "heuristic cell-name matching",
    }


# ---------------------------------------------------------------------
# Timing extraction
# ---------------------------------------------------------------------

def timing_patterns() -> dict[str, list[str]]:
    # Supports summary formats such as:
    #   worst slack max 1.57
    #   worst slack: 1.57
    #   wns max 1.57
    #   tns max 0.00
    #   worst slack min 0.47
    return {
        "wns_setup_ns": [
            rf"\bworst\s+slack\s+max\s*[:=]?\s*({NUM})",
            rf"\bwns\s+max\s*[:=]?\s*({NUM})",
            rf"\bworst\s+slack(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
            rf"\bwns(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
        ],
        "tns_setup_ns": [
            rf"\btotal\s+negative\s+slack\s*[:=]\s*({NUM})",
            rf"\btns\s+max\s*[:=]?\s*({NUM})",
            rf"\btns(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
            rf"\btotal\s+negative\s+slack(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
        ],
        "wns_hold_ns": [
            rf"\bworst\s+slack\s+min\s*[:=]?\s*({NUM})",
            rf"\bwns\s+min\s*[:=]?\s*({NUM})",
            rf"\bworst\s+slack(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
            rf"\bwns(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
        ],
    }


def parse_timing(text: str, metric: str) -> float | None:
    return last_number(text, timing_patterns()[metric])


# ---------------------------------------------------------------------
# DEF and routing-log extraction
# ---------------------------------------------------------------------

def parse_def_routing(def_text: str) -> dict[str, Any]:
    section = re.search(
        r"\bNETS\s+\d+\s*;(.*?)\bEND\s+NETS\b",
        def_text,
        re.I | re.S,
    )

    if not section:
        return {
            "def_nets_section_found": False,
            "routed_net_count": None,
            "route_segment_count": None,
        }

    entries = re.findall(
        r"(?ms)^[ \t]*-[ \t]+.*?;",
        section.group(1),
    )

    routed_nets = 0
    route_clauses = 0

    for entry in entries:
        clauses = re.findall(
            r"\b(?:ROUTED|FIXED|COVER|NEW)\s+"
            r"[A-Za-z0-9_.$/\[\]-]+",
            entry,
            re.I,
        )
        if clauses:
            routed_nets += 1

        route_clauses += len(re.findall(
            r"\b(?:ROUTED|NEW)\s+[A-Za-z0-9_.$/\[\]-]+",
            entry,
            re.I,
        ))

    return {
        "def_nets_section_found": True,
        "routed_net_count": routed_nets,
        "route_segment_count": route_clauses,
    }


def parse_route_log(text: str) -> dict[str, Any]:
    # Use the final occurrence because detailed routing logs can report
    # intermediate iterations before the final routing summary.
    wirelength = last_number(text, [
        rf"\bTotal\s+wire\s+length\s*[:=]\s*({NUM})\s*(?:um|µm)\b",
        rf"\bTotal\s+wire\s+length\s*[:=]\s*({NUM})",
    ])

    vias = last_number(text, [
        rf"\bTotal\s+number\s+of\s+vias\s*[:=]\s*({NUM})",
        rf"\bNumber\s+of\s+vias\s*[:=]\s*({NUM})",
    ])

    violations = last_number(text, [
        rf"\bNumber\s+of\s+violations\s*[:=]\s*({NUM})",
        rf"\bTotal\s+violations\s*[:=]\s*({NUM})",
    ])

    # OpenROAD sometimes reports the final summary as:
    # "Number of violations = 0". Do not assume a zero merely because
    # the log says detailed routing completed.
    return {
        "wirelength_um": wirelength,
        "via_count": int(vias) if vias is not None else None,
        "final_detailed_route_violations": (
            int(violations) if violations is not None else None
        ),
    }


def parse_congestion(text: str) -> float | None:
    value = last_number(text, [
        rf"\btotal\s+(?:routing\s+)?overflow\s*[:=]\s*({NUM})",
        rf"\bcongestion\s+overflow\s*[:=]\s*({NUM})",
        rf"\brouting\s+overflow\s*[:=]\s*({NUM})",
    ])
    if value is not None:
        return value

    if re.search(
        r"\b(?:no overflowing gcells|no overflowing gcell|"
        r"zero routing overflow)\b",
        text,
        re.I,
    ):
        return 0.0

    return None


def parse_physical_log_values(text: str) -> dict[str, Any]:
    # Only populate these fields when a recognizable label is present.
    effective_density = last_number(text, [
        rf"\buniform density\s+to\s+({NUM})",
        rf"\beffective\s+(?:placement\s+)?density\s*[:=]\s*({NUM})",
    ])

    design_area = last_number(text, [
        rf"\bmovable instances area\s*[:=]?\s*({NUM})",
        rf"\bdesign area\s*[:=]\s*({NUM})",
    ])

    return {
        "effective_place_density": effective_density,
        "design_area_um2": design_area,
    }


# ---------------------------------------------------------------------
# Physical target extraction
# ---------------------------------------------------------------------

def physical_features(run_dir: Path) -> dict[str, Any]:
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    def_path = run_dir / "final.def"
    odb_path = run_dir / "final.odb"
    def_text = read_text(def_path)

    setup_path, setup_text = first_existing_text(run_dir, [
        "wns_setup.rpt",
        "wns_setup_global_route.rpt",
        "timing_setup.rpt",
        "timing_setup_global_route.rpt",
        "setup_timing.rpt",
        "setup.rpt",
    ])

    tns_path, tns_text = first_existing_text(run_dir, [
        "tns_setup.rpt",
        "tns_setup_global_route.rpt",
        "timing_setup.rpt",
        "timing_setup_global_route.rpt",
        "setup_timing.rpt",
        "setup.rpt",
    ])

    hold_path, hold_text = first_existing_text(run_dir, [
        "wns_hold.rpt",
        "timing_hold.rpt",
        "wns_hold_global_route.rpt",
        "timing_hold_global_route.rpt",
        "hold_timing.rpt",
        "hold.rpt",
    ])

    congestion_path, congestion_text = first_existing_text(run_dir, [
        "congestion.rpt",
        "global_route_congestion.rpt",
        "routing_congestion.rpt",
        "grt_congestion.rpt",
    ])

    log_names = [
        "5_2_route.log",
        "detailed_route.log",
        "openroad_log.txt",
        "flow_diagnostics.log",
        "5_1_grt.log",
        "global_route.log",
    ]

    logs: dict[str, str] = {}
    for name in log_names:
        text = read_text(run_dir / name)
        if text.strip():
            logs[name] = text

    # Prefer a detailed-routing-specific log; otherwise use the full log.
    route_log_name = next(
        (name for name in (
            "5_2_route.log",
            "detailed_route.log",
            "openroad_log.txt",
            "flow_diagnostics.log",
        ) if name in logs),
        None,
    )
    route_text = logs.get(route_log_name, "")

    all_logs = "\n".join(logs.values())

    # If a dedicated report is absent, search the combined logs.
    setup_source = setup_text or all_logs
    tns_source = tns_text or all_logs
    hold_source = hold_text or all_logs
    congestion_source = congestion_text or all_logs

    wns_setup = parse_timing(setup_source, "wns_setup_ns")
    tns_setup = parse_timing(tns_source, "tns_setup_ns")
    wns_hold = parse_timing(hold_source, "wns_hold_ns")

    # Routing log metrics are preferred over unrelated placement summaries.
    route_metrics = parse_route_log(route_text)
    congestion_overflow = parse_congestion(congestion_source)
    physical_log_values = parse_physical_log_values(all_logs)

    def_counts = parse_def_routing(def_text) if def_text else {
        "def_nets_section_found": False,
        "routed_net_count": None,
        "route_segment_count": None,
    }

    detailed_route_success = bool(re.search(
        r"SUCCESS:\s*Detailed routing|Complete detail routing",
        route_text,
        re.I,
    ))

    artifacts_present = (
        def_path.is_file()
        and odb_path.is_file()
        and bool(def_text.strip())
    )

    return {
        "final_def_available": def_path.is_file(),
        "final_odb_available": odb_path.is_file(),
        "final_def_nonempty": bool(def_text.strip()),
        "artifacts_present": artifacts_present,
        "detailed_route_success_marker_found": detailed_route_success,
        **def_counts,
        **route_metrics,
        **physical_log_values,
        "wns_setup_ns": wns_setup,
        "tns_setup_ns": tns_setup,
        "wns_hold_ns": wns_hold,
        "congestion_overflow": congestion_overflow,
        "setup_report_source": (
            str(setup_path) if setup_path else (
                "logs" if setup_source else None
            )
        ),
        "tns_report_source": (
            str(tns_path) if tns_path else (
                "logs" if tns_source else None
            )
        ),
        "hold_report_source": (
            str(hold_path) if hold_path else (
                "logs" if hold_source else None
            )
        ),
        "congestion_report_source": (
            str(congestion_path) if congestion_path else (
                "logs" if congestion_source else None
            )
        ),
        "routing_log_source": route_log_name,
        "log_files_found": list(logs),
        "target_measurement_status": {
            "wns_setup_ns": "measured" if wns_setup is not None else "unavailable",
            "tns_setup_ns": "measured" if tns_setup is not None else "unavailable",
            "wns_hold_ns": "measured" if wns_hold is not None else "unavailable",
            "congestion_overflow": "measured" if congestion_overflow is not None else "unavailable",
            "wirelength_um": "measured" if route_metrics["wirelength_um"] is not None else "unavailable",
            "via_count": "measured" if route_metrics["via_count"] is not None else "unavailable",
            "final_detailed_route_violations": (
                "measured"
                if route_metrics["final_detailed_route_violations"] is not None
                else "unavailable"
            ),
            "design_area_um2": (
                "measured"
                if physical_log_values["design_area_um2"] is not None
                else "unavailable"
            ),
        },
        "note": (
            "DEF routing counts are approximate clause counts. "
            "Fanout is based on Yosys cell input directions when available. "
            "Routing log values use the last matching summary in the selected "
            "routing log. A zero router violation count is not independent "
            "signoff DRC verification. Congestion is null unless a recognized "
            "overflow metric is found."
        ),
        "target_feature_stage": (
            "post-placement/routing targets; exclude these outcomes from "
            "predictors when predicting the same outcomes"
        ),
    }


# ---------------------------------------------------------------------
# Configuration CSV
# ---------------------------------------------------------------------

def write_configuration_csv(
    outdir: Path,
    run_dir: Path,
    args: argparse.Namespace,
) -> None:
    """
    Populate fir_features.csv with a configuration row.

    Supply the design parameters explicitly on the command line.
    Unknown parameters remain blank instead of being guessed.
    """
    run_id = args.run_id or run_dir.name

    flow_status = args.flow_status or "unknown"
    last_stage = "unknown"

    logs = "\n".join(
        read_text(run_dir / name)
        for name in (
            "openroad_log.txt",
            "flow_diagnostics.log",
            "5_2_route.log",
            "detailed_route.log",
        )
    )

    if re.search(r"SUCCESS:\s*Detailed routing|Complete detail routing", logs, re.I):
        flow_status = args.flow_status or "detailed_routing_completed"
        last_stage = "detailed_routing"
    elif re.search(r"global placement", logs, re.I):
        last_stage = "global_placement"

    fields = [
        "run_id", "taps", "data_width", "coeff_width", "acc_width",
        "utilization", "place_density", "clk_period_ns",
        "flow_status", "last_completed_stage",
    ]

    row = {
        "run_id": run_id,
        "taps": args.taps,
        "data_width": args.data_width,
        "coeff_width": args.coeff_width,
        "acc_width": args.acc_width,
        "utilization": args.utilization,
        "place_density": args.place_density,
        "clk_period_ns": args.clock_period,
        "flow_status": flow_status,
        "last_completed_stage": last_stage,
    }

    write_rows_csv(outdir / "fir_features.csv", fields, row)


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract FIR netlist features and OpenROAD physical targets."
    )

    parser.add_argument(
        "--netlist-json",
        default="fir_project/synth/fir_filter_netlist.json",
    )
    parser.add_argument(
        "--run-dir",
        default="fir_project/runs/fir_8tap_w8_util40_clk10",
    )
    parser.add_argument("--outdir", default=None)
    parser.add_argument("--top", default="fir_filter")
    parser.add_argument("--design-id", default="fir_filter_8tap")
    parser.add_argument("--run-id", default=None)

    parser.add_argument("--taps", type=int, default=None)
    parser.add_argument("--data-width", type=int, default=None)
    parser.add_argument("--coeff-width", type=int, default=None)
    parser.add_argument("--acc-width", type=int, default=None)
    parser.add_argument("--utilization", type=float, default=None)
    parser.add_argument("--place-density", type=float, default=None)
    parser.add_argument("--clock-period", type=float, default=None)
    parser.add_argument("--flow-status", default=None)

    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    outdir = Path(args.outdir) if args.outdir else run_dir
    netlist_path = Path(args.netlist_json)

    if args.utilization is not None and not (0 < args.utilization <= 100):
        parser.error("--utilization must be > 0 and <= 100.")

    if args.place_density is not None and not (0 < args.place_density <= 1):
        parser.error("--place-density must be > 0 and <= 1.")

    if args.clock_period is not None and args.clock_period <= 0:
        parser.error("--clock-period must be positive.")

    for name in ("taps", "data_width", "coeff_width", "acc_width"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive.")

    try:
        outdir.mkdir(parents=True, exist_ok=True)

        pre = extract_netlist_features(
            netlist_path,
            args.top,
            args.design_id,
        )
        pre["utilization_constraint_percent"] = args.utilization
        pre["clock_period_ns"] = args.clock_period

        post = physical_features(run_dir)

        write_json(outdir / "preplacement_features.json", pre)
        write_csv(outdir / "preplacement_features.csv", pre)

        write_json(outdir / "physical_targets.json", post)
        write_csv(outdir / "physical_targets.csv", post)

        combined = {**pre, **post}
        write_json(outdir / "features.json", combined)
        write_csv(outdir / "features.csv", combined)

        write_configuration_csv(outdir, run_dir, args)

        status = {
            "design_id": args.design_id,
            "run_id": args.run_id or run_dir.name,
            "netlist_json": str(netlist_path),
            "run_dir": str(run_dir),
            "output_dir": str(outdir),
            "fanout_direction_metadata_found": pre["fanout_direction_metadata_found"],
            "final_def_available": post["final_def_available"],
            "final_odb_available": post["final_odb_available"],
            "artifacts_present": post["artifacts_present"],
            "detailed_route_success_marker_found": post["detailed_route_success_marker_found"],
            "target_measurement_status": post["target_measurement_status"],
        }
        write_json(outdir / "extraction_status.json", status)

    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("\n===== FIR Feature Extraction =====")
    print(f"Run directory : {run_dir}")
    print(f"Output directory: {outdir}")
    print(f"Cell count    : {pre['cell_count']}")
    print(f"Sequential    : {pre['sequential_cell_count']}")
    print(f"Combinational : {pre['combinational_cell_count']}")
    print(f"Fanout method : {pre['fanout_method']}")
    print(f"Final DEF     : {post['final_def_available']}")
    print(f"Final ODB     : {post['final_odb_available']}")
    print(f"Routing success marker: {post['detailed_route_success_marker_found']}")

    print("\n===== Physical Targets =====")
    for key in (
        "wns_setup_ns",
        "tns_setup_ns",
        "wns_hold_ns",
        "congestion_overflow",
        "wirelength_um",
        "via_count",
        "final_detailed_route_violations",
        "design_area_um2",
        "effective_place_density",
    ):
        print(f"{key}: {post.get(key)}")

    print("\n===== Target Status =====")
    for key, value in post["target_measurement_status"].items():
        print(f"{key}: {value}")

    print("\nGenerated: preplacement_features.csv, physical_targets.csv,")
    print("           features.csv, fir_features.csv, extraction_status.json")

    print("\nExtraction finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
