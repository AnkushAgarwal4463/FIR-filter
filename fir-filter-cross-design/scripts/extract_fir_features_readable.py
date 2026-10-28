#!/usr/bin/env python3
"""FIR feature extraction helpers used by the multi-run dataset builder."""

from __future__ import annotations

import json
import math
import re
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
    r"(?:and|or|xor|xnor|mux|maj|fa|ha|aoi|oai|nand|nor|add|sub|mul|alu|logic|cmp)",
    re.I,
)


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


def select_top(modules: dict[str, Any], requested_top: str) -> str:
    if requested_top in modules:
        return requested_top

    marked = []
    for name, module in modules.items():
        value = str(module.get("attributes", {}).get("top", "")).strip("'")
        if value.lstrip("0") == "1":
            marked.append(name)

    if len(marked) == 1:
        return marked[0]

    if len(modules) == 1:
        return next(iter(modules))

    raise ValueError(
        f"Top '{requested_top}' not found. Available: {list(modules)}"
    )


def classify_cell(cell_type: str) -> str:
    """Heuristic cell classification; validate against Liberty metadata."""
    name = str(cell_type).lower().lstrip("\\")

    if SEQ_RE.search(name):
        return "sequential"
    if BUF_RE.search(name):
        return "buffer_inverter"
    if COMB_RE.search(name):
        return "combinational"

    return "other"


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
    classes = Counter()

    for cell_type, count in cell_types.items():
        classes[classify_cell(cell_type)] += count

    all_bits: set[int] = set()
    sink_counts: Counter[int] = Counter()
    directions_found = False

    for obj in list(netnames.values()) + list(ports.values()):
        for bit in obj.get("bits", []):
            if isinstance(bit, int) and not isinstance(bit, bool):
                all_bits.add(bit)

    for cell in cells.values():
        connections = cell.get("connections", {})
        directions = cell.get("port_directions", {})

        if isinstance(directions, dict) and directions:
            directions_found = True

        for port_name, bits in connections.items():
            direction = str(directions.get(port_name, "")).lower()

            for bit in bits:
                if isinstance(bit, int) and not isinstance(bit, bool):
                    all_bits.add(bit)

                    if direction == "input":
                        sink_counts[bit] += 1

    fanouts = (
        [sink_counts[bit] for bit in all_bits if sink_counts[bit] > 0]
        if directions_found
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
        "port_bit_count": sum(
            len(port.get("bits", [])) for port in ports.values()
        ),
        "sequential_cell_count": classes["sequential"],
        "combinational_cell_count": classes["combinational"],
        "buffer_inverter_count": classes["buffer_inverter"],
        "other_cell_count": classes["other"],
        "unique_cell_type_count": len(cell_types),
        "avg_bit_fanout": mean(fanouts) if fanouts else None,
        "max_bit_fanout": max(fanouts) if fanouts else None,
        "std_bit_fanout": (
            pstdev(fanouts)
            if len(fanouts) > 1
            else (0.0 if fanouts else None)
        ),
        "fanout_direction_metadata_found": directions_found,
        "fanout_method": (
            "Yosys cell input-port directions"
            if directions_found
            else "unavailable: cell port directions missing"
        ),
        "total_net_bits": net_bit_total,
        "unique_cell_types": dict(cell_types),
        "feature_stage": "post-synthesis / pre-placement",
        "cell_classification_method": (
            "heuristic cell-name matching; validate against Liberty"
        ),
    }


def last_number(text: str, patterns: list[str]) -> float | None:
    for pattern in patterns:
        matches = list(re.finditer(pattern, text, re.I | re.M))

        for match in reversed(matches):
            value = finite_float(match.group(1))
            if value is not None:
                return value

    return None


def collect_logs(run_dir: Path) -> dict[str, str]:
    """Collect log and text files recursively."""
    if not run_dir.is_dir():
        return {}

    paths = sorted(
        path
        for path in run_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".log", ".txt"}
    )

    priority = {
        "5_2_route.log": 0,
        "detailed_route.log": 1,
        "openroad_log.txt": 2,
        "flow_diagnostics.log": 3,
    }
    paths.sort(
        key=lambda path: (
            priority.get(path.name.lower(), 4),
            str(path),
        )
    )

    result = {}
    for path in paths:
        content = read_text(path)
        if content.strip():
            result[str(path.relative_to(run_dir))] = content

    return result


