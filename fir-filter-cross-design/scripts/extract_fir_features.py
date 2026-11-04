
#!/usr/bin/env python3
"""
Extract FIR netlist and physical-design features into features.csv.

Usage:
    python3 scripts/extract_fir_features.py \
        --run-dir fir_project/runs/fir_8tap_w8_util40_clk10 \
        --output fir_project/runs/fir_8tap_w8_util40_clk10/features.csv

The extractor searches the run directory for:
- Synthesis netlists
- OpenROAD reports
- DEF and ODB files
- Global-placement and routing reports
- Configuration and status JSON files

Unavailable values are written as empty CSV fields, not fabricated values.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any


FEATURE_FIELDS = [
    "run_id",
    "taps",
    "data_width",
    "coeff_width",
    "acc_width",
    "utilization",
    "place_density",
    "clock_period",
    "netlist_cell_count",
    "net_count",
    "netlist_port_count",
    "sequential_cell_count",
    "combinational_cell_count",
    "buffer_cell_count",
    "inverter_cell_count",
    "estimated_logic_depth",
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


def read_text(path: Path) -> str:
    """Read a text file safely."""

    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"WARNING: Cannot read {path}: {exc}", file=sys.stderr)
        return ""


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object safely."""

    try:
        data = json.loads(read_text(path))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def find_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    """Find files recursively, avoiding duplicate paths."""

    found: set[Path] = set()

    for pattern in patterns:
        found.update(root.rglob(pattern))

    return sorted(path for path in found if path.is_file())


def parse_number(value: Any) -> float | None:
    """Convert a numeric value or numeric string to float."""

    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", "")

    if not text or text.lower() in {"null", "none", "n/a", "nan", "unavailable"}:
        return None

    match = re.search(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?",
        text,
    )

    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def first_number(text: str, patterns: tuple[str, ...]) -> float | None:
    """Extract the first matching number from text."""

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)

        if match:
            value = parse_number(match.group(1))
            if value is not None:
                return value

    return None


def parse_run_id(run_dir: Path) -> dict[str, Any]:
    """Parse configuration parameters from a run-directory name."""

    name = run_dir.name

    patterns = {
        "taps": r"(?:^|[_-])t(?:aps)?(\d+)",
        "data_width": r"(?:^|[_-])dw(\d+)",
        "coeff_width": r"(?:^|[_-])cw(\d+)",
        "acc_width": r"(?:^|[_-])aw(\d+)",
        "utilization": r"(?:^|[_-])u(\d+)",
        "clock_period": r"(?:^|[_-])clk(\d+(?:\.\d+)?)",
    }

    result: dict[str, Any] = {"run_id": name}

    for key, pattern in patterns.items():
        match = re.search(pattern, name, flags=re.IGNORECASE)

        if match:
            number = parse_number(match.group(1))
            if number is not None:
                result[key] = (
                    int(number) if number.is_integer() and key != "clock_period"
                    else number
                )

    return result


def load_config(run_dir: Path) -> dict[str, Any]:
    """Load parameters from JSON configuration files when available."""

    config: dict[str, Any] = {}

    for path in find_files(run_dir, ("*.json",)):
        data = read_json(path)

        # Only import known configuration fields, not arbitrary nested data.
        aliases = {
            "taps": ("taps", "TAPS", "num_taps"),
            "data_width": ("data_width", "DATA_WIDTH"),
            "coeff_width": ("coeff_width", "COEFF_WIDTH"),
            "acc_width": ("acc_width", "ACC_WIDTH"),
            "utilization": ("utilization", "UTILIZATION", "target_utilization"),
            "place_density": ("place_density", "density", "PLACE_DENSITY"),
            "clock_period": ("clock_period", "clk_period", "CLOCK_PERIOD"),
        }

        for output_key, keys in aliases.items():
            for key in keys:
                value = parse_number(data.get(key))
                if value is not None:
                    config.setdefault(output_key, value)
                    break

    return config


def find_netlist(run_dir: Path) -> Path | None:
    """Find a likely synthesized Verilog netlist."""

    candidates = find_files(
        run_dir,
        ("*.v", "*.vg", "*.sv"),
    )

    if not candidates:
        return None

    # Prefer files whose names suggest a synthesized or final netlist.
    preferred = [
        path
        for path in candidates
        if any(
            token in path.name.lower()
            for token in ("netlist", "synth", "final", "1_2_yosys")
        )
    ]

    # Exclude testbenches and RTL sources where possible.
    preferred = [
        path
        for path in preferred
        if not any(
            token in str(path).lower()
            for token in ("/tb/", "/testbench/", "/rtl/")
        )
    ]

    if preferred:
        return max(preferred, key=lambda path: path.stat().st_size)

    remaining = [
        path
        for path in candidates
        if not any(
            token in str(path).lower()
            for token in ("/tb/", "/testbench/", "/rtl/")
        )
    ]

    return max(remaining, key=lambda path: path.stat().st_size) if remaining else None


