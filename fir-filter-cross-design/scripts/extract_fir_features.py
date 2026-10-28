#!/usr/bin/env python3
"""Extract FIR synthesis features and OpenROAD physical targets.

Outputs: preplacement_features.csv/.json, physical_targets.csv/.json,
features.csv/.json, fir_features.csv, extraction_status.json.
Unavailable metrics remain null; values are not fabricated.
"""
from __future__ import annotations
import argparse, csv, json, math, os, re, sys
from collections import Counter
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
SEQ_RE = re.compile(r"(?:sky130_fd_sc_hd__(?:df|sd|ed|sdf)|\b(?:dfrtp|dfrbp|dfrtn|dfr|dff|flipflop|latch)\b)", re.I)
BUF_RE = re.compile(r"(?:buf|inv|clkbuf)", re.I)
COMB_RE = re.compile(r"(?:and|or|xor|xnor|mux|maj|fa|ha|aoi|oai|nand|nor|add|sub|mul|alu|logic|cmp)", re.I)


def read_text(path: Path) -> str:
    try: return path.read_text(encoding="utf-8", errors="ignore")
    except OSError: return ""


def finite_float(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError): return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, allow_nan=False); f.write("\n")


def write_csv(path: Path, data: dict[str, Any]) -> None:
    # Keep nested diagnostics as JSON text in CSV rather than silently dropping them.
    flat = {
        k: json.dumps(v, sort_keys=True, ensure_ascii=False) if isinstance(v, (dict, list, tuple)) else v
        for k, v in data.items()
    }
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(flat)); writer.writeheader(); writer.writerow(flat)


def write_rows_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
        writer.writerow({k: row.get(k) for k in fields})


def last_number(text: str, patterns: list[str]) -> float | None:
    for pattern in patterns:
        matches = list(re.finditer(pattern, text, re.I | re.M))
        for match in reversed(matches):
            value = finite_float(match.group(1))
            if value is not None: return value
    return None


def collect_logs(run_dir: Path) -> dict[str, str]:
    """Collect .log/.txt files recursively, including nested OpenROAD logs."""
    if not run_dir.is_dir(): return {}
    paths = sorted(p for p in run_dir.rglob("*") if p.is_file() and p.suffix.lower() in {".log", ".txt"})
    priority = {"5_2_route.log": 0, "detailed_route.log": 1, "openroad_log.txt": 2, "flow_diagnostics.log": 3}
    paths.sort(key=lambda p: (priority.get(p.name.lower(), 4), str(p)))
    result = {}
    for p in paths:
        content = read_text(p)
        if content.strip(): result[str(p.relative_to(run_dir))] = content
    return result


def choose_routing_log(logs: dict[str, str]) -> tuple[str | None, str]:
    priority = ("5_2_route.log", "detailed_route.log", "openroad_log.txt", "flow_diagnostics.log")
    for wanted in priority:
        for name, content in logs.items():
            if Path(name).name.lower() == wanted and re.search(r"detailed routing|detail routing|total wire length|number of vias|violations", content, re.I):
                return name, content
    return ("combined_logs", "\n".join(logs.values())) if logs else (None, "")


def classify_cell(cell_type: str) -> str:
    """Heuristic only; validate cell classes against Liberty for research use."""
    name = str(cell_type).lower().lstrip("\\")
    if SEQ_RE.search(name): return "sequential"
    if BUF_RE.search(name): return "buffer_inverter"
    if COMB_RE.search(name): return "combinational"
    return "other"


def load_netlist(path: Path) -> dict[str, Any]:
    if not path.is_file(): raise FileNotFoundError(f"Netlist JSON not found: {path}")
    try: data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc: raise ValueError(f"Invalid JSON netlist: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("modules"), dict):
        raise ValueError("Expected a Yosys JSON file containing 'modules'.")
    return data


def select_top(modules: dict[str, Any], requested_top: str) -> str:
    if requested_top in modules: return requested_top
    marked = []
    for name, module in modules.items():
        value = str(module.get("attributes", {}).get("top", "")).strip("'")
        if value.lstrip("0") == "1": marked.append(name)
    if len(marked) == 1: return marked[0]
    if len(modules) == 1: return next(iter(modules))
    raise ValueError(f"Top '{requested_top}' not found. Available: {list(modules)}")


def configured_path(name: str) -> Path | None:
    """Read a path from the environment or config/sky130_example.env."""
    value = os.environ.get(name)
    if value:
        return Path(value.strip().strip("\"'"))
    cfg = Path("config/sky130_example.env")
    if cfg.is_file():
        pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*?)\s*$")
        for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = pattern.match(line)
            if match:
                value = match.group(1).split("#", 1)[0].strip().strip("\"'")
                # Expand shell-style ${VAR:-default} and ordinary ${VAR} references.
                value = re.sub(
                    r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]+)\}",
                    lambda m: os.environ.get(m.group(1), m.group(2)),
                    value,
                )
                value = os.path.expandvars(value)
                if value:
                    return Path(value)
    return None


def _balanced_block(text: str, open_brace: int) -> str:
    """Return a brace-delimited block body, respecting nested braces."""
    depth = 0
    for i in range(open_brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace + 1:i]
    return ""