def choose_routing_log(logs: dict[str, str]) -> tuple[str | None, str]:
    priority = (
        "5_2_route.log",
        "detailed_route.log",
        "openroad_log.txt",
        "flow_diagnostics.log",
    )

    for wanted in priority:
        for name, content in logs.items():
            if (
                Path(name).name.lower() == wanted
                and re.search(
                    r"detailed routing|detail routing|total wire length|"
                    r"number of vias|violations",
                    content,
                    re.I,
                )
            ):
                return name, content

    if logs:
        return "combined_logs", "\n".join(logs.values())

    return None, ""


def timing_patterns() -> dict[str, list[str]]:
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


def parse_def_routing(text: str) -> dict[str, Any]:
    section = re.search(
        r"\bNETS\s+\d+\s*;(.*?)\bEND\s+NETS\b",
        text,
        re.I | re.S,
    )

    if not section:
        return {
            "def_nets_section_found": False,
            "routed_net_count": None,
            "route_segment_count": None,
        }

    entries = re.findall(r"(?ms)^[ \t]*-[ \t]+.*?;", section.group(1))
    routed = 0
    clauses = 0

    for entry in entries:
        found = re.findall(
            r"\b(?:ROUTED|FIXED|COVER|NEW)\s+[A-Za-z0-9_.$/\[\]-]+",
            entry,
            re.I,
        )

        if found:
            routed += 1

        clauses += len(
            re.findall(
                r"\b(?:ROUTED|NEW)\s+[A-Za-z0-9_.$/\[\]-]+",
                entry,
                re.I,
            )
        )

    return {
        "def_nets_section_found": True,
        "routed_net_count": routed,
        "route_segment_count": clauses,
    }


def parse_route_log(text: str) -> dict[str, Any]:
    wire = last_number(
        text,
        [
            rf"\bTotal\s+wire\s+length\s*[:=]\s*({NUM})\s*(?:um|µm)\b",
            rf"\bTotal\s+wire\s+length\s*[:=]\s*({NUM})",
        ],
    )
    vias = last_number(
        text,
        [
            rf"\bTotal\s+number\s+of\s+vias\s*[:=]\s*({NUM})",
            rf"\bNumber\s+of\s+vias\s*[:=]\s*({NUM})",
        ],
    )
    violations = last_number(
        text,
        [
            rf"\bNumber\s+of\s+violations\s*[:=]\s*({NUM})",
            rf"\bTotal\s+violations\s*[:=]\s*({NUM})",
        ],
    )

    return {
        "wirelength_um": wire,
        "via_count": int(vias) if vias is not None else None,
        "final_detailed_route_violations": (
            int(violations) if violations is not None else None
        ),
    }


def parse_congestion(text: str) -> float | None:
    value = last_number(
        text,
        [
            rf"\btotal\s+(?:routing\s+)?overflow\s*[:=]\s*({NUM})",
            rf"\bcongestion\s+overflow\s*[:=]\s*({NUM})",
            rf"\brouting\s+overflow\s*[:=]\s*({NUM})",
        ],
    )

    if value is not None:
        return value

    if re.search(
        r"\b(?:no overflowing gcells|no overflowing gcell|zero routing overflow)\b",
        text,
        re.I,
    ):
        return 0.0

    return None


def parse_physical_log_values(text: str) -> dict[str, Any]:
    density = last_number(
        text,
        [
            rf"\buniform density\s+to\s+({NUM})",
            rf"\b(?:auto(?:matically)?\s+)?adjust(?:s|ed|ing)?\s+uniform density\s+to\s+({NUM})",
            rf"\beffective\s+(?:placement\s+)?density\s*[:=]\s*({NUM})",
        ],
    )
    area = last_number(
        text,
        [
            rf"\bmovable instances area\s*[:=]?\s*({NUM})",
            rf"\bdesign area\s*[:=]\s*({NUM})",
        ],
    )

    return {
        "effective_place_density": density,
        "design_area_um2": area,
    }


