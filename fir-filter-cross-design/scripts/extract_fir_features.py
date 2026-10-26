#!/usr/bin/env python3
"""Extract pre-placement netlist features and separately recorded physical targets.

This intentionally does not fill unavailable timing/congestion/wire metrics with zero.
Keep predictors (netlist + known constraints) separate from post-placement targets to
avoid target leakage in the ML experiment.
"""
from __future__ import annotations
import argparse, csv, json, math, re
from collections import Counter
from pathlib import Path
from statistics import mean, pstdev

SEQ_RE = re.compile(r'(?:df|ed|sd|dfr|dfrtp|dfrbp|dfrtn|sdf|ff|latch)', re.I)
BUF_RE = re.compile(r'(?:buf|inv|clkbuf)', re.I)
COMB_RE = re.compile(r'(?:and|or|xor|xnor|mux|maj|fa|ha|a[oi]i|o[ai]|nand|nor|add|sub|mul|alu|logic|cmp)', re.I)
NUM = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'

def read_text(path: Path) -> str:
    try: return path.read_text(errors='ignore') if path.exists() else ''
    except OSError: return ''

def classify(name: str) -> str:
    s = name.lower().lstrip('\\')
    if SEQ_RE.search(s): return 'sequential'
    if BUF_RE.search(s): return 'buffer_inverter'
    if COMB_RE.search(s): return 'combinational'
    return 'other'

def netlist_features(path: Path, top: str) -> dict:
    design = json.loads(path.read_text())
    modules = design.get('modules', {})
    if top not in modules:
        marked = [n for n, m in modules.items() if str(m.get('attributes', {}).get('top', '')).strip() in ('1', "'1")]
        if marked: top = marked[0]
        elif len(modules) == 1: top = next(iter(modules))
        else: raise ValueError(f"Top '{top}' not found. Available modules: {list(modules)}")
    mod = modules[top]
    cells = mod.get('cells', {})
    ports = mod.get('ports', {})
    cell_types = Counter(str(c.get('type', 'unknown')) for c in cells.values())
    classes = Counter(classify(t) for t in cell_types for _ in range(cell_types[t]))
    bit_sinks = Counter(); all_bits = set(); net_bits = []
    for net in mod.get('netnames', {}).values():
        bits = [b for b in net.get('bits', []) if isinstance(b, int)]
        all_bits.update(bits); net_bits.append(len(bits))
    for cell in cells.values():
        for p, bits in cell.get('connections', {}).items():
            if cell.get('port_directions', {}).get(p, 'input') == 'input':
                for b in bits:
                    if isinstance(b, int): bit_sinks[b] += 1
    fanouts = [bit_sinks[b] for b in all_bits if bit_sinks[b] > 0]
    port_width = sum(len(p.get('bits', [])) for p in ports.values())
    return {
        'design_id': 'fir_filter_8tap', 'top_module': top,
        'cell_count': len(cells), 'netname_count': len(mod.get('netnames', {})),
        'port_count': len(ports), 'port_bit_count': port_width,
        'sequential_cell_count': classes['sequential'],
        'combinational_cell_count': classes['combinational'],
        'buffer_inverter_count': classes['buffer_inverter'], 'other_cell_count': classes['other'],
        'unique_cell_type_count': len(cell_types),
        'avg_bit_fanout': mean(fanouts) if fanouts else None,
        'max_bit_fanout': max(fanouts) if fanouts else None,
        'std_bit_fanout': pstdev(fanouts) if len(fanouts) > 1 else (0.0 if fanouts else None),
        'total_net_bits': sum(net_bits),
        'unique_cell_types': dict(cell_types),
        'feature_stage': 'post-synthesis / pre-placement',
    }

def parse_report_number(text: str, patterns: list[str]):
    for pattern in patterns:
        m = re.search(pattern, text, re.I | re.M)
        if m:
            try:
                v = float(m.group(1))
                if math.isfinite(v): return v
            except (TypeError, ValueError): pass
    return None