def extract_netlist_features(
    netlist_path: Path | None,
) -> dict[str, Any]:
    """Extract approximate structural features from synthesized Verilog."""

    result: dict[str, Any] = {}

    if netlist_path is None:
        return result

    text = read_text(netlist_path)

    # Count module instances by matching cell instance declarations.
    # These are estimates from synthesized Verilog, not library area reports.
    instance_pattern = re.compile(
        r"(?m)^\s*"
        r"([A-Za-z_$][\w$]*(?:\s*#\s*\([^;]*?\))?)"
        r"\s+([A-Za-z_$][\w$]*)\s*"
        r"\([^;]*?\)\s*;",
        flags=re.DOTALL,
    )

    instances: list[str] = []

    for match in instance_pattern.finditer(text):
        cell_type = match.group(1).split("#", 1)[0].strip()

        if cell_type.lower() in {"module", "endmodule", "assign"}:
            continue

        instances.append(cell_type)

    if instances:
        result["netlist_cell_count"] = len(instances)

        sequential = [
            cell
            for cell in instances
            if re.search(r"(?:^|[_$])(df|dff|sdff|ff|latch|dlatch)", cell, re.I)
        ]

        buffers = [
            cell for cell in instances
            if re.search(r"(?:^|[_$])buf", cell, re.I)
        ]

        inverters = [
            cell for cell in instances
            if re.search(r"(?:^|[_$])inv", cell, re.I)
        ]

        result["sequential_cell_count"] = len(sequential)
        result["buffer_cell_count"] = len(buffers)
        result["inverter_cell_count"] = len(inverters)
        result["combinational_cell_count"] = len(instances) - len(sequential)

    # Approximate net count from declared wire identifiers.
    wire_names = re.findall(
        r"(?m)^\s*wire(?:\s+[^;]*?)?\s+([^;]+);",
        text,
    )

    net_count = 0

    for declaration in wire_names:
        declaration = re.sub(r"\[[^\]]+\]", "", declaration)

        for name in declaration.split(","):
            name = re.sub(r"\s*=.*$", "", name).strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                net_count += 1

    if wire_names:
        result["net_count"] = net_count

    # Count top-level ports from the first module declaration.
    module_match = re.search(
        r"\bmodule\s+\w+\s*\((.*?)\)\s*;",
        text,
        flags=re.DOTALL,
    )

    if module_match:
        ports = [
            token.strip()
            for token in module_match.group(1).split(",")
            if token.strip()
        ]
        result["netlist_port_count"] = len(ports)

    # Fanout is not reliably available from plain Verilog text without
    # parsing connection expressions. Obtain it from reports when possible.

    return result


def parse_timing_reports(run_dir: Path) -> dict[str, Any]:
    """Parse timing values from text reports when recognizable."""

    result: dict[str, Any] = {}

    report_files = find_files(
        run_dir,
        ("*.rpt", "*.report", "*.txt", "*.log"),
    )

    combined_text = "\n".join(read_text(path) for path in report_files)

    patterns = {
        "wns_setup_ns": (
            r"wns(?:\s*\(setup\))?\s*[:=]\s*(-?\d+(?:\.\d+)?)",
            r"worst\s+slack(?:\s*\(setup\))?\s*[:=]\s*(-?\d+(?:\.\d+)?)",
        ),
        "tns_setup_ns": (
            r"tns(?:\s*\(setup\))?\s*[:=]\s*(-?\d+(?:\.\d+)?)",
            r"total\s+negative\s+slack(?:\s*\(setup\))?\s*[:=]\s*(-?\d+(?:\.\d+)?)",
        ),
        "wns_hold_ns": (
            r"wns(?:\s*\(hold\))?\s*[:=]\s*(-?\d+(?:\.\d+)?)",
            r"worst\s+hold\s+slack\s*[:=]\s*(-?\d+(?:\.\d+)?)",
        ),
        "tns_hold_ns": (
            r"tns(?:\s*\(hold\))?\s*[:=]\s*(-?\d+(?:\.\d+)?)",
            r"total\s+hold\s+negative\s+slack\s*[:=]\s*(-?\d+(?:\.\d+)?)",
        ),
    }

    for field, field_patterns in patterns.items():
        value = first_number(combined_text, field_patterns)
        if value is not None:
            result[field] = value

    return result