def find_report(
    run_dir: Path,
    candidates: list[str],
) -> tuple[Path | None, str]:
    for candidate in candidates:
        paths = [run_dir / candidate] + sorted(run_dir.rglob(candidate))

        for path in paths:
            if path.is_file():
                content = read_text(path)
                if content.strip():
                    return path, content

    return None, ""


def physical_features(run_dir: Path) -> dict[str, Any]:
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    def_path = run_dir / "final.def"
    odb_path = run_dir / "final.odb"
    def_text = read_text(def_path)

    setup_path, setup_text = find_report(
        run_dir,
        [
            "wns_setup.rpt",
            "wns_setup_global_route.rpt",
            "timing_setup.rpt",
            "timing_setup_global_route.rpt",
            "setup_timing.rpt",
            "setup.rpt",
        ],
    )
    tns_path, tns_text = find_report(
        run_dir,
        [
            "tns_setup.rpt",
            "tns_setup_global_route.rpt",
            "timing_setup.rpt",
            "timing_setup_global_route.rpt",
            "setup_timing.rpt",
            "setup.rpt",
        ],
    )
    hold_path, hold_text = find_report(
        run_dir,
        [
            "wns_hold.rpt",
            "timing_hold.rpt",
            "wns_hold_global_route.rpt",
            "hold_timing.rpt",
            "hold.rpt",
        ],
    )
    cong_path, cong_text = find_report(
        run_dir,
        [
            "congestion.rpt",
            "global_route_congestion.rpt",
            "routing_congestion.rpt",
            "grt_congestion.rpt",
        ],
    )

    logs = collect_logs(run_dir)
    route_name, route_text = choose_routing_log(logs)
    all_logs = "\n".join(logs.values())

    setup_src = setup_text or all_logs
    tns_src = tns_text or all_logs
    hold_src = hold_text or all_logs
    cong_src = cong_text or all_logs

    wns_setup = parse_timing(setup_src, "wns_setup_ns")
    tns_setup = parse_timing(tns_src, "tns_setup_ns")
    wns_hold = parse_timing(hold_src, "wns_hold_ns")

    route = parse_route_log(route_text)
    congestion = parse_congestion(cong_src)
    physical = parse_physical_log_values(all_logs)

    def_counts = (
        parse_def_routing(def_text)
        if def_text
        else {
            "def_nets_section_found": False,
            "routed_net_count": None,
            "route_segment_count": None,
        }
    )

    route_success = bool(
        re.search(
            r"SUCCESS:\s*Detailed routing|Complete detail routing",
            all_logs,
            re.I,
        )
    )

    def_ok = def_path.is_file() and def_path.stat().st_size > 0
    odb_ok = odb_path.is_file() and odb_path.stat().st_size > 0

    measurements = {
        "wns_setup_ns": "measured" if wns_setup is not None else "unavailable",
        "tns_setup_ns": "measured" if tns_setup is not None else "unavailable",
        "wns_hold_ns": "measured" if wns_hold is not None else "unavailable",
        "congestion_overflow": "measured" if congestion is not None else "unavailable",
        "wirelength_um": "measured" if route["wirelength_um"] is not None else "unavailable",
        "via_count": "measured" if route["via_count"] is not None else "unavailable",
        "final_detailed_route_violations": (
            "measured"
            if route["final_detailed_route_violations"] is not None
            else "unavailable"
        ),
        "design_area_um2": (
            "measured" if physical["design_area_um2"] is not None else "unavailable"
        ),
    }

    return {
        "final_def_available": def_path.is_file(),
        "final_odb_available": odb_path.is_file(),
        "final_def_nonempty": bool(def_text.strip()),
        "artifacts_present": def_ok and odb_ok,
        "detailed_route_success_marker_found": route_success,
        **def_counts,
        **route,
        **physical,
        "wns_setup_ns": wns_setup,
        "tns_setup_ns": tns_setup,
        "wns_hold_ns": wns_hold,
        "congestion_overflow": congestion,
        "setup_report_source": str(setup_path) if setup_path else ("logs" if setup_src else None),
        "tns_report_source": str(tns_path) if tns_path else ("logs" if tns_src else None),
        "hold_report_source": str(hold_path) if hold_path else ("logs" if hold_src else None),
        "congestion_report_source": str(cong_path) if cong_path else ("logs" if cong_src else None),
        "routing_log_source": route_name,
        "log_files_found": list(logs),
        "target_measurement_status": measurements,
        "note": (
            "DEF route counts are approximate. Cell classes are heuristic and "
            "should be validated against Liberty. Fanout is unavailable if "
            "Yosys port directions are missing. Router violations are not "
            "independent signoff DRC. Congestion remains null unless a "
            "recognized overflow metric is found."
        ),
        "target_feature_stage": (
            "post-placement/routing targets; exclude these outcomes from "
            "predictors when predicting the same outcomes"
        ),
    }


