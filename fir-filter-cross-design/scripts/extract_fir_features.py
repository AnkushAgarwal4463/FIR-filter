
#!/usr/bin/env python3
"""
FIR netlist and physical-design feature extractor.

Supports the command-line interface used by run_fir_flow.sh:

    --netlist-json
    --outdir
    --top
    --design-id
    --run-id
    --taps
    --data-width
    --coeff-width
    --acc-width
    --utilization
    --place-density
    --clock-period

Also supports the legacy interface:
    --run-dir
    --output
    --config

Missing measurements remain blank. No physical metrics are fabricated.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any


FIELDS = [
    "run_id",
    "design_id",
    "top",
    "taps",
    "data_width",
    "coeff_width",
    "acc_width",
    "utilization",
    "place_density",
    "clock_period",
    "netlist_json",
    "netlist_json_found",
    "netlist_cell_count",
    "net_count",
    "netlist_port_count",
    "sequential_cell_count",
    "combinational_cell_count",
    "buffer_cell_count",
    "inverter_cell_count",
    "max_fanout",
    "mean_fanout",
    "final_def",
    "final_odb",
    "routed_net_count",
    "route_segment_count",
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
]


def number(value: Any) -> float | None:
    """Convert a value to a number when possible."""

    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", "")

    if not text or text.lower() in {
        "none", "null", "nan", "n/a", "unavailable"
    }:
        return None

    match = re.search(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?",
        text,
    )

    return float(match.group()) if match else None


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: Could not read JSON {path}: {exc}", file=sys.stderr)
        return {}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def find_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    found: set[Path] = set()

    if not root.is_dir():
        return []

    for pattern in patterns:
        found.update(root.rglob(pattern))

    return sorted(path for path in found if path.is_file())


def first_value(data: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    """Find a value using alternative field names."""

    for key in aliases:
        if key in data and data[key] not in (None, ""):
            return data[key]

    return None


def flatten_json(data: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested JSON dictionaries for flexible metric lookup."""

    result: dict[str, Any] = {}

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
    """Find a metric by exact key or final nested key."""

    wanted = {alias.lower() for alias in aliases}

    for key, value in flattened.items():
        final_key = key.rsplit(".", 1)[-1]

        if key in wanted or final_key in wanted:
            parsed = number(value)

            if parsed is not None:
                return parsed

    return None


def extract_netlist_json(
    path: Path | None,
) -> dict[str, Any]:
    """Extract metrics from a supplied netlist JSON file."""

    result: dict[str, Any] = {}

    if path is None or not path.is_file():
        return result

    data = read_json(path)

    if not data:
        return result

    flat = flatten_json(data)

    aliases = {
        "netlist_cell_count": (
            "netlist_cell_count", "cell_count", "num_cells",
            "total_cells", "cells_count",
        ),
        "net_count": (
            "net_count", "num_nets", "total_nets",
        ),
        "netlist_port_count": (
            "netlist_port_count", "port_count", "num_ports",
        ),
        "sequential_cell_count": (
            "sequential_cell_count", "sequential_cells",
            "num_sequential_cells", "flop_count",
        ),
        "combinational_cell_count": (
            "combinational_cell_count", "combinational_cells",
            "num_combinational_cells",
        ),
        "buffer_cell_count": (
            "buffer_cell_count", "buffer_count", "num_buffers",
        ),
        "inverter_cell_count": (
            "inverter_cell_count", "inverter_count", "num_inverters",
        ),
        "max_fanout": (
            "max_fanout", "maximum_fanout",
        ),
        "mean_fanout": (
            "mean_fanout", "average_fanout", "avg_fanout",
        ),
        "standard_cell_area_um2": (
            "standard_cell_area_um2", "cell_area_um2",
            "standard_cell_area", "design_area",
        ),
    }

    for output_key, keys in aliases.items():
        value = find_json_metric(flat, keys)

        if value is not None:
            result[output_key] = value

    # Count cells if the JSON contains a cell collection.
    if "netlist_cell_count" not in result:
        for key in ("cells", "instances", "cell_list"):
            value = data.get(key)

            if isinstance(value, (list, dict)):
                result["netlist_cell_count"] = len(value)
                break

    # Count ports if the JSON contains a port collection.
    if "netlist_port_count" not in result:
        for key in ("ports", "port_list"):
            value = data.get(key)

            if isinstance(value, (list, dict)):
                result["netlist_port_count"] = len(value)
                break

    return result