def load_liberty_pin_directions(path: Path | None) -> dict[str, dict[str, str]]:
    """Read cell pin directions from Liberty as a fallback for Yosys JSON."""
    if path is None or not path.is_file():
        return {}
    text = read_text(path)
    result: dict[str, dict[str, str]] = {}
    for match in re.finditer(r"\bcell\s*\(\s*\"?([^\)\"\s]+)\"?\s*\)\s*\{", text, re.I):
        cell_name = match.group(1)
        body = _balanced_block(text, match.end() - 1)
        if not body:
            continue
        pins: dict[str, str] = {}
        for pin_match in re.finditer(r"\bpin\s*\(\s*\"?([^\)\"\s]+)\"?\s*\)\s*\{", body, re.I):
            pin_name = pin_match.group(1)
            pin_body = _balanced_block(body, pin_match.end() - 1)
            direction = re.search(r"\bdirection\s*:\s*(input|output|inout)\s*;", pin_body, re.I)
            if direction:
                pins[pin_name] = direction.group(1).lower()
        if pins:
            result[cell_name] = pins
    return result


def load_lef_sizes(path: Path | None) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float]]]:
    """Return LEF macro and site dimensions in microns."""
    macros: dict[str, tuple[float, float]] = {}
    sites: dict[str, tuple[float, float]] = {}
    if path is None or not path.is_file():
        return macros, sites
    text = read_text(path)
    size_re = re.compile(rf"\bSIZE\s+({NUM})\s+BY\s+({NUM})\s*;", re.I)
    for match in re.finditer(r"(?mi)^\s*MACRO\s+(\S+)\s*$", text):
        name = match.group(1)
        end = re.search(rf"(?mi)^\s*END\s+{re.escape(name)}\s*$", text[match.end():])
        if not end:
            continue
        body = text[match.end():match.end() + end.start()]
        size = size_re.search(body)
        if size:
            macros[name] = (float(size.group(1)), float(size.group(2)))
    for match in re.finditer(r"(?mi)^\s*SITE\s+(\S+)\s*$", text):
        name = match.group(1)
        end = re.search(rf"(?mi)^\s*END\s+{re.escape(name)}\s*$", text[match.end():])
        if not end:
            continue
        body = text[match.end():match.end() + end.start()]
        size = size_re.search(body)
        if size:
            sites[name] = (float(size.group(1)), float(size.group(2)))
    return macros, sites


def measure_density_from_def(def_text: str, lef_path: Path | None) -> dict[str, Any]:
    """Compute placed standard-cell area / placement-row area from DEF + LEF.

    This is an achieved-density estimate, not the requested global-placement target.
    It is only returned when both areas can be reconstructed from the files.
    """
    macros, sites = load_lef_sizes(lef_path)
    if not def_text or not macros or not sites:
        return {"effective_place_density": None, "placed_cell_area_from_def_um2": None,
                "placement_row_area_um2": None, "effective_place_density_method": "unavailable: DEF/LEF geometry incomplete"}

    component_section = re.search(r"\bCOMPONENTS\s+\d+\s*;(.*?)\bEND\s+COMPONENTS\b", def_text, re.I | re.S)
    placed_area = 0.0
    placed_cells = 0
    if component_section:
        for entry in re.findall(r"(?ms)^[ \t]*-[ \t]+.*?;", component_section.group(1)):
            head = re.match(r"(?s)\s*-\s+(\S+)\s+(\S+)(.*)", entry)
            if not head:
                continue
            cell_type, rest = head.group(2), head.group(3)
            if not re.search(r"\+\s*(?:PLACED|FIXED|COVER)\b", rest, re.I):
                continue
            size = macros.get(cell_type)
            if size:
                placed_area += size[0] * size[1]
                placed_cells += 1

    row_area = 0.0
    row_count = 0
    row_re = re.compile(
        rf"(?mi)^\s*ROW\s+\S+\s+(\S+)\s+{NUM}\s+{NUM}\s+\S+\s+DO\s+(\d+)\s+BY\s+(\d+)\s+STEP\s+{NUM}\s+{NUM}\s*;"
    )
    for row in row_re.finditer(def_text):
        site_name = row.group(1)
        dims = sites.get(site_name)
        if not dims:
            continue
        nx, ny = int(row.group(2)), int(row.group(3))
        row_area += nx * ny * dims[0] * dims[1]
        row_count += 1

    density = placed_area / row_area if placed_area > 0 and row_area > 0 else None
    return {
        "effective_place_density": density,
        "placed_cell_area_from_def_um2": placed_area if placed_area > 0 else None,
        "placement_row_area_um2": row_area if row_area > 0 else None,
        "effective_place_density_method": (
            f"placed LEF macro area / DEF placement-row area; "
            f"{placed_cells} placed cells, {row_count} rows"
            if density is not None else "unavailable: no placed cells or placement rows could be measured"
        ),
    }


