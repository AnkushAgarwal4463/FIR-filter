
#!/usr/bin/env python3
"""
FIR netlist and physical-design feature extractor for OpenROAD / Sky130HD.

Generates:
    features.csv
    preplacement_features.csv
    physical_targets.csv
    fir_features.csv
    extraction_status.json
    run_report.md

The OpenROAD-generated fir_features_status.csv is read but never overwritten.

Unavailable measurements remain blank. No physical metrics are fabricated.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


FEATURE_FIELDS = [
    "run_id", "design_id", "top",
    "taps", "data_width", "coeff_width", "acc_width",
    "utilization", "place_density", "clock_period",
    "netlist_json", "netlist_json_found",
    "netlist_cell_count", "net_count", "netlist_port_count",
    "sequential_cell_count", "combinational_cell_count",
    "buffer_cell_count", "inverter_cell_count",
    "max_fanout", "mean_fanout",
    "final_def", "final_odb",
    "routed_net_count", "route_segment_count",
    "wirelength_um", "via_count", "final_drc_violations",
    "global_route_overflow", "effective_density",
    "standard_cell_area_um2",
    "wns_setup_ns", "tns_setup_ns",
    "wns_hold_ns", "tns_hold_ns",
]

PREPLACEMENT_FIELDS = [
    "run_id", "design_id", "top",
    "utilization", "utilization_constraint_percent",
    "place_density", "clock_period", "clock_period_ns",
    "netlist_json_found",
    "netlist_cell_count", "net_count", "netlist_port_count",
    "sequential_cell_count", "combinational_cell_count",
    "buffer_cell_count", "inverter_cell_count",
    "max_fanout", "mean_fanout",
]

PHYSICAL_FIELDS = [
    "run_id", "design_id", "top",
    "clock_period", "clock_period_ns",
    "utilization", "place_density",
    "final_def", "final_odb",
    "routed_net_count", "route_segment_count",
    "wirelength_um", "via_count",
    "final_drc_violations", "global_route_overflow",
    "global_route_has_congestion",
    "effective_density", "standard_cell_area_um2",
    "wns_setup_ns", "tns_setup_ns",
    "wns_hold_ns", "tns_hold_ns",
]

FIR_FIELDS = FEATURE_FIELDS.copy()


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", "")

    if not text or text.lower() in {
        "none", "null", "nan", "n/a", "na",
        "unavailable", "unknown",
    }:
        return None

    match = re.search(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?",
        text,
    )

    return float(match.group()) if match else None


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_json(path: Path) -> dict[str, Any]:
    if not path or not path.is_file():
        return {}

    try:
        data = json.loads(read_text(path))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: Could not read {path}: {exc}", file=sys.stderr)
        return {}


def find_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    if not root.is_dir():
        return []

    found = set()

    for pattern in patterns:
        found.update(root.rglob(pattern))

    return sorted(p for p in found if p.is_file())


def write_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerow({
            field: "" if row.get(field) is None else row.get(field, "")
            for field in fields
        })


def flatten_json(data: Any, prefix: str = "") -> dict[str, Any]:
    result = {}

    if isinstance(data, dict):
        for key, value in data.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            result[name.lower()] = value
            result.update(flatten_json(value, name))

    return result


def find_json_metric(
    flattened: dict[str, Any],
    aliases: tuple[str, ...],
) -> float | None:
    wanted = {alias.lower() for alias in aliases}

    for key, value in flattened.items():
        if key in wanted or key.rsplit(".", 1)[-1] in wanted:
            parsed = number(value)

            if parsed is not None:
                return parsed

    return None


def select_yosys_module(
    data: dict[str, Any],
    top: str,
) -> dict[str, Any]:
    modules = data.get("modules", {})

    if not isinstance(modules, dict) or not modules:
        return {}

    if top in modules and isinstance(modules[top], dict):
        return modules[top]

    # If the requested top is unavailable, do not silently select
    # an unrelated module when several modules are present.
    if len(modules) == 1:
        only_module = next(iter(modules.values()))
        return only_module if isinstance(only_module, dict) else {}

    return {}


def extract_yosys_json(
    path: Path | None,
    top: str,
) -> dict[str, Any]:
    """Extract netlist structure and fanout from Yosys JSON."""

    if path is None or not path.is_file():
        return {}

    data = read_json(path)

    if not data:
        return {}

    module = select_yosys_module(data, top)

    if not module:
        print(
            f"WARNING: Could not identify top module '{top}' in Yosys JSON.",
            file=sys.stderr,
        )
        return {}

    cells = module.get("cells", {})
    ports = module.get("ports", {})
    nets = module.get("netnames", {})

    if not isinstance(cells, dict):
        cells = {}

    if not isinstance(ports, dict):
        ports = {}

    if not isinstance(nets, dict):
        nets = {}

    result: dict[str, Any] = {
        "netlist_cell_count": len(cells),
        "netlist_port_count": len(ports),
        "net_count": len(nets),
    }

    sequential = 0
    buffers = 0
    inverters = 0

    # Count cells by their synthesized cell types.
    for cell in cells.values():
        if not isinstance(cell, dict):
            continue

        cell_type = str(cell.get("type", ""))

        if re.search(r"(dff|sdff|latch|dlatch|_df)", cell_type, re.I):
            sequential += 1

        if re.search(r"buf", cell_type, re.I):
            buffers += 1

        if re.search(r"inv", cell_type, re.I):
            inverters += 1

    result["sequential_cell_count"] = sequential
    result["combinational_cell_count"] = max(
        0, len(cells) - sequential
    )
    result["buffer_cell_count"] = buffers
    result["inverter_cell_count"] = inverters

    # Calculate fanout from actual Yosys cell connection directions.
    sink_counts: Counter = Counter()
    driver_bits = set()

    for cell in cells.values():
        if not isinstance(cell, dict):
            continue

        connections = cell.get("connections", {})
        directions = cell.get("port_directions", {})

        if not isinstance(connections, dict):
            continue

        if not isinstance(directions, dict):
            continue

        for port_name, bits in connections.items():
            direction = directions.get(port_name, "")

            if not isinstance(bits, list):
                continue

            for bit in bits:
                if not isinstance(bit, int):
                    continue

                if direction == "input":
                    sink_counts[bit] += 1
                elif direction == "output":
                    driver_bits.add(bit)

    # Only count nets with an identified cell driver.
    fanouts = [
        sink_counts[bit]
        for bit in driver_bits
        if sink_counts[bit] > 0
    ]

    if fanouts:
        result["max_fanout"] = max(fanouts)
        result["mean_fanout"] = sum(fanouts) / len(fanouts)

    return result


def extract_verilog_netlist(path: Path | None) -> dict[str, Any]:
    """Fallback for basic synthesized Verilog counts."""

    if path is None or not path.is_file():
        return {}

    text = read_text(path)
    if not text:
        return {}

    result: dict[str, Any] = {}

    # This is a fallback only. Yosys JSON is preferred for cell counts.
    instance_pattern = re.compile(
        r"(?m)^\s*"
        r"([A-Za-z_$][\w$]*)"
        r"(?:\s*#\s*\(.*?\))?"
        r"\s+([A-Za-z_$][\w$]*)\s*"
        r"\([^;]*?\)\s*;",
        re.DOTALL,
    )

    excluded = {
        "module", "endmodule", "assign", "input",
        "output", "inout", "wire", "reg", "logic",
    }

    cell_types = [
        match.group(1)
        for match in instance_pattern.finditer(text)
        if match.group(1).lower() not in excluded
    ]

    if cell_types:
        sequential = sum(
            bool(re.search(r"(dff|sdff|latch|dlatch|_df)", c, re.I))
            for c in cell_types
        )

        result["netlist_cell_count"] = len(cell_types)
        result["sequential_cell_count"] = sequential
        result["combinational_cell_count"] = len(cell_types) - sequential
        result["buffer_cell_count"] = sum(
            bool(re.search(r"buf", c, re.I)) for c in cell_types
        )
        result["inverter_cell_count"] = sum(
            bool(re.search(r"inv", c, re.I)) for c in cell_types
        )

    return result


def extract_report_value(
    text: str,
    patterns: tuple[str, ...],
) -> float | None:
    for pattern in patterns:
        matches = re.findall(pattern, text, re.I | re.M)

        if matches:
            parsed = number(matches[-1])

            if parsed is not None:
                return parsed

    return None


def extract_def_metrics(def_path: Path) -> dict[str, Any]:
    """Extract counts available directly in the final DEF."""

    if not def_path.is_file():
        return {}

    text = read_text(def_path)
    if not text:
        return {}

    result: dict[str, Any] = {}

    patterns = {
        "routed_net_count": (
            r"(?m)^\s*NETS\s+(\d+)\s*;",
        ),
        "via_count": (
            r"(?m)^\s*VIAS\s+(\d+)\s*;",
        ),
    }

    for field, field_patterns in patterns.items():
        value = extract_report_value(text, field_patterns)

        if value is not None:
            result[field] = value

    # Count routed path declarations in DEF net sections.
    # This is a route-segment count, not a wirelength measurement.
    result["route_segment_count"] = len(
        re.findall(r"\+\s*(?:ROUTED|NEW)\s+", text, re.I)
    )

    return result


def extract_report_metrics(run_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def report(name: str) -> str:
        return read_text(run_dir / name)

    # Dedicated OpenROAD timing reports.
    setup_wns = report("wns_setup.rpt")
    hold_wns = report("wns_hold.rpt")
    setup_tns = report("tns_setup.rpt")

    result["wns_setup_ns"] = extract_report_value(
        setup_wns,
        (
            r"worst\s+slack\s*[:=]?\s*(-?[\d.eE+]+)",
            r"wns(?:\s*\(setup\))?\s*[:=]\s*(-?[\d.eE+]+)",
        ),
    )

    result["wns_hold_ns"] = extract_report_value(
        hold_wns,
        (
            r"worst\s+slack\s*[:=]?\s*(-?[\d.eE+]+)",
            r"wns(?:\s*\(hold\))?\s*[:=]\s*(-?[\d.eE+]+)",
        ),
    )

    result["tns_setup_ns"] = extract_report_value(
        setup_tns,
        (
            r"\btns\b\s*[:=]?\s*(-?[\d.eE+]+)",
            r"total\s+negative\s+slack\s*[:=]?\s*(-?[\d.eE+]+)",
        ),
    )

    # Some OpenROAD builds expose TNS in the general timing report.
    if result["tns_setup_ns"] is None:
        result["tns_setup_ns"] = extract_report_value(
            report("timing_setup.rpt"),
            (
                r"\btns\b\s*[:=]?\s*(-?[\d.eE+]+)",
                r"total\s+negative\s+slack\s*[:=]?\s*(-?[\d.eE+]+)",
            ),
        )

    # Area report.
    area_text = report("design_area.rpt")

    result["standard_cell_area_um2"] = extract_report_value(
        area_text,
        (
            r"(?:design|standard.cell|total.cell)\s+area\s*[:=]?\s*([\d.eE+]+)",
            r"area\s*[:=]\s*([\d.eE+]+)\s*(?:um|µm)",
        ),
    )

    # Congestion reports.
    congestion_text = report("congestion.rpt")
    overflow_text = report("congestion_overflow.rpt")
    diagnostics_text = report("flow_diagnostics.log")

    result["global_route_overflow"] = extract_report_value(
        congestion_text + "\n" + overflow_text,
        (
            r"(?:total\s+)?overflow\s*[:=]\s*(-?[\d.eE+]+)",
            r"overflow\s*[:=]\s*(-?[\d.eE+]+)",
        ),
    )

    congestion_match = re.search(
        r"GLOBAL_ROUTE_HAS_CONGESTION:\s*(true|false|unavailable)",
        diagnostics_text + "\n" + overflow_text,
        re.I,
    )

    if congestion_match:
        status = congestion_match.group(1).lower()

        if status == "true":
            result["global_route_has_congestion"] = 1
        elif status == "false":
            result["global_route_has_congestion"] = 0
        else:
            result["global_route_has_congestion"] = ""

    # Look for additional structured metrics in JSON reports.
    for path in find_files(run_dir, ("*.json",)):
        data = read_json(path)

        if not data:
            continue

        flat = flatten_json(data)

        aliases = {
            "wirelength_um": ("wirelength_um", "wire_length_um"),
            "via_count": ("via_count", "total_vias"),
            "final_drc_violations": (
                "final_drc_violations", "drc_violations"
            ),
            "global_route_overflow": (
                "global_route_overflow", "route_overflow"
            ),
            "effective_density": (
                "effective_density", "achieved_density"
            ),
            "standard_cell_area_um2": (
                "standard_cell_area_um2", "cell_area_um2"
            ),
            "wns_setup_ns": ("wns_setup_ns", "setup_wns"),
            "tns_setup_ns": ("tns_setup_ns", "setup_tns"),
            "wns_hold_ns": ("wns_hold_ns", "hold_wns"),
            "tns_hold_ns": ("tns_hold_ns", "hold_tns"),
        }

        for output_key, keys in aliases.items():
            if result.get(output_key) in (None, ""):
                value = find_json_metric(flat, keys)

                if value is not None:
                    result[output_key] = value

    return result


def read_existing_status(run_dir: Path) -> dict[str, Any]:
    """Read the Tcl-generated status file without modifying it."""

    path = run_dir / "fir_features_status.csv"

    if not path.is_file():
        return {
            "file_found": False,
            "last_status": "",
            "last_completed_stage": "",
            "entries": 0,
        }

    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))

        last = rows[-1] if rows else {}

        return {
            "file_found": True,
            "last_status": last.get("status", ""),
            "last_completed_stage": last.get(
                "last_completed_stage", ""
            ),
            "entries": len(rows),
        }

    except (OSError, csv.Error) as exc:
        return {
            "file_found": True,
            "read_error": str(exc),
            "last_status": "",
            "last_completed_stage": "",
            "entries": 0,
        }


def build_report(
    path: Path,
    row: dict[str, Any],
    status: dict[str, Any],
    netlist_path: Path | None,
) -> None:
    def display(key: str, unit: str = "") -> str:
        value = row.get(key)

        if value in (None, ""):
            return "Unavailable"

        return f"{value}{unit}"

    lines = [
        f"# FIR Physical-Design Run Report",
        "",
        "## Run configuration",
        "",
        f"- Run ID: `{row['run_id']}`",
        f"- Design ID: `{row['design_id']}`",
        f"- Top module: `{row['top']}`",
        f"- Taps: {display('taps')}",
        f"- Data width: {display('data_width')}",
        f"- Coefficient width: {display('coeff_width')}",
        f"- Accumulator width: {display('acc_width')}",
        f"- Utilization constraint: {display('utilization', '%')}",
        f"- Placement density: {display('place_density')}",
        f"- Clock period: {display('clock_period', ' ns')}",
        "",
        "## Netlist features",
        "",
        f"- Netlist JSON: {'Found' if row['netlist_json_found'] else 'Not found'}",
        f"- Netlist cells: {display('netlist_cell_count')}",
        f"- Nets: {display('net_count')}",
        f"- Ports: {display('netlist_port_count')}",
        f"- Sequential cells: {display('sequential_cell_count')}",
        f"- Combinational cells: {display('combinational_cell_count')}",
        f"- Maximum fanout: {display('max_fanout')}",
        f"- Mean fanout: {display('mean_fanout')}",
        "",
        "## Physical-design results",
        "",
        f"- Final DEF: {'Found' if row['final_def'] else 'Missing'}",
        f"- Final ODB: {'Found' if row['final_odb'] else 'Missing'}",
        f"- Routed nets: {display('routed_net_count')}",
        f"- Route segments: {display('route_segment_count')}",
        f"- Wirelength: {display('wirelength_um', ' um')}",
        f"- Via count: {display('via_count')}",
        f"- Global-route overflow: {display('global_route_overflow')}",
        f"- Effective density: {display('effective_density')}",
        f"- Standard-cell area: {display('standard_cell_area_um2', ' um^2')}",
        "",
        "## Timing results",
        "",
        f"- Setup WNS: {display('wns_setup_ns', ' ns')}",
        f"- Setup TNS: {display('tns_setup_ns', ' ns')}",
        f"- Hold WNS: {display('wns_hold_ns', ' ns')}",
        f"- Hold TNS: {display('tns_hold_ns', ' ns')}",
        "",
        "## Flow status",
        "",
        f"- Status file found: {status.get('file_found')}",
        f"- Last flow status: `{status.get('last_status', '')}`",
        f"- Last completed stage: `{status.get('last_completed_stage', '')}`",
        f"- Status entries: {status.get('entries', 0)}",
        "",
        "## Extraction notes",
        "",
        f"- Netlist JSON path: `{netlist_path or 'Not supplied'}`",
        "- Unavailable metrics are left blank in CSV files.",
        "- DEF route-segment count is a count of ROUTED/NEW declarations.",
        "- Wirelength is not inferred from route-segment count.",
        "- The Tcl-generated fir_features_status.csv is read-only to this extractor.",
        "",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract FIR netlist and physical-design features."
    )

    parser.add_argument("--netlist-json", type=Path)
    parser.add_argument("--outdir", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path)

    parser.add_argument("--top", default="fir_filter")
    parser.add_argument("--design-id", default="")
    parser.add_argument("--run-id", default="")

    parser.add_argument("--taps", type=float)
    parser.add_argument("--data-width", type=float)
    parser.add_argument("--coeff-width", type=float)
    parser.add_argument("--acc-width", type=float)
    parser.add_argument("--utilization", type=float)
    parser.add_argument("--place-density", type=float)
    parser.add_argument("--clock-period", type=float)

    return parser


def main() -> int:
    args = build_parser().parse_args()

    run_dir = args.outdir or args.run_dir

    if run_dir is None:
        print("ERROR: Supply --outdir or --run-dir.", file=sys.stderr)
        return 2

    run_dir.mkdir(parents=True, exist_ok=True)

    run_id = args.run_id or run_dir.name

    config = read_json(args.config) if args.config else {}

    row: dict[str, Any] = {
        "run_id": run_id,
        "design_id": args.design_id or "",
        "top": args.top or "fir_filter",
        "taps": args.taps,
        "data_width": args.data_width,
        "coeff_width": args.coeff_width,
        "acc_width": args.acc_width,
        "utilization": args.utilization,
        "place_density": args.place_density,
        "clock_period": args.clock_period,
        "netlist_json": (
            str(args.netlist_json) if args.netlist_json else ""
        ),
        "netlist_json_found": int(
            bool(args.netlist_json and args.netlist_json.is_file())
        ),
    }

    # Fill missing configuration from optional JSON.
    for key in (
        "taps", "data_width", "coeff_width", "acc_width",
        "utilization", "place_density", "clock_period",
    ):
        if row.get(key) is None:
            row[key] = number(config.get(key))

    # Netlist extraction: Yosys JSON first, Verilog fallback second.
    json_metrics = extract_yosys_json(
        args.netlist_json,
        row["top"],
    )

    for key, value in json_metrics.items():
        row[key] = value

    verilog_files = find_files(run_dir, ("*.v", "*.vg", "*.sv"))

    verilog_files = [
        path for path in verilog_files
        if not any(
            part.lower() in {"rtl", "tb", "testbench"}
            for part in path.parts
        )
    ]

    netlist_verilog = (
        max(verilog_files, key=lambda p: p.stat().st_size)
        if verilog_files else None
    )

    for key, value in extract_verilog_netlist(netlist_verilog).items():
        if row.get(key) in (None, ""):
            row[key] = value

    # Physical artifacts.
    def_path = run_dir / "final.def"
    odb_path = run_dir / "final.odb"

    row["final_def"] = int(def_path.is_file() and def_path.stat().st_size > 0)
    row["final_odb"] = int(odb_path.is_file() and odb_path.stat().st_size > 0)

    # DEF-derived metrics only fill unavailable report values.
    for key, value in extract_def_metrics(def_path).items():
        if row.get(key) in (None, ""):
            row[key] = value

    # Report-derived metrics.
    for key, value in extract_report_metrics(run_dir).items():
        if row.get(key) in (None, ""):
            row[key] = value

    # Read the Tcl-generated status file. Never overwrite it.
    status = read_existing_status(run_dir)

    # Ensure stable output fields.
    for field in FEATURE_FIELDS:
        row.setdefault(field, "")

    # Convert utilization into the explicitly named percentage field.
    preplacement = {
        key: row.get(key, "")
        for key in PREPLACEMENT_FIELDS
    }

    if row.get("utilization") not in (None, ""):
        preplacement["utilization_constraint_percent"] = row["utilization"]

    if row.get("clock_period") not in (None, ""):
        preplacement["clock_period_ns"] = row["clock_period"]

    physical = {
        key: row.get(key, "")
        for key in PHYSICAL_FIELDS
    }

    # Generate all required CSV outputs.
    output_files = {
        "features.csv": FEATURE_FIELDS,
        "preplacement_features.csv": PREPLACEMENT_FIELDS,
        "physical_targets.csv": PHYSICAL_FIELDS,
        "fir_features.csv": FIR_FIELDS,
    }

    for filename, fields in output_files.items():
        write_csv(run_dir / filename, fields, row)

    # Generate extraction status JSON.
    artifact_status = {
        "netlist_json": bool(
            args.netlist_json and args.netlist_json.is_file()
        ),
        "final_def": bool(row["final_def"]),
        "final_odb": bool(row["final_odb"]),
        "fir_features_status_csv": status.get("file_found", False),
    }

    missing_reports = [
        name for name in (
            "timing_setup.rpt",
            "timing_hold.rpt",
            "wns_setup.rpt",
            "wns_hold.rpt",
            "tns_setup.rpt",
            "design_area.rpt",
            "congestion.rpt",
            "congestion_overflow.rpt",
        )
        if not (run_dir / name).is_file()
    ]

    extraction_status = {
        "run_id": run_id,
        "design_id": row["design_id"],
        "top": row["top"],
        "extraction_completed": True,
        "artifact_status": artifact_status,
        "openroad_flow_status": status,
        "missing_optional_reports": missing_reports,
        "generated_files": list(output_files.keys()) + [
            "extraction_status.json",
            "run_report.md",
        ],
        "unavailable_metrics": [
            field for field in (
                "wirelength_um",
                "via_count",
                "final_drc_violations",
                "global_route_overflow",
                "effective_density",
                "standard_cell_area_um2",
                "wns_setup_ns",
                "tns_setup_ns",
                "wns_hold_ns",
                "tns_hold_ns",
            )
            if row.get(field) in (None, "")
        ],
        "note": (
            "Unavailable metrics remain blank; no physical values "
            "are fabricated."
        ),
    }

    status_path = run_dir / "extraction_status.json"
    status_path.write_text(
        json.dumps(extraction_status, indent=2),
        encoding="utf-8",
    )

    # Generate human-readable report.
    build_report(
        run_dir / "run_report.md",
        row,
        status,
        args.netlist_json,
    )

    print("FEATURE EXTRACTION COMPLETED")
    print(f"Run ID: {run_id}")
    print(f"Run directory: {run_dir}")
    print(f"Netlist JSON found: {row['netlist_json_found']}")
    print(f"Final DEF found: {row['final_def']}")
    print(f"Final ODB found: {row['final_odb']}")

    for filename in output_files:
        path = run_dir / filename
        print(f"Generated: {path.name} ({path.stat().st_size} bytes)")

    print("Generated: extraction_status.json")
    print("Generated: run_report.md")

    if not row["final_def"] or not row["final_odb"]:
        print(
            "WARNING: One or more final physical artifacts are missing.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
