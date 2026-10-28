
#!/usr/bin/env python3
"""Extract FIR synthesis features and OpenROAD physical targets.

Outputs:
  preplacement_features.csv / .json
  physical_targets.csv / .json
  features.csv / .json
  fir_features.csv
  extraction_status.json

Unavailable metrics remain null; values are not fabricated.
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


# ============================================================
# General helpers
# ============================================================

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
        key: value
        for key, value in data.items()
        if not isinstance(value, (dict, list, tuple))
    }
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(flat))
        writer.writeheader()
        writer.writerow(flat)


def write_rows_csv(
    path: Path,
    fields: list[str],
    row: dict[str, Any],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerow({key: row.get(key) for key in fields})


def last_number(
    text: str,
    patterns: list[str],
) -> float | None:
    """Return the last number matched by the first matching pattern."""
    for pattern in patterns:
        matches = list(re.finditer(pattern, text, re.I | re.M))
        for match in reversed(matches):
            value = finite_float(match.group(1))
            if value is not None:
                return value
    return None


def collect_logs(run_dir: Path) -> dict[str, str]:
    """Collect nonempty .log and .txt files recursively."""
    if not run_dir.is_dir():
        return {}

    paths = [
        path
        for path in run_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".log", ".txt"}
    ]

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

    logs = {}
    for path in paths:
        content = read_text(path)
        if content.strip():
            logs[str(path.relative_to(run_dir))] = content

    return logs


def choose_routing_log(
    logs: dict[str, str],
) -> tuple[str | None, str]:
    """Prefer a detailed-routing log; fall back to all collected logs."""
    preferred = (
        "5_2_route.log",
        "detailed_route.log",
        "openroad_log.txt",
        "flow_diagnostics.log",
    )

    for wanted in preferred:
        for name, content in logs.items():
            if Path(name).name.lower() != wanted:
                continue

            if re.search(
                r"detailed routing|detail routing|total wire length|"
                r"number of vias|violations",
                content,
                re.I,
            ):
                return name, content

    if logs:
        return "combined_logs", "\n".join(logs.values())

    return None, ""


def find_report(
    run_dir: Path,
    candidates: list[str],
) -> tuple[Path | None, str]:
    """Find a nonempty report in the run directory or its subdirectories."""
    for candidate in candidates:
        paths = [run_dir / candidate]
        paths.extend(sorted(run_dir.rglob(candidate)))

        seen = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)

            if path.is_file():
                content = read_text(path)
                if content.strip():
                    return path, content

    return None, ""


# ============================================================
# Netlist extraction
# ============================================================

def classify_cell(cell_type: str) -> str:
    """
    Heuristic classification based on cell names.

    Validate these categories against Sky130 Liberty metadata
    before treating them as research-grade cell classifications.
    """
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

    if not isinstance(data, dict) or not isinstance(
        data.get("modules"), dict
    ):
        raise ValueError(
            "Expected a Yosys JSON file containing 'modules'."
        )

    return data


def select_top(
    modules: dict[str, Any],
    requested_top: str,
) -> str:
    if requested_top in modules:
        return requested_top

    marked = []

    for name, module in modules.items():
        value = str(
            module.get("attributes", {}).get("top", "")
        ).strip("'")

        if value.lstrip("0") == "1":
            marked.append(name)

    if len(marked) == 1:
        print(
            f"WARNING: using detected top module {marked[0]}",
            file=sys.stderr,
        )
        return marked[0]

    if len(modules) == 1:
        return next(iter(modules))

    raise ValueError(
        f"Top '{requested_top}' not found. "
        f"Available modules: {list(modules)}"
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
        str(cell.get("type", "unknown"))
        for cell in cells.values()
    )

    class_counts = Counter()
    for cell_type, count in cell_types.items():
        class_counts[classify_cell(cell_type)] += count

    # Calculate fanout only when actual input-port directions exist.
    # Do not infer missing directions from signal names.
    all_bits: set[int] = set()
    sink_counts: Counter[int] = Counter()
    direction_metadata_found = False

    for obj in list(netnames.values()) + list(ports.values()):
        for bit in obj.get("bits", []):
            if isinstance(bit, int) and not isinstance(bit, bool):
                all_bits.add(bit)

    for cell in cells.values():
        connections = cell.get("connections", {})
        directions = cell.get("port_directions", {})

        if isinstance(directions, dict) and directions:
            direction_metadata_found = True

        for port_name, bits in connections.items():
            direction = str(
                directions.get(port_name, "")
            ).lower()

            for bit in bits:
                if isinstance(bit, int) and not isinstance(bit, bool):
                    all_bits.add(bit)

                    if direction == "input":
                        sink_counts[bit] += 1

    fanouts = (
        [
            sink_counts[bit]
            for bit in all_bits
            if sink_counts[bit] > 0
        ]
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
        "port_bit_count": sum(
            len(port.get("bits", []))
            for port in ports.values()
        ),
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
        "cell_classification_method": (
            "heuristic cell-name matching; validate against Liberty"
        ),
    }


# ============================================================
# Timing extraction
# ============================================================

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
            rf"\btotal\s+negative\s+slack"
            rf"(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})",
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


# ============================================================
# DEF and routing-log extraction
# ============================================================

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
    """Parse the last matching values in the selected routing log."""
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
    effective_density = last_number(text, [
        rf"\buniform density\s+to\s+({NUM})",
        rf"\b(?:auto(?:matically)?\s+)?adjust(?:s|ed|ing)?\s+"
        rf"uniform density\s+to\s+({NUM})",
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


# ============================================================
# Physical target extraction
# ============================================================

def physical_features(run_dir: Path) -> dict[str, Any]:
    if not run_dir.is_dir():
        raise FileNotFoundError(
            f"Run directory not found: {run_dir}"
        )

    def_path = run_dir / "final.def"
    odb_path = run_dir / "final.odb"
    def_text = read_text(def_path)

    setup_path, setup_text = find_report(run_dir, [
        "wns_setup.rpt",
        "wns_setup_global_route.rpt",
        "timing_setup.rpt",
        "timing_setup_global_route.rpt",
        "setup_timing.rpt",
        "setup.rpt",
    ])

    tns_path, tns_text = find_report(run_dir, [
        "tns_setup.rpt",
        "tns_setup_global_route.rpt",
        "timing_setup.rpt",
        "timing_setup_global_route.rpt",
        "setup_timing.rpt",
        "setup.rpt",
    ])

    hold_path, hold_text = find_report(run_dir, [
        "wns_hold.rpt",
        "timing_hold.rpt",
        "wns_hold_global_route.rpt",
        "hold_timing.rpt",
        "hold.rpt",
    ])

    congestion_path, congestion_text = find_report(run_dir, [
        "congestion.rpt",
        "global_route_congestion.rpt",
        "routing_congestion.rpt",
        "grt_congestion.rpt",
    ])

    logs = collect_logs(run_dir)
    route_log_name, route_text = choose_routing_log(logs)
    all_logs = "\n".join(logs.values())

    setup_source = setup_text or all_logs
    tns_source = tns_text or all_logs
    hold_source = hold_text or all_logs
    congestion_source = congestion_text or all_logs

    wns_setup = parse_timing(setup_source, "wns_setup_ns")
    tns_setup = parse_timing(tns_source, "tns_setup_ns")
    wns_hold = parse_timing(hold_source, "wns_hold_ns")

    route_metrics = parse_route_log(route_text)
    congestion_overflow = parse_congestion(congestion_source)
    physical_log_values = parse_physical_log_values(all_logs)

    def_counts = (
        parse_def_routing(def_text)
        if def_text
        else {
            "def_nets_section_found": False,
            "routed_net_count": None,
            "route_segment_count": None,
        }
    )

    # Search all collected logs for the success marker, including nested logs.
    detailed_route_success = bool(re.search(
        r"SUCCESS:\s*Detailed routing|Complete detail routing",
        all_logs,
        re.I,
    ))

    def_ok = (
        def_path.is_file()
        and def_path.stat().st_size > 0
    )
    odb_ok = (
        odb_path.is_file()
        and odb_path.stat().st_size > 0
    )

    measurements = {
        "wns_setup_ns": (
            "measured" if wns_setup is not None else "unavailable"
        ),
        "tns_setup_ns": (
            "measured" if tns_setup is not None else "unavailable"
        ),
        "wns_hold_ns": (
            "measured" if wns_hold is not None else "unavailable"
        ),
        "congestion_overflow": (
            "measured"
            if congestion_overflow is not None
            else "unavailable"
        ),
        "wirelength_um": (
            "measured"
            if route_metrics["wirelength_um"] is not None
            else "unavailable"
        ),
        "via_count": (
            "measured"
            if route_metrics["via_count"] is not None
            else "unavailable"
        ),
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
    }

    return {
        "final_def_available": def_path.is_file(),
        "final_odb_available": odb_path.is_file(),
        "final_def_nonempty": bool(def_text.strip()),
        "artifacts_present": def_ok and odb_ok,
        "detailed_route_success_marker_found": detailed_route_success,
        **def_counts,
        **route_metrics,
        **physical_log_values,
        "wns_setup_ns": wns_setup,
        "tns_setup_ns": tns_setup,
        "wns_hold_ns": wns_hold,
        "congestion_overflow": congestion_overflow,
        "setup_report_source": (
            str(setup_path)
            if setup_path
            else "logs" if setup_source else None
        ),
        "tns_report_source": (
            str(tns_path)
            if tns_path
            else "logs" if tns_source else None
        ),
        "hold_report_source": (
            str(hold_path)
            if hold_path
            else "logs" if hold_source else None
        ),
        "congestion_report_source": (
            str(congestion_path)
            if congestion_path
            else "logs" if congestion_source else None
        ),
        "routing_log_source": route_log_name,
        "log_files_found": list(logs),
        "target_measurement_status": measurements,
        "note": (
            "DEF route counts are approximate. Cell classifications are "
            "heuristic and should be validated against Liberty metadata. "
            "Fanout is unavailable if Yosys port directions are missing. "
            "Router violations are not independent signoff DRC. Congestion "
            "remains null unless a recognized overflow metric is found."
        ),
        "target_feature_stage": (
            "post-placement/routing targets; exclude these outcomes from "
            "predictors when predicting the same outcomes"
        ),
    }


# ============================================================
# Configuration CSV and flow-status detection
# ============================================================


def write_human_report(
    outdir: Path,
    pre: dict[str, Any],
    post: dict[str, Any],
    args: argparse.Namespace,
    run_dir: Path,
) -> None:
    """Write a readable engineering report with assessments and actions."""

    def fmt(value: Any, unit: str = "") -> str:
        if value is None:
            return "Unavailable"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, float):
            return f"{value:,.4f}{unit}"
        return f"{value}{unit}"

    def assess(metric: str, value: Any) -> str:
        if value is None:
            return "Not assessed: metric unavailable"
        if metric in ("wns_setup_ns", "wns_hold_ns"):
            return "Non-negative slack" if value >= 0 else "Negative slack: investigate"
        if metric == "tns_setup_ns":
            return "Zero setup TNS" if value == 0 else (
                "Negative TNS: investigate" if value < 0
                else "Unexpected positive TNS: verify parser"
            )
        if metric == "final_detailed_route_violations":
            return "Zero reported violations" if value == 0 else "Investigate reported violations"
        if metric == "congestion_overflow":
            return "Zero reported overflow" if value == 0 else "Compare with baseline and tool definition"
        if metric == "avg_bit_fanout":
            return "Extracted fanout" if value is not None else "Input-port direction metadata missing"
        if metric in ("cell_count", "sequential_cell_count",
                      "combinational_cell_count", "unique_cell_type_count"):
            return "Informational; cell classification is heuristic"
        if metric in ("wirelength_um", "via_count", "design_area_um2"):
            return "Compare across runs using the same extraction method"
        if metric == "effective_place_density":
            return "Verify whether this is target or achieved density"
        return "Verify metric definition and source"

    def action(metric: str, value: Any) -> str:
        if value is None:
            if metric == "congestion_overflow":
                return "Inspect the OpenROAD congestion report/log and add a parser for its actual format."
            if metric == "avg_bit_fanout":
                return "Check whether the Yosys JSON contains cell port_directions; otherwise enrich the netlist metadata."
            if metric == "effective_place_density":
                return "Inspect placement logs and distinguish requested density from achieved utilization."
            if metric in ("wns_setup_ns", "tns_setup_ns", "wns_hold_ns"):
                return "Inspect the corresponding timing report and verify the parser matched the correct check."
            return "Inspect the source report/log; keep this value null until it can be extracted reliably."

        if metric in ("wns_setup_ns", "wns_hold_ns") and value < 0:
            return "Inspect violating paths and check constraints before attempting timing repair."
        if metric == "tns_setup_ns" and value < 0:
            return "Inspect setup violations and confirm the report's TNS units and semantics."
        if metric == "final_detailed_route_violations" and value != 0:
            return "Review detailed-routing violations and rerun checks after any repair."
        if metric == "congestion_overflow" and value != 0:
            return "Identify congested regions and compare placement/routing settings."
        if metric in ("wirelength_um", "via_count", "design_area_um2"):
            return "Compare with other configurations; do not optimize this metric in isolation."
        if metric == "effective_place_density":
            return "Confirm the metric definition before using it as an ML target."
        if metric == "avg_bit_fanout":
            return "Check fanout distribution and investigate high-fanout nets."
        if metric in ("cell_count", "sequential_cell_count",
                      "combinational_cell_count", "unique_cell_type_count"):
            return "Validate cell categories against Liberty metadata before research use."
        return "Retain the result and validate against the source report."

    # Match the status logic used by write_configuration_csv().
    def flow_status() -> tuple[str, str]:
        def_ok = bool(
            post.get("final_def_available")
            and post.get("final_def_nonempty")
        )
        odb_ok = bool(post.get("final_odb_available"))
        route_success = bool(post.get("detailed_route_success_marker_found"))

        if route_success and def_ok and odb_ok:
            return "detailed_routing_completed", "detailed_routing"
        if def_ok and odb_ok:
            return "artifacts_present", "unknown"
        if def_ok or odb_ok:
            return "partial_artifacts", "unknown"
        if post.get("log_files_found"):
            return "flow_incomplete_or_unverified", "unknown"
        return "no_artifacts_or_logs", "unknown"

    metrics = [
        ("cell_count", "Synthesis cell count", pre.get("cell_count"), "", "Mapped cell instances in the synthesized netlist."),
        ("sequential_cell_count", "Sequential cells", pre.get("sequential_cell_count"), "", "Cells classified as sequential using name patterns."),
        ("combinational_cell_count", "Combinational cells", pre.get("combinational_cell_count"), "", "Cells classified as combinational using name patterns."),
        ("unique_cell_type_count", "Unique cell types", pre.get("unique_cell_type_count"), "", "Number of distinct mapped cell types."),
        ("avg_bit_fanout", "Average bit fanout", pre.get("avg_bit_fanout"), "", "Average sink count per included net bit."),
        ("effective_place_density", "Effective placement density", post.get("effective_place_density"), "", "Density value extracted from logs; may be a requested setting."),
        ("design_area_um2", "Reported design area", post.get("design_area_um2"), " µm²", "Area reported by the matched log pattern; verify its exact definition."),
        ("wirelength_um", "Routed wirelength", post.get("wirelength_um"), " µm", "Wirelength extracted from routing logs."),
        ("via_count", "Via count", post.get("via_count"), "", "Via count extracted from routing logs."),
        ("wns_setup_ns", "Setup WNS", post.get("wns_setup_ns"), " ns", "Worst setup slack extracted from a report or fallback logs."),
        ("tns_setup_ns", "Setup TNS", post.get("tns_setup_ns"), " ns", "Total negative setup slack extracted from a report or fallback logs."),
        ("wns_hold_ns", "Hold WNS", post.get("wns_hold_ns"), " ns", "Worst hold slack extracted from a report or fallback logs."),
        ("congestion_overflow", "Congestion overflow", post.get("congestion_overflow"), "", "Overflow metric, if recognized in the report or logs."),
        ("final_detailed_route_violations", "Detailed-route violations", post.get("final_detailed_route_violations"), "", "Extracted router violations; not independent signoff DRC."),
    ]

    detected_status, last_stage = flow_status()
    run_id = args.run_id or run_dir.name

    lines = [
        "# FIR Physical Design Run Report",
        "",
        f"- **Run ID:** `{run_id}`",
        f"- **Design ID:** `{args.design_id}`",
        f"- **Top module:** `{pre.get('top_module')}`",
        f"- **Run directory:** `{run_dir}`",
        f"- **Detected flow status:** `{detected_status}`",
        f"- **Last completed stage:** `{last_stage}`",
        "",
        "## Configuration",
        "",
        "| Parameter | Value |",
        "|---|---:|",
        f"| Taps | {fmt(args.taps)} |",
        f"| Data width | {fmt(args.data_width, ' bits')} |",
        f"| Coefficient width | {fmt(args.coeff_width, ' bits')} |",
        f"| Accumulator width | {fmt(args.acc_width, ' bits')} |",
        f"| Target utilization | {fmt(args.utilization, '%')} |",
        f"| Requested placement density | {fmt(args.place_density)} |",
        f"| Clock period | {fmt(args.clock_period, ' ns')} |",
        "",
        "## Metrics, meaning, assessment and recommended action",
        "",
        "| Tag | Value | Meaning | Assessment | Recommended action |",
        "|---|---:|---|---|---|",
    ]

    for key, label, value, unit, meaning in metrics:
        lines.append(
            f"| `{key}` ({label}) | {fmt(value, unit)} | {meaning} | "
            f"{assess(key, value)} | {action(key, value)} |"
        )

    missing = [key for key, _, value, _, _ in metrics if value is None]

    lines.extend(["", "## Artifact checks", ""])
    lines.append(f"- Final DEF available: {fmt(post.get('final_def_available'))}")
    lines.append(f"- Final DEF nonempty: {fmt(post.get('final_def_nonempty'))}")
    lines.append(f"- Final ODB available: {fmt(post.get('final_odb_available'))}")
    lines.append(
        "- Detailed-route success marker: "
        f"{fmt(post.get('detailed_route_success_marker_found'))}"
    )

    lines.extend(["", "## Required follow-up", ""])
    if missing:
        lines.append("Metrics not extracted:")
        lines.extend(f"- `{key}`" for key in missing)
    else:
        lines.append("All listed metrics have values. Validate their definitions and units before drawing research conclusions.")

    lines.extend([
        "",
        "## Extraction caveats",
        "",
        f"- Fanout method: {pre.get('fanout_method', 'unknown')}.",
        f"- Cell classification: {pre.get('cell_classification_method', 'unknown')}.",
        f"- Routing note: {post.get('note', 'No note provided')}",
        "- DEF-derived route counts may be approximate.",
        "- Router-reported zero violations does not establish independent signoff DRC closure.",
        "- A single run cannot establish optimization quality; compare configurations under consistent constraints.",
        "- Exclude post-placement/routing targets from predictor features when predicting those same outcomes.",
        "",
    ])

    (outdir / "run_report.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def write_configuration_csv(
    outdir: Path,
    run_dir: Path,
    args: argparse.Namespace,
    post: dict[str, Any],
) -> None:
    """Write the FIR configuration and automatically detected flow status."""

    def_ok = bool(
        post.get("final_def_available")
        and post.get("final_def_nonempty")
    )
    odb_ok = bool(post.get("final_odb_available"))
    route_success = bool(
        post.get("detailed_route_success_marker_found")
    )

    if route_success and def_ok and odb_ok:
        flow_status = "detailed_routing_completed"
        last_stage = "detailed_routing"
    elif def_ok and odb_ok:
        flow_status = "artifacts_present"
        last_stage = "unknown"
    elif def_ok or odb_ok:
        flow_status = "partial_artifacts"
        last_stage = "unknown"
    elif post.get("log_files_found"):
        flow_status = "flow_incomplete_or_unverified"
        last_stage = "unknown"
    else:
        flow_status = "no_artifacts_or_logs"
        last_stage = "unknown"

    fields = [
        "run_id",
        "taps",
        "data_width",
        "coeff_width",
        "acc_width",
        "utilization",
        "place_density",
        "clk_period_ns",
        "flow_status",
        "last_completed_stage",
    ]

    row = {
        "run_id": args.run_id or run_dir.name,
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


# ============================================================
# Main
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract FIR netlist features and OpenROAD physical targets."
        )
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
    parser.add_argument(
        "--design-id",
        default="fir_filter_8tap",
    )
    parser.add_argument("--run-id", default=None)

    parser.add_argument("--taps", type=int, default=None)
    parser.add_argument("--data-width", type=int, default=None)
    parser.add_argument("--coeff-width", type=int, default=None)
    parser.add_argument("--acc-width", type=int, default=None)
    parser.add_argument("--utilization", type=float, default=None)
    parser.add_argument("--place-density", type=float, default=None)
    parser.add_argument("--clock-period", type=float, default=None)
    parser.add_argument(
        "--flow-status",
        default=None,
        help="Deprecated; detected status is used instead",
    )

    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    outdir = Path(args.outdir) if args.outdir else run_dir
    netlist_path = Path(args.netlist_json)

    if args.utilization is not None and not (
        0 < args.utilization <= 100
    ):
        parser.error("--utilization must be > 0 and <= 100.")

    if args.place_density is not None and not (
        0 < args.place_density <= 1
    ):
        parser.error("--place-density must be > 0 and <= 1.")

    if args.clock_period is not None and args.clock_period <= 0:
        parser.error("--clock-period must be positive.")

    for name in ("taps", "data_width", "coeff_width", "acc_width"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            parser.error(
                f"--{name.replace('_', '-')} must be positive."
            )

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

        write_human_report(
            outdir,
            pre,
            post,
            args,
            run_dir,
        )
        write_configuration_csv(
            outdir,
            run_dir,
            args,
            post,
        )

        status = {
            "design_id": args.design_id,
            "run_id": args.run_id or run_dir.name,
            "netlist_json": str(netlist_path),
            "run_dir": str(run_dir),
            "output_dir": str(outdir),
            "fanout_direction_metadata_found": (
                pre["fanout_direction_metadata_found"]
            ),
            "final_def_available": post["final_def_available"],
            "final_odb_available": post["final_odb_available"],
            "artifacts_present": post["artifacts_present"],
            "detailed_route_success_marker_found": (
                post["detailed_route_success_marker_found"]
            ),
            "target_measurement_status": (
                post["target_measurement_status"]
            ),
        }

        write_json(outdir / "extraction_status.json", status)

    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("\n===== FIR Feature Extraction =====")
    print(f"Run directory: {run_dir}")
    print(f"Output directory: {outdir}")
    print(f"Cell count: {pre['cell_count']}")
    print(f"Sequential: {pre['sequential_cell_count']}")
    print(f"Combinational: {pre['combinational_cell_count']}")
    print(f"Fanout method: {pre['fanout_method']}")
    print(f"Final DEF: {post['final_def_available']}")
    print(f"Final ODB: {post['final_odb_available']}")
    print(
        "Routing success marker: "
        f"{post['detailed_route_success_marker_found']}"
    )

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

    print(
        "\nGenerated: preplacement_features.csv, physical_targets.csv,"
    )
    print(
        "           features.csv, fir_features.csv, "
        "extraction_status.json, run_report.md"
    )
    print("\nExtraction finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