def parse_physical_reports(run_dir: Path) -> dict[str, Any]:
    """Extract physical metrics from reports when their format is recognizable."""

    result: dict[str, Any] = {}

    report_files = find_files(
        run_dir,
        ("*.rpt", "*.report", "*.txt", "*.log", "*.json"),
    )

    for path in report_files:
        if path.suffix.lower() == ".json":
            data = read_json(path)

            aliases = {
                "max_fanout": ("max_fanout", "maximum_fanout"),
                "mean_fanout": ("mean_fanout", "average_fanout"),
                "wirelength_um": ("wirelength_um", "wire_length_um", "total_wirelength"),
                "via_count": ("via_count", "total_vias"),
                "final_drc_violations": ("final_drc_violations", "drc_violations"),
                "global_route_overflow": ("global_route_overflow", "route_overflow"),
                "effective_density": ("effective_density", "achieved_density"),
                "standard_cell_area_um2": ("standard_cell_area_um2", "cell_area_um2"),
                "routed_net_count": ("routed_net_count",),
                "route_segment_count": ("route_segment_count",),
            }

            for output_key, keys in aliases.items():
                for key in keys:
                    value = parse_number(data.get(key))
                    if value is not None:
                        result.setdefault(output_key, value)
                        break

            continue

        text = read_text(path)

        patterns = {
            "max_fanout": (
                r"max(?:imum)?\s+fanout\s*[:=]\s*(\d+(?:\.\d+)?)",
            ),
            "mean_fanout": (
                r"(?:mean|average)\s+fanout\s*[:=]\s*(\d+(?:\.\d+)?)",
            ),
            "wirelength_um": (
                r"(?:total\s+)?wirelength(?:\s*\(um\))?\s*[:=]\s*(\d+(?:\.\d+)?)",
                r"total\s+wire\s+length(?:\s*\(um\))?\s*[:=]\s*(\d+(?:\.\d+)?)",
            ),
            "via_count": (
                r"(?:total\s+)?vias?\s*[:=]\s*(\d+)",
            ),
            "final_drc_violations": (
                r"(?:final\s+)?(?:DRC\s+)?violations?\s*[:=]\s*(\d+)",
            ),
            "global_route_overflow": (
                r"(?:global\s+)?(?:route\s+)?overflow\s*[:=]\s*(-?\d+(?:\.\d+)?)",
            ),
            "effective_density": (
                r"(?:effective|achieved|realized)\s+density\s*[:=]\s*(\d+(?:\.\d+)?)",
            ),
            "standard_cell_area_um2": (
                r"(?:standard.cell|design|total.cell)\s+area(?:\s*\(um(?:\^?2|²)\))?\s*[:=]\s*(\d+(?:\.\d+)?)",
            ),
            "routed_net_count": (
                r"routed\s+nets?\s*[:=]\s*(\d+)",
            ),
            "route_segment_count": (
                r"(?:route\s+)?segments?\s*[:=]\s*(\d+)",
            ),
        }

        for field, field_patterns in patterns.items():
            if field in result:
                continue

            value = first_number(text, field_patterns)
            if value is not None:
                result[field] = value

    return result


def extract_artifact_presence(run_dir: Path) -> dict[str, Any]:
    """Record whether final DEF and ODB artifacts exist."""

    def_files = find_files(run_dir, ("*.def",))
    odb_files = find_files(run_dir, ("*.odb",))

    return {
        "final_def": int(bool(def_files)),
        "final_odb": int(bool(odb_files)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract FIR netlist and physical-design features."
    )

    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Directory containing this configuration's run outputs.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output CSV path (default: <run-dir>/features.csv).",
    )

    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional JSON file containing configuration parameters.",
    )

    args = parser.parse_args()

    run_dir = args.run_dir

    if not run_dir.is_dir():
        print(f"ERROR: Run directory does not exist: {run_dir}", file=sys.stderr)
        return 1

    output_path = args.output or (run_dir / "features.csv")

    # Populate fields from run ID, then fill missing configuration fields
    # from discovered JSON files and the explicitly supplied config.
    row: dict[str, Any] = parse_run_id(run_dir)
    discovered_config = load_config(run_dir)

    if args.config:
        if not args.config.is_file():
            print(f"ERROR: Config file does not exist: {args.config}", file=sys.stderr)
            return 1

        discovered_config.update(read_json(args.config))

    config_aliases = {
        "taps": ("taps", "TAPS", "num_taps"),
        "data_width": ("data_width", "DATA_WIDTH"),
        "coeff_width": ("coeff_width", "COEFF_WIDTH"),
        "acc_width": ("acc_width", "ACC_WIDTH"),
        "utilization": ("utilization", "UTILIZATION", "target_utilization"),
        "place_density": ("place_density", "density", "PLACE_DENSITY"),
        "clock_period": ("clock_period", "clk_period", "CLOCK_PERIOD"),
    }

    for output_key, aliases in config_aliases.items():
        if row.get(output_key) not in (None, ""):
            continue

        for key in aliases:
            value = parse_number(discovered_config.get(key))
            if value is not None:
                row[output_key] = value
                break

    netlist_path = find_netlist(run_dir)

    row.update(extract_netlist_features(netlist_path))
    row.update(parse_timing_reports(run_dir))
    row.update(parse_physical_reports(run_dir))
    row.update(extract_artifact_presence(run_dir))

    # Preserve a stable schema. Missing metrics are intentionally blank.
    for field in FEATURE_FIELDS:
        row.setdefault(field, "")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=FEATURE_FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerow(row)

    print(f"Run ID: {row['run_id']}")
    print(f"Netlist found: {'yes' if netlist_path else 'no'}")
    print(f"Final DEF found: {row['final_def']}")
    print(f"Final ODB found: {row['final_odb']}")
    print(f"Feature CSV written: {output_path}")

    missing = [
        field
        for field in FEATURE_FIELDS
        if field not in {"run_id"} and row.get(field, "") == ""
    ]

    if missing:
        print("Unavailable metrics left blank: " + ", ".join(missing))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