DISPLAY_NAMES = {
    "design_id": "Design ID",
    "top_module": "Top module",
    "netlist_json": "Netlist JSON",
    "cell_count": "Cell count",
    "netname_count": "Net names",
    "port_count": "Port count",
    "port_bit_count": "Port bits",
    "sequential_cell_count": "Sequential cells",
    "combinational_cell_count": "Combinational cells",
    "buffer_inverter_count": "Buffers / inverters",
    "other_cell_count": "Other cells",
    "unique_cell_type_count": "Unique cell types",
    "avg_bit_fanout": "Average bit fanout",
    "max_bit_fanout": "Maximum bit fanout",
    "std_bit_fanout": "Bit fanout standard deviation",
    "fanout_direction_metadata_found": "Fanout direction metadata found",
    "fanout_method": "Fanout extraction method",
    "total_net_bits": "Total net bits",
    "feature_stage": "Feature stage",
    "cell_classification_method": "Cell classification method",
    "utilization_constraint_percent": "Utilization constraint (%)",
    "clock_period_ns": "Clock period (ns)",
    "final_def_available": "Final DEF available",
    "final_odb_available": "Final ODB available",
    "final_def_nonempty": "Final DEF non-empty",
    "artifacts_present": "Final DEF and ODB present",
    "detailed_route_success_marker_found": "Detailed routing success marker found",
    "def_nets_section_found": "DEF NETS section found",
    "routed_net_count": "Routed nets (approx.)",
    "route_segment_count": "Route segments (approx.)",
    "wirelength_um": "Wirelength (µm)",
    "via_count": "Via count",
    "final_detailed_route_violations": "Detailed-route violations (reported)",
    "effective_place_density": "Effective placement density",
    "design_area_um2": "Design area (µm²)",
    "wns_setup_ns": "Setup WNS (ns)",
    "tns_setup_ns": "Setup TNS (ns)",
    "wns_hold_ns": "Hold WNS (ns)",
    "congestion_overflow": "Congestion overflow",
    "setup_report_source": "Setup timing report source",
    "tns_report_source": "TNS report source",
    "hold_report_source": "Hold timing report source",
    "congestion_report_source": "Congestion report source",
    "routing_log_source": "Routing log source",
    "note": "Notes / limitations",
    "target_feature_stage": "Target stage / ML leakage warning",
    "run_id": "Run ID",
    "taps": "FIR taps",
    "data_width": "Data width (bits)",
    "coeff_width": "Coefficient width (bits)",
    "acc_width": "Accumulator width (bits)",
    "utilization": "Utilization (%)",
    "place_density": "Placement density",
    "clk_period_ns": "Clock period (ns)",
    "flow_status": "Flow status",
    "last_completed_stage": "Last completed stage",
    "fanout_total_cells": "Fanout total cells",
    "fanout_cells_with_any_directions": "Cells with direction metadata",
    "fanout_cells_with_complete_directions": "Cells with complete direction metadata",
    "fanout_direction_coverage_fraction": "Fanout direction coverage fraction",
    "fanout_direction_coverage_percent": "Fanout direction coverage (%)",
    "fanout_metadata_complete_for_all_cells": "Fanout metadata complete for all cells",
    "fanout_quality_warning": "Fanout metadata warning",
}


def display_value(value: Any) -> str:
    """Format values for readable Markdown reports."""
    if value is None or value == "":
        return "N/A (not extracted)"

    if isinstance(value, bool):
        return "Yes" if value else "No"

    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")

    if isinstance(value, dict):
        return "; ".join(
            f"{DISPLAY_NAMES.get(key, key.replace('_', ' ').title())}: "
            f"{display_value(item)}"
            for key, item in value.items()
        )

    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) if value else "N/A"

    return str(value)