def extract_verilog_netlist(
    netlist_path: Path | None,
) -> dict[str, Any]:
    """Extract basic cell and port counts from synthesized Verilog."""

    result: dict[str, Any] = {}

    if netlist_path is None or not netlist_path.is_file():
        return result

    text = read_text(netlist_path)

    # Count module instances using common synthesized Verilog syntax.
    instance_pattern = re.compile(
        r"(?m)^\s*"
        r"([A-Za-z_$][\w$]*)"
        r"(?:\s*#\s*\(.*?\))?"
        r"\s+([A-Za-z_$][\w$]*)\s*"
        r"\([^;]*?\)\s*;",
        flags=re.DOTALL,
    )

    cell_types = []

    for match in instance_pattern.finditer(text):
        cell_type = match.group(1)

        if cell_type.lower() not in {
            "module", "endmodule", "assign", "input",
            "output", "inout", "wire", "reg",
        }:
            cell_types.append(cell_type)

    if cell_types:
        result["netlist_cell_count"] = len(cell_types)

        sequential = [
            cell for cell in cell_types
            if re.search(r"(dff|sdff|latch|dlatch|_df)", cell, re.I)
        ]

        buffers = [
            cell for cell in cell_types
            if re.search(r"buf", cell, re.I)
        ]

        inverters = [
            cell for cell in cell_types
            if re.search(r"inv", cell, re.I)
        ]

        result["sequential_cell_count"] = len(sequential)
        result["combinational_cell_count"] = (
            len(cell_types) - len(sequential)
        )
        result["buffer_cell_count"] = len(buffers)
        result["inverter_cell_count"] = len(inverters)

    # Count declared wire identifiers as a basic net-count estimate.
    wire_declarations = re.findall(
        r"(?m)^\s*wire(?:\s+[^;]*?)?\s+([^;]+);",
        text,
    )

    if wire_declarations:
        names = set()

        for declaration in wire_declarations:
            declaration = re.sub(r"\[[^\]]+\]", "", declaration)

            for item in declaration.split(","):
                name = re.sub(r"\s*=.*$", "", item).strip()

                if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                    names.add(name)

        result["net_count"] = len(names)

    module_match = re.search(
        r"\bmodule\s+\w+\s*\((.*?)\)\s*;",
        text,
        re.DOTALL,
    )

    if module_match:
        ports = [
            item.strip()
            for item in module_match.group(1).split(",")
            if item.strip()
        ]

        result["netlist_port_count"] = len(ports)

    return result


def extract_report_metrics(run_dir: Path) -> dict[str, Any]:
    """Extract physical-design and timing metrics from reports."""

    result: dict[str, Any] = {}

    report_files = find_files(
        run_dir,
        ("*.rpt", "*.report", "*.log", "*.txt", "*.json"),
    )

    all_text = "\n".join(
        read_text(path)
        for path in report_files
        if path.suffix.lower() != ".json"
    )

    patterns = {
        "wns_setup_ns": (
            r"wns(?:\s*\(setup\))?\s*[:=]\s*(-?[\d.]+)",
            r"worst\s+slack(?:\s*\(setup\))?\s*[:=]\s*(-?[\d.]+)",
        ),
        "tns_setup_ns": (
            r"tns(?:\s*\(setup\))?\s*[:=]\s*(-?[\d.]+)",
            r"total\s+negative\s+slack(?:\s*\(setup\))?\s*[:=]\s*(-?[\d.]+)",
        ),
        "wns_hold_ns": (
            r"wns\s*\(hold\)\s*[:=]\s*(-?[\d.]+)",
            r"worst\s+hold\s+slack\s*[:=]\s*(-?[\d.]+)",
        ),
        "tns_hold_ns": (
            r"tns\s*\(hold\)\s*[:=]\s*(-?[\d.]+)",
            r"total\s+hold\s+negative\s+slack\s*[:=]\s*(-?[\d.]+)",
        ),
        "wirelength_um": (
            r"(?:total\s+)?wirelength(?:\s*\(um\))?\s*[:=]\s*([\d.]+)",
            r"total\s+wire\s+length(?:\s*\(um\))?\s*[:=]\s*([\d.]+)",
        ),
        "via_count": (
            r"(?:total\s+)?vias?\s*[:=]\s*(\d+)",
        ),
        "final_drc_violations": (
            r"(?:final\s+)?(?:DRC\s+)?violations?\s*[:=]\s*(\d+)",
        ),
        "global_route_overflow": (
            r"(?:global\s+)?(?:route\s+)?overflow\s*[:=]\s*(-?[\d.]+)",
        ),
        "effective_density": (
            r"(?:effective|achieved|realized)\s+density\s*[:=]\s*([\d.]+)",
        ),
        "standard_cell_area_um2": (
            r"(?:design|standard.cell|total.cell)\s+area\s*[:=]\s*([\d.]+)",
        ),
        "max_fanout": (
            r"max(?:imum)?\s+fanout\s*[:=]\s*([\d.]+)",
        ),
        "mean_fanout": (
            r"(?:mean|average)\s+fanout\s*[:=]\s*([\d.]+)",
        ),
        "routed_net_count": (
            r"routed\s+nets?\s*[:=]\s*(\d+)",
        ),
        "route_segment_count": (
            r"(?:route\s+)?segments?\s*[:=]\s*(\d+)",
        ),
    }

    for field, field_patterns in patterns.items():
        for pattern in field_patterns:
            match = re.search(pattern, all_text, re.I | re.M)

            if match:
                value = number(match.group(1))

                if value is not None:
                    result[field] = value
                    break

    # Also inspect JSON report files for structured metrics.
    for path in report_files:
        if path.suffix.lower() != ".json":
            continue

        data = read_json(path)

        if not data:
            continue

        flat = flatten_json(data)

        aliases = {
            "wirelength_um": ("wirelength_um", "wire_length_um"),
            "via_count": ("via_count", "total_vias"),
            "final_drc_violations": ("final_drc_violations", "drc_violations"),
            "global_route_overflow": ("global_route_overflow", "route_overflow"),
            "effective_density": ("effective_density", "achieved_density"),
            "standard_cell_area_um2": ("standard_cell_area_um2", "cell_area_um2"),
            "wns_setup_ns": ("wns_setup_ns", "setup_wns"),
            "tns_setup_ns": ("tns_setup_ns", "setup_tns"),
            "wns_hold_ns": ("wns_hold_ns", "hold_wns"),
            "tns_hold_ns": ("tns_hold_ns", "hold_tns"),
        }

        for output_key, keys in aliases.items():
            if output_key not in result:
                value = find_json_metric(flat, keys)

                if value is not None:
                    result[output_key] = value

    return result