def extract_netlist_features(path: Path, requested_top: str, design_id: str) -> dict[str, Any]:
    design = load_netlist(path)
    top = select_top(design["modules"], requested_top)
    module = design["modules"][top]
    cells = module.get("cells", {})
    ports = module.get("ports", {})
    netnames = module.get("netnames", {})
    if not isinstance(cells, dict):
        raise ValueError("Invalid cells object in Yosys JSON.")

    cell_types = Counter(str(c.get("type", "unknown")) for c in cells.values())
    classes = Counter()
    for cell_type, count in cell_types.items():
        classes[classify_cell(cell_type)] += count

    liberty_path = configured_path("LIBERTY")
    liberty_directions = load_liberty_pin_directions(liberty_path)
    all_bits: set[int] = set()
    sink_counts: Counter[int] = Counter()
    port_count_total = 0
    port_count_known = 0
    cells_with_any_directions = 0
    cells_with_complete_directions = 0

    for obj in list(netnames.values()) + list(ports.values()):
        for bit in obj.get("bits", []):
            if isinstance(bit, int) and not isinstance(bit, bool):
                all_bits.add(bit)

    for cell in cells.values():
        connections = cell.get("connections", {})
        directions = cell.get("port_directions", {})
        if not isinstance(directions, dict):
            directions = {}
        cell_type = str(cell.get("type", ""))
        liberty_cell = liberty_directions.get(cell_type, {})
        resolved = {**liberty_cell, **directions}  # Yosys metadata takes precedence.
        if resolved:
            cells_with_any_directions += 1
        complete = True
        for port_name, bits in connections.items():
            direction = str(resolved.get(port_name, "")).lower()
            port_count_total += 1
            if direction in {"input", "output", "inout"}:
                port_count_known += 1
            else:
                complete = False
            for bit in bits:
                if isinstance(bit, int) and not isinstance(bit, bool):
                    all_bits.add(bit)
                    if direction == "input":
                        sink_counts[bit] += 1
        if complete and connections:
            cells_with_complete_directions += 1

    coverage = port_count_known / port_count_total if port_count_total else 0.0
    complete_all = bool(cells) and cells_with_complete_directions == len(cells)
    fanouts = [sink_counts[b] for b in all_bits if sink_counts[b] > 0] if complete_all else []
    net_bit_total = sum(
        sum(isinstance(b, int) and not isinstance(b, bool) for b in n.get("bits", []))
        for n in netnames.values()
    )

    if complete_all:
        fanout_method = "Yosys port_directions with Liberty fallback; input-pin sink count per connected net bit"
        fanout_warning = ""
    else:
        fanout_method = "unavailable: some connected cell ports have no resolved direction"
        fanout_warning = (
            f"Resolved directions for {port_count_known}/{port_count_total} cell ports "
            f"({coverage:.1%}); complete directions for {cells_with_complete_directions}/{len(cells)} cells."
        )

    return {
        "design_id": design_id,
        "top_module": top,
        "netlist_json": str(path),
        "cell_count": len(cells),
        "netname_count": len(netnames),
        "port_count": len(ports),
        "port_bit_count": sum(len(p.get("bits", [])) for p in ports.values()),
        "sequential_cell_count": classes["sequential"],
        "combinational_cell_count": classes["combinational"],
        "buffer_inverter_count": classes["buffer_inverter"],
        "other_cell_count": classes["other"],
        "unique_cell_type_count": len(cell_types),
        "avg_bit_fanout": mean(fanouts) if fanouts else None,
        "max_bit_fanout": max(fanouts) if fanouts else None,
        "std_bit_fanout": pstdev(fanouts) if len(fanouts) > 1 else (0.0 if fanouts else None),
        "fanout_direction_metadata_found": cells_with_any_directions > 0,
        "fanout_direction_coverage_fraction": coverage,
        "fanout_direction_coverage_percent": coverage * 100.0,
        "fanout_metadata_complete_for_all_cells": complete_all,
        "fanout_cells_with_any_directions": cells_with_any_directions,
        "fanout_cells_with_complete_directions": cells_with_complete_directions,
        "fanout_total_cells": len(cells),
        "fanout_method": fanout_method,
        "fanout_quality_warning": fanout_warning,
        "total_net_bits": net_bit_total,
        "unique_cell_types": dict(cell_types),
        "feature_stage": "post-synthesis / pre-placement",
        "cell_classification_method": "heuristic cell-name matching; validate against Liberty",
    }


def timing_patterns() -> dict[str, list[str]]:
    return {
        "wns_setup_ns": [rf"\bworst\s+slack\s+max\s*[:=]?\s*({NUM})", rf"\bwns\s+max\s*[:=]?\s*({NUM})", rf"\bworst\s+slack(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})", rf"\bwns(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})"],
        "tns_setup_ns": [rf"\btotal\s+negative\s+slack\s*[:=]?\s*({NUM})", rf"\btns\s+max\s*[:=]?\s*({NUM})", rf"\btns(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})", rf"\btotal\s+negative\s+slack(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})"],
        "wns_hold_ns": [rf"\bworst\s+slack\s+min\s*[:=]?\s*({NUM})", rf"\bwns\s+min\s*[:=]?\s*({NUM})", rf"\bworst\s+slack(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})", rf"\bwns(?:\s*\([^)]*\))?\s*[:=]\s*({NUM})"],
    }


def parse_timing(text: str, metric: str) -> float | None:
    return last_number(text, timing_patterns()[metric])


def parse_def_routing(text: str) -> dict[str, Any]:
    section = re.search(r"\bNETS\s+\d+\s*;(.*?)\bEND\s+NETS\b", text, re.I | re.S)
    if not section: return {"def_nets_section_found": False, "routed_net_count": None, "route_segment_count": None}
    entries = re.findall(r"(?ms)^[ \t]*-[ \t]+.*?;", section.group(1)); routed = 0; clauses = 0
    for entry in entries:
        found = re.findall(r"\b(?:ROUTED|FIXED|COVER|NEW)\s+[A-Za-z0-9_.$/\[\]-]+", entry, re.I)
        if found: routed += 1
        clauses += len(re.findall(r"\b(?:ROUTED|NEW)\s+[A-Za-z0-9_.$/\[\]-]+", entry, re.I))
    return {"def_nets_section_found": True, "routed_net_count": routed, "route_segment_count": clauses}