def physical_features(run_dir: Path) -> dict:
    def_text = read_text(run_dir / 'final.def')
    setup = read_text(run_dir / 'timing_setup.rpt') or read_text(run_dir / 'timing_setup_global_route.rpt')
    hold = read_text(run_dir / 'timing_hold.rpt')
    cong = read_text(run_dir / 'congestion.rpt')
    log = read_text(run_dir / 'openroad_log.txt')
    # DEF route geometry can contain '*' coordinate reuse; this lightweight summary counts
    # routed statements/segments but deliberately does not claim wirelength without parsing it.
    nets_match = re.search(r'\bNETS\s+\d+\s*;(.*?)\bEND\s+NETS', def_text, re.S | re.I)
    net_text = nets_match.group(1) if nets_match else ''
    routed_statements = re.findall(r'\b(?:ROUTED|FIXED)\b.*?;', net_text, re.S | re.I)
    segment_count = len(re.findall(r'\b(?:ROUTED|NEW)\s+[A-Za-z0-9_]+', net_text, re.I))
    wns_setup = parse_report_number(setup, [r'worst\s+slack\s*[:=]\s*(' + NUM + r')', r'wns\s*[:=]\s*(' + NUM + r')', r' slack\s+(' + NUM + r')'])
    tns_setup = parse_report_number(setup, [r'total\s+negative\s+slack\s*[:=]\s*(' + NUM + r')', r'tns\s*[:=]\s*(' + NUM + r')'])
    wns_hold = parse_report_number(hold, [r'worst\s+slack\s*[:=]\s*(' + NUM + r')', r'wns\s*[:=]\s*(' + NUM + r')'])
    congestion = parse_report_number(cong, [r'(?:total\s+)?overflow\s*[:=]\s*(' + NUM + r')', r'congestion\s*[:=]\s*(' + NUM + r')'])
    if congestion is None and re.search(r'no\s+overflowing\s+gcell|zero\s+routing\s+overflow', cong + '\n' + log, re.I): congestion = 0.0
    return {
        'final_def_available': bool(def_text), 'final_odb_available': (run_dir / 'final.odb').exists(),
        'routed_net_count': len(routed_statements) if nets_match else None,
        'route_segment_count': segment_count if nets_match else None,
        'wns_setup_ns': wns_setup, 'tns_setup_ns': tns_setup, 'wns_hold_ns': wns_hold,
        'congestion_overflow': congestion,
        'wirelength_um': None,
        'note': 'wirelength_um remains null unless measured with a validated DEF geometry parser or OpenROAD report.',
        'feature_stage': 'post-placement/routing targets; do not use as inputs when predicting these same outcomes',
    }

def write_csv(path: Path, data: dict):
    flat = {k: v for k, v in data.items() if not isinstance(v, (dict, list))}
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(flat)); w.writeheader(); w.writerow(flat)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--netlist-json', default='fir_project/synth/fir_filter_netlist.json')
    p.add_argument('--run-dir', default='fir_project/runs/fir_8tap_w8_util40_clk10')
    p.add_argument('--outdir', default=None)
    p.add_argument('--top', default='fir_filter')
    p.add_argument('--utilization', type=float, default=None)
    p.add_argument('--clock-period', type=float, default=None)
    a = p.parse_args()
    outdir = Path(a.outdir) if a.outdir else Path(a.run_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    pre = netlist_features(Path(a.netlist_json), a.top)
    pre['utilization_constraint_percent'] = a.utilization
    pre['clock_period_ns'] = a.clock_period
    post = physical_features(Path(a.run_dir))
    (outdir / 'preplacement_features.json').write_text(json.dumps(pre, indent=2))
    (outdir / 'physical_targets.json').write_text(json.dumps(post, indent=2))
    write_csv(outdir / 'preplacement_features.csv', pre)
    write_csv(outdir / 'physical_targets.csv', post)
    combined = {**pre, **post}
    (outdir / 'features.json').write_text(json.dumps(combined, indent=2))
    write_csv(outdir / 'features.csv', combined)
    missing = [k for k in ('cell_count', 'sequential_cell_count', 'avg_bit_fanout') if pre.get(k) is None]
    if missing: print('WARNING: missing structural features:', ', '.join(missing))
    print('Wrote preplacement features and physical targets under', outdir)
    print('Review physical_targets.json; unavailable values remain null, not fabricated zeroes.')

if __name__ == '__main__': main()