def extract_artifact_presence(run_dir: Path) -> dict[str, int]:
    """Check for final physical-design outputs."""

    return {
        "final_def": int(bool(find_files(run_dir, ("*.def",)))),
        "final_odb": int(bool(find_files(run_dir, ("*.odb",)))),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract FIR netlist and physical-design features."
    )

    # Current run_fir_flow.sh interface.
    parser.add_argument("--netlist-json", type=Path)
    parser.add_argument("--outdir", type=Path)
    parser.add_argument("--top")
    parser.add_argument("--design-id")
    parser.add_argument("--run-id")
    parser.add_argument("--taps", type=float)
    parser.add_argument("--data-width", type=float)
    parser.add_argument("--coeff-width", type=float)
    parser.add_argument("--acc-width", type=float)
    parser.add_argument("--utilization", type=float)
    parser.add_argument("--place-density", type=float)
    parser.add_argument("--clock-period", type=float)

    # Legacy interface.
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path)

    return parser


def main() -> int:
    args = build_parser().parse_args()

    run_dir = args.outdir or args.run_dir

    if run_dir is None:
        print(
            "ERROR: Supply --outdir or --run-dir.",
            file=sys.stderr,
        )
        return 2

    run_dir.mkdir(parents=True, exist_ok=True)

    run_id = args.run_id or run_dir.name
    output_path = args.output or (run_dir / "features.csv")

    row: dict[str, Any] = {
        "run_id": run_id,
        "design_id": args.design_id or "",
        "top": args.top or "",
        "taps": args.taps,
        "data_width": args.data_width,
        "coeff_width": args.coeff_width,
        "acc_width": args.acc_width,
        "utilization": args.utilization,
        "place_density": args.place_density,
        "clock_period": args.clock_period,
        "netlist_json": str(args.netlist_json) if args.netlist_json else "",
        "netlist_json_found": int(
            bool(args.netlist_json and args.netlist_json.is_file())
        ),
    }

    # Read optional config JSON and fill only missing configuration fields.
    config = {}

    if args.config:
        config.update(read_json(args.config))

    for key in (
        "taps",
        "data_width",
        "coeff_width",
        "acc_width",
        "utilization",
        "place_density",
        "clock_period",
    ):
        if row.get(key) is None:
            value = number(config.get(key))

            if value is not None:
                row[key] = value

    # Extract from the explicitly supplied JSON file.
    row.update(extract_netlist_json(args.netlist_json))

    # Look for a synthesized Verilog file in the run directory.
    verilog_files = find_files(
        run_dir,
        ("*.v", "*.vg", "*.sv"),
    )

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

    verilog_metrics = extract_verilog_netlist(netlist_verilog)

    # JSON metrics take priority; Verilog counts fill missing values.
    for key, value in verilog_metrics.items():
        row.setdefault(key, value)

    row.update(extract_report_metrics(run_dir))
    row.update(extract_artifact_presence(run_dir))

    # Keep all expected columns in a stable order.
    for field in FIELDS:
        row.setdefault(field, "")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerow(row)

    print("FEATURE EXTRACTION COMPLETED")
    print(f"Run ID: {run_id}")
    print(f"Run directory: {run_dir}")
    print(f"Netlist JSON found: {row['netlist_json_found']}")
    print(f"Final DEF found: {row['final_def']}")
    print(f"Final ODB found: {row['final_odb']}")
    print(f"Output: {output_path}")

    if row["final_def"] != 1 or row["final_odb"] != 1:
        print(
            "WARNING: One or more final physical-design outputs are missing.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