def parse_route_log(text: str) -> dict[str, Any]:
    wire = last_number(text, [rf"\bTotal\s+wire\s+length\s*[:=]\s*({NUM})\s*(?:um|µm)\b", rf"\bTotal\s+wire\s+length\s*[:=]\s*({NUM})"])
    vias = last_number(text, [rf"\bTotal\s+number\s+of\s+vias\s*[:=]\s*({NUM})", rf"\bNumber\s+of\s+vias\s*[:=]\s*({NUM})"])
    viol = last_number(text, [rf"\bNumber\s+of\s+violations\s*[:=]\s*({NUM})", rf"\bTotal\s+violations\s*[:=]\s*({NUM})"])
    return {"wirelength_um": wire, "via_count": int(vias) if vias is not None else None, "final_detailed_route_violations": int(viol) if viol is not None else None}


def parse_congestion(text: str) -> float | None:
    """Parse explicit overflow metrics; never infer zero from missing data."""
    patterns = [
        rf"(?im)^\s*total\s+(?:routing\s+)?overflow\s*(?:[:=]|is|of)\s*({NUM})\b",
        rf"(?im)^\s*(?:global\s+)?routing\s+overflow\s*(?:[:=]|is|of)\s*({NUM})\b",
        rf"(?im)^\s*congestion\s+overflow\s*(?:[:=]|is|of)\s*({NUM})\b",
        rf"(?im)^\s*overflow\s*(?:[:=]|is|of)\s*({NUM})\b",
        rf"(?im)^.*?\btotal\s+overflow\s+(?:is|of)\s+({NUM})\b",
    ]
    value = last_number(text, patterns)
    if value is not None:
        return value
    if re.search(
        r"\b(?:no\s+overflowing\s+gcells?|zero\s+routing\s+overflow|total\s+overflow\s*(?:[:=]|is|of)\s*0(?:\.0*)?\b)",
        text,
        re.I,
    ):
        return 0.0
    return None


def parse_physical_log_values(text: str) -> dict[str, Any]:
    density = last_number(text, [
        rf"\buniform density\s+to\s+({NUM})",
        rf"\b(?:auto(?:matically)?\s+)?adjust(?:s|ed|ing)?\s+uniform density\s+to\s+({NUM})",
        rf"\beffective\s+(?:placement\s+)?density\s*[:=]\s*({NUM})",
        rf"\bachieved\s+(?:placement\s+)?density\s*[:=]\s*({NUM})",
    ])
    area = last_number(text, [
        rf"\bmovable instances area\s*[:=]?\s*({NUM})",
        rf"\bdesign area\s*[:=]?\s*({NUM})",
    ])
    return {"effective_place_density": density, "design_area_um2": area}


def find_report(run_dir: Path, candidates: list[str]) -> tuple[Path | None, str]:
    for candidate in candidates:
        paths = [run_dir / candidate] + sorted(run_dir.rglob(candidate))
        for path in paths:
            if path.is_file():
                content = read_text(path)
                if content.strip(): return path, content
    return None, ""


def physical_features(run_dir: Path) -> dict[str, Any]:
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    def_path = run_dir / "final.def"
    odb_path = run_dir / "final.odb"
    def_text = read_text(def_path)

    setup_path, setup_text = find_report(run_dir, [
        "wns_setup.rpt", "wns_setup_global_route.rpt", "timing_setup.rpt",
        "timing_setup_global_route.rpt", "setup_timing.rpt", "setup.rpt"
    ])
    tns_path, tns_text = find_report(run_dir, [
        "tns_setup.rpt", "tns_setup_global_route.rpt", "timing_setup.rpt",
        "timing_setup_global_route.rpt", "setup_timing.rpt", "setup.rpt"
    ])
    hold_path, hold_text = find_report(run_dir, [
        "wns_hold.rpt", "timing_hold.rpt", "wns_hold_global_route.rpt",
        "hold_timing.rpt", "hold.rpt"
    ])
    cong_path, cong_text = find_report(run_dir, [
        "congestion.rpt", "global_route_congestion.rpt", "routing_congestion.rpt", "grt_congestion.rpt"
    ])
    area_path, area_text = find_report(run_dir, ["design_area.rpt", "area.rpt"])

    logs = collect_logs(run_dir)
    route_name, route_text = choose_routing_log(logs)
    all_logs = "\n".join(logs.values())

    setup_src = setup_text or all_logs
    tns_src = tns_text or all_logs
    hold_src = hold_text or all_logs
    wns_setup = parse_timing(setup_src, "wns_setup_ns")
    tns_setup = parse_timing(tns_src, "tns_setup_ns")
    wns_hold = parse_timing(hold_src, "wns_hold_ns")
    route = parse_route_log(route_text)

    # First parse the designated congestion report. If it contains no recognized
    # numeric overflow, fall back to the OpenROAD logs rather than stopping here.
    congestion = parse_congestion(cong_text) if cong_text else None
    congestion_source = str(cong_path) if congestion is not None and cong_path else None
    if congestion is None:
        congestion = parse_congestion(all_logs)
        if congestion is not None:
            congestion_source = "logs"

    physical = parse_physical_log_values("\n".join([area_text, all_logs]))
    lef_path = configured_path("CELL_LEF")
    density_measurement = measure_density_from_def(def_text, lef_path)
    # Prefer a density reconstructed from final placement geometry. Otherwise use
    # an explicit achieved/effective density message, never the target argument.
    if density_measurement.get("effective_place_density") is None and physical.get("effective_place_density") is not None:
        density_measurement = {
            **density_measurement,
            "effective_place_density": physical["effective_place_density"],
            "effective_place_density_method": "explicit achieved/effective density reported in OpenROAD log",
        }

    def_counts = parse_def_routing(def_text) if def_text else {
        "def_nets_section_found": False, "routed_net_count": None, "route_segment_count": None
    }
    route_success = bool(re.search(
        r"SUCCESS:\s*Detailed routing|Complete detail routing|Detailed routing completed",
        all_logs, re.I
    ))
    def_ok = def_path.is_file() and def_path.stat().st_size > 0
    odb_ok = odb_path.is_file() and odb_path.stat().st_size > 0

    measurements = {
        "wns_setup_ns": "measured" if wns_setup is not None else "unavailable",
        "tns_setup_ns": "measured" if tns_setup is not None else "unavailable",
        "wns_hold_ns": "measured" if wns_hold is not None else "unavailable",
        "congestion_overflow": "measured" if congestion is not None else "unavailable",
        "wirelength_um": "measured" if route["wirelength_um"] is not None else "unavailable",
        "via_count": "measured" if route["via_count"] is not None else "unavailable",
        "final_detailed_route_violations": "measured" if route["final_detailed_route_violations"] is not None else "unavailable",
        "design_area_um2": "measured" if physical["design_area_um2"] is not None else "unavailable",
        "effective_place_density": "measured" if density_measurement["effective_place_density"] is not None else "unavailable",
    }
    return {
        "final_def_available": def_path.is_file(),
        "final_odb_available": odb_path.is_file(),
        "final_def_nonempty": bool(def_text.strip()),
        "artifacts_present": def_ok and odb_ok,
        "detailed_route_success_marker_found": route_success,
        **def_counts,
        **route,
        "effective_place_density": density_measurement["effective_place_density"],
        "placed_cell_area_from_def_um2": density_measurement.get("placed_cell_area_from_def_um2"),
        "placement_row_area_um2": density_measurement.get("placement_row_area_um2"),
        "effective_place_density_method": density_measurement.get("effective_place_density_method"),
        "design_area_um2": physical["design_area_um2"],
        "wns_setup_ns": wns_setup,
        "tns_setup_ns": tns_setup,
        "wns_hold_ns": wns_hold,
        "congestion_overflow": congestion,
        "setup_report_source": str(setup_path) if setup_path else ("logs" if setup_src else None),
        "tns_report_source": str(tns_path) if tns_path else ("logs" if tns_src else None),
        "hold_report_source": str(hold_path) if hold_path else ("logs" if hold_src else None),
        "congestion_report_source": congestion_source,
        "routing_log_source": route_name,
        "log_files_found": list(logs),
        "target_measurement_status": measurements,
        "note": (
            "DEF route counts are approximate. Cell classes are heuristic and should be validated against Liberty. "
            "Fanout uses Yosys directions with Liberty fallback. Congestion is extracted from a recognized overflow "
            "metric; missing overflow is unavailable, not zero. Effective density is measured from placed LEF macro "
            "area divided by DEF placement-row area when geometry is available. Router violations are not independent signoff DRC."
        ),
        "target_feature_stage": "post-placement/routing targets; exclude these outcomes from predictors when predicting the same outcomes",
    }


def read_tcl_status(run_dir: Path) -> tuple[str | None, str | None]:
    """Read the final Tcl status-history row if the flow generated one."""
    path = run_dir / "fir_features_status.csv"
    if not path.is_file():
        return None, None
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error, UnicodeError):
        return None, None
    if not rows:
        return None, None
    row = rows[-1]
    status = (row.get("status") or row.get("flow_status") or "").strip().upper()
    stage = (row.get("last_completed_stage") or row.get("stage") or "").strip()
    if not status and not stage:
        return None, None
    if status in {"COMPLETED", "SUCCESS", "SUCCEEDED"}:
        return "completed", stage or "flow_completed"
    if status in {"FAILED", "ERROR"}:
        return "failed", stage or "unknown"
    if status in {"IN_PROGRESS", "RUNNING", "STARTED"}:
        return "in_progress", stage or "unknown"
    return status.lower() if status else None, stage or None


def write_configuration_csv(outdir: Path, run_dir: Path, args: argparse.Namespace, post: dict[str, Any]) -> None:
    """Write one configuration row, preferring the Tcl stage-history record."""
    run_id = args.run_id or run_dir.name
    def_ok = bool(post["final_def_available"] and post["final_def_nonempty"])
    odb_ok = bool(post["final_odb_available"])
    route_success = bool(post["detailed_route_success_marker_found"])

    tcl_status, tcl_stage = read_tcl_status(run_dir)
    if tcl_status == "completed" and def_ok and odb_ok:
        flow_status, last_stage = "completed", tcl_stage or "flow_completed"
    elif tcl_status == "failed":
        flow_status, last_stage = "failed", tcl_stage or "unknown"
    elif tcl_status == "in_progress":
        flow_status, last_stage = "in_progress", tcl_stage or "unknown"
    elif route_success and def_ok and odb_ok:
        flow_status, last_stage = "detailed_routing_completed", "detailed_routing"
    elif def_ok and odb_ok:
        flow_status, last_stage = "artifacts_present_unverified", "unknown"
    elif def_ok or odb_ok:
        flow_status, last_stage = "partial_artifacts", "unknown"
    elif post.get("log_files_found"):
        flow_status, last_stage = "flow_incomplete_or_unverified", "unknown"
    else:
        flow_status, last_stage = "no_artifacts_or_logs", "unknown"

    fields = ["run_id", "taps", "data_width", "coeff_width", "acc_width", "utilization", "place_density", "clk_period_ns", "flow_status", "last_completed_stage"]
    row = {
        "run_id": run_id, "taps": args.taps, "data_width": args.data_width,
        "coeff_width": args.coeff_width, "acc_width": args.acc_width,
        "utilization": args.utilization, "place_density": args.place_density,
        "clk_period_ns": args.clock_period, "flow_status": flow_status,
        "last_completed_stage": last_stage,
    }
    write_rows_csv(outdir / "fir_features.csv", fields, row)
    print(
        f"Detected flow status: {flow_status}\nLast completed stage: {last_stage}\n"
        f"Routing success marker: {route_success}\nFinal DEF nonempty: {def_ok}\nFinal ODB nonempty: {odb_ok}"
    )



DISPLAY_NAMES = {
    "design_id": "Design ID", "top_module": "Top module", "netlist_json": "Netlist JSON",
    "cell_count": "Cell count", "netname_count": "Net names", "port_count": "Port count",
    "port_bit_count": "Port bits", "sequential_cell_count": "Sequential cells",
    "combinational_cell_count": "Combinational cells", "buffer_inverter_count": "Buffers / inverters",
    "other_cell_count": "Other cells", "unique_cell_type_count": "Unique cell types",
    "avg_bit_fanout": "Average bit fanout", "max_bit_fanout": "Maximum bit fanout",
    "std_bit_fanout": "Bit fanout standard deviation", "fanout_direction_metadata_found": "Fanout direction metadata found",
    "fanout_method": "Fanout extraction method", "fanout_quality_warning": "Fanout quality warning",
    "fanout_direction_coverage_fraction": "Fanout direction coverage fraction",
    "fanout_direction_coverage_percent": "Fanout direction coverage (%)",
    "fanout_metadata_complete_for_all_cells": "Fanout metadata complete",
    "fanout_cells_with_any_directions": "Cells with any direction metadata",
    "fanout_cells_with_complete_directions": "Cells with complete direction metadata",
    "fanout_total_cells": "Cells checked for fanout", "total_net_bits": "Total net bits",
    "feature_stage": "Feature stage", "cell_classification_method": "Cell classification method",
    "utilization_constraint_percent": "Utilization constraint (%)", "clock_period_ns": "Clock period (ns)",
    "final_def_available": "Final DEF available", "final_odb_available": "Final ODB available",
    "final_def_nonempty": "Final DEF non-empty", "artifacts_present": "Final DEF and ODB present",
    "detailed_route_success_marker_found": "Detailed routing success marker found",
    "def_nets_section_found": "DEF NETS section found", "routed_net_count": "Routed nets (approx.)",
    "route_segment_count": "Route segments (approx.)", "wirelength_um": "Wirelength (µm)",
    "via_count": "Via count", "final_detailed_route_violations": "Detailed-route violations (reported)",
    "effective_place_density": "Effective placement density", "effective_place_density_method": "Effective density measurement method",
    "placed_cell_area_from_def_um2": "Placed cell area from DEF (µm²)", "placement_row_area_um2": "Placement row area (µm²)",
    "design_area_um2": "Design area (µm²)",
    "wns_setup_ns": "Setup WNS (ns)", "tns_setup_ns": "Setup TNS (ns)",
    "wns_hold_ns": "Hold WNS (ns)", "congestion_overflow": "Congestion overflow",
    "setup_report_source": "Setup timing report source", "tns_report_source": "TNS report source",
    "hold_report_source": "Hold timing report source", "congestion_report_source": "Congestion report source",
    "routing_log_source": "Routing log source", "note": "Notes / limitations",
    "target_feature_stage": "Target stage / ML leakage warning", "run_id": "Run ID",
    "taps": "FIR taps", "data_width": "Data width (bits)", "coeff_width": "Coefficient width (bits)",
    "acc_width": "Accumulator width (bits)", "utilization": "Utilization (%)",
    "place_density": "Placement density", "clk_period_ns": "Clock period (ns)",
    "flow_status": "Flow status", "last_completed_stage": "Last completed stage",
}


def display_value(value: Any) -> str:
    """Human-readable value; unavailable measurements are never represented as zero."""
    if value is None or value == "":
        return "N/A (not extracted)"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list, tuple)):
        if isinstance(value, dict):
            return "; ".join(f"{DISPLAY_NAMES.get(k, k.replace('_', ' ').title())}: {display_value(v)}" for k, v in value.items())
        return ", ".join(str(v) for v in value) if value else "N/A"
    return str(value)


def markdown_table(rows: list[tuple[str, Any]]) -> str:
    lines = ["| Metric | Value |", "|---|---|"]
    for label, value in rows:
        safe_label = str(label).replace("|", r"\|").replace("\n", " ")
        safe_value = display_value(value).replace("|", r"\|").replace("\n", "<br>")
        lines.append(f"| {safe_label} | {safe_value} |")
    return "\n".join(lines)


def write_human_report(outdir: Path, pre: dict[str, Any], post: dict[str, Any], args: argparse.Namespace, run_dir: Path) -> None:
    """Write a compact Markdown report with one metric per row."""
    run_id = args.run_id or run_dir.name
    config = [
        ("Run ID", run_id), ("Design ID", args.design_id), ("Top module", args.top),
        ("FIR taps", args.taps), ("Data width (bits)", args.data_width),
        ("Coefficient width (bits)", args.coeff_width), ("Accumulator width (bits)", args.acc_width),
        ("Utilization constraint (%)", args.utilization), ("Placement density", args.place_density),
        ("Clock period (ns)", args.clock_period),
    ]
    tcl_status, tcl_stage = read_tcl_status(run_dir)
    config.extend([("Flow status", tcl_status or "derived from artifacts/logs"), ("Last completed stage", tcl_stage or "derived from artifacts/logs")])
    tcl_status, tcl_stage = read_tcl_status(run_dir)
    if tcl_status == "completed" and post.get("artifacts_present"):
        flow_status = "completed"
    elif tcl_status == "failed":
        flow_status = "failed"
    elif post.get("detailed_route_success_marker_found") and post.get("final_def_nonempty") and post.get("final_odb_available"):
        flow_status = "detailed_routing_completed"
    elif post.get("artifacts_present"):
        flow_status = "artifacts_present_unverified"
    else:
        flow_status = "incomplete_or_unverified"
    physical_keys = [
        "final_def_available", "final_odb_available", "final_def_nonempty", "artifacts_present",
        "detailed_route_success_marker_found", "routed_net_count", "route_segment_count", "wirelength_um",
        "via_count", "final_detailed_route_violations", "effective_place_density",
        "effective_place_density_method", "placed_cell_area_from_def_um2", "placement_row_area_um2",
        "design_area_um2", "wns_setup_ns", "tns_setup_ns", "wns_hold_ns", "congestion_overflow",
    ]
    synthesis_keys = [
        "cell_count", "netname_count", "port_count", "port_bit_count", "sequential_cell_count",
        "combinational_cell_count", "buffer_inverter_count", "other_cell_count", "unique_cell_type_count",
        "avg_bit_fanout", "max_bit_fanout", "std_bit_fanout", "total_net_bits",
        "fanout_direction_metadata_found", "fanout_direction_coverage_percent",
        "fanout_metadata_complete_for_all_cells", "fanout_method", "fanout_quality_warning",
        "cell_classification_method",
    ]
    lines = [
        "# FIR Physical-Design Feature Report", "", f"**Run:** `{run_id}`  ",
        f"**Detected flow status:** `{flow_status}`  ", f"**Run directory:** `{run_dir}`", "",
        "## 1. Design configuration", "", markdown_table(config), "",
        "## 2. Synthesis / netlist features", "",
        markdown_table([(DISPLAY_NAMES.get(k, k.replace("_", " ").title()), pre.get(k)) for k in synthesis_keys]), "",
        "## 3. Physical-design results", "",
        markdown_table([(DISPLAY_NAMES.get(k, k.replace("_", " ").title()), post.get(k)) for k in physical_keys]), "",
        "## 4. Measurement sources and status", "",
        markdown_table([(DISPLAY_NAMES.get(k, k.replace("_", " ").title()), post.get(k)) for k in ("setup_report_source", "tns_report_source", "hold_report_source", "congestion_report_source", "routing_log_source", "target_feature_stage", "note")]), "",
        "## 5. Interpretation notes", "",
        "- `N/A (not extracted)` means the extractor did not find a trustworthy value; it does **not** mean zero.",
        "- Routed-net and route-segment counts parsed from DEF are approximate.",
        "- Cell-type classifications are heuristic and should be validated against Liberty metadata.",
        "- Timing and routing results are post-placement/routing targets. Exclude a target from input features when predicting that same target to prevent data leakage.",
        "- Zero reported detailed-route violations does not by itself prove full signoff DRC cleanliness.", "",
        "## 6. Output files", "",
        "| File | Purpose |", "|---|---|",
        "| `preplacement_features.csv` | Machine-readable synthesis features |",
        "| `physical_targets.csv` | Machine-readable physical-design targets |",
        "| `features.csv` | Combined raw feature/target record |",
        "| `fir_features.csv` | Compact design configuration and flow status |",
        "| `fir_feature_report.xlsx` | Formatted Excel workbook, if `openpyxl` is installed |",
        "| `run_report.md` | Readable report for GitHub |", "",
    ]
    (outdir / "run_report.md").write_text("\n".join(lines), encoding="utf-8")


def write_excel_workbook(outdir: Path, pre: dict[str, Any], post: dict[str, Any], combined: dict[str, Any], args: argparse.Namespace, run_dir: Path) -> bool:
    """Create a formatted XLSX workbook; return False if openpyxl is unavailable."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("WARNING: openpyxl is not installed; XLSX not created. Install it with: python -m pip install openpyxl", file=sys.stderr)
        return False

    workbook = Workbook()
    config_ws = workbook.active
    config_ws.title = "Configuration"
    config = {
        "Run ID": args.run_id or run_dir.name, "Design ID": args.design_id, "Top module": args.top,
        "Netlist JSON": str(Path(args.netlist_json)), "Run directory": str(run_dir),
        "FIR taps": args.taps, "Data width (bits)": args.data_width,
        "Coefficient width (bits)": args.coeff_width, "Accumulator width (bits)": args.acc_width,
        "Utilization constraint (%)": args.utilization, "Placement density": args.place_density,
        "Clock period (ns)": args.clock_period,
        "Flow status": (
            "completed" if read_tcl_status(run_dir)[0] == "completed" and post.get("artifacts_present")
            else "failed" if read_tcl_status(run_dir)[0] == "failed"
            else "detailed_routing_completed" if post.get("detailed_route_success_marker_found") and post.get("final_def_nonempty") and post.get("final_odb_available")
            else "artifacts_present_unverified" if post.get("artifacts_present")
            else "incomplete_or_unverified"
        ),
    }

    def add_key_value_sheet(ws, mapping: dict[str, Any], title: str) -> None:
        ws.append([title, "Value"])
        for key, value in mapping.items():
            label = DISPLAY_NAMES.get(key, key.replace("_", " ").title())
            if value is None or value == "": value = "N/A (not extracted)"
            elif isinstance(value, (dict, list, tuple)): value = display_value(value)
            ws.append([label, value])
        style_sheet(ws, header_row=1)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    def style_sheet(ws, header_row: int = 1) -> None:
        fill = PatternFill("solid", fgColor="1F4E78")
        header_font = Font(color="FFFFFF", bold=True)
        thin_gray = Side(style="thin", color="D9E2F3")
        for cell in ws[header_row]:
            cell.fill = fill; cell.font = header_font; cell.alignment = Alignment(vertical="center", wrap_text=True)
        for row in ws.iter_rows(min_row=header_row + 1):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                cell.border = Border(bottom=thin_gray)
        ws.row_dimensions[header_row].height = 24
        for col_cells in ws.columns:
            col_letter = get_column_letter(col_cells[0].column)
            max_len = max((len(str(c.value)) if c.value is not None else 0 for c in col_cells), default=10)
            ws.column_dimensions[col_letter].width = min(max(max_len + 2, 16), 52)

    add_key_value_sheet(config_ws, config, "Parameter")
    synthesis_ws = workbook.create_sheet("Synthesis Features")
    add_key_value_sheet(synthesis_ws, pre, "Feature")
    physical_ws = workbook.create_sheet("Physical Targets")
    add_key_value_sheet(physical_ws, post, "Metric")
    combined_ws = workbook.create_sheet("Combined Data")
    add_key_value_sheet(combined_ws, combined, "Field")
    for ws in workbook.worksheets:
        ws.sheet_view.showGridLines = False
    workbook.save(outdir / "fir_feature_report.xlsx")
    return True

def main() -> int:
    parser = argparse.ArgumentParser(description="Extract FIR netlist features and OpenROAD physical targets.")
    parser.add_argument("--netlist-json", default="fir_project/synth/fir_filter_netlist.json")
    parser.add_argument("--run-dir", default="fir_project/runs/fir_8tap_w8_util40_clk10")
    parser.add_argument("--outdir", default=None); parser.add_argument("--top", default="fir_filter")
    parser.add_argument("--design-id", default="fir_filter_8tap"); parser.add_argument("--run-id", default=None)
    parser.add_argument("--taps", type=int, default=None); parser.add_argument("--data-width", type=int, default=None)
    parser.add_argument("--coeff-width", type=int, default=None); parser.add_argument("--acc-width", type=int, default=None)
    parser.add_argument("--utilization", type=float, default=None); parser.add_argument("--place-density", type=float, default=None)
    parser.add_argument("--clock-period", type=float, default=None)
    parser.add_argument("--flow-status", default=None, help="Deprecated; detected status is used instead")
    args = parser.parse_args(); run_dir = Path(args.run_dir); outdir = Path(args.outdir) if args.outdir else run_dir; netlist_path = Path(args.netlist_json)
    if args.utilization is not None and not (0 < args.utilization <= 100): parser.error("--utilization must be > 0 and <= 100.")
    if args.place_density is not None and not (0 < args.place_density <= 1): parser.error("--place-density must be > 0 and <= 1.")
    if args.clock_period is not None and args.clock_period <= 0: parser.error("--clock-period must be positive.")
    for name in ("taps", "data_width", "coeff_width", "acc_width"):
        value = getattr(args, name)
        if value is not None and value <= 0: parser.error(f"--{name.replace('_', '-')} must be positive.")
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        pre = extract_netlist_features(netlist_path, args.top, args.design_id)
        pre["utilization_constraint_percent"] = args.utilization; pre["clock_period_ns"] = args.clock_period
        post = physical_features(run_dir)
        write_json(outdir / "preplacement_features.json", pre); write_csv(outdir / "preplacement_features.csv", pre)
        write_json(outdir / "physical_targets.json", post); write_csv(outdir / "physical_targets.csv", post)
        combined = {**pre, **post}; write_json(outdir / "features.json", combined); write_csv(outdir / "features.csv", combined)
        write_configuration_csv(outdir, run_dir, args, post)
        write_human_report(outdir, pre, post, args, run_dir)
        xlsx_created = write_excel_workbook(outdir, pre, post, combined, args, run_dir)
        status = {"design_id": args.design_id, "run_id": args.run_id or run_dir.name, "netlist_json": str(netlist_path), "run_dir": str(run_dir), "output_dir": str(outdir), "fanout_direction_metadata_found": pre["fanout_direction_metadata_found"], "final_def_available": post["final_def_available"], "final_odb_available": post["final_odb_available"], "artifacts_present": post["artifacts_present"], "detailed_route_success_marker_found": post["detailed_route_success_marker_found"], "target_measurement_status": post["target_measurement_status"], "markdown_report_created": True, "excel_workbook_created": xlsx_created}
        write_json(outdir / "extraction_status.json", status)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1
    print("\n===== FIR Feature Extraction =====")
    print(f"Run directory : {run_dir}\nOutput directory: {outdir}\nCell count    : {pre['cell_count']}\nSequential    : {pre['sequential_cell_count']}\nCombinational : {pre['combinational_cell_count']}\nFanout method : {pre['fanout_method']}\nFinal DEF     : {post['final_def_available']}\nFinal ODB     : {post['final_odb_available']}\nRouting success marker: {post['detailed_route_success_marker_found']}")
    print("\n===== Physical Targets =====")
    for key in ("wns_setup_ns", "tns_setup_ns", "wns_hold_ns", "congestion_overflow", "wirelength_um", "via_count", "final_detailed_route_violations", "design_area_um2", "effective_place_density"):
        print(f"{key}: {post.get(key)}")
    print("\n===== Target Status =====")
    for key, value in post["target_measurement_status"].items(): print(f"{key}: {value}")
    print("\nGenerated: preplacement_features.csv, physical_targets.csv, features.csv, fir_features.csv, run_report.md, extraction_status.json")
    print(f"Excel workbook: {'fir_feature_report.xlsx' if xlsx_created else 'not created (install openpyxl)'}")
    print("\nExtraction finished.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

