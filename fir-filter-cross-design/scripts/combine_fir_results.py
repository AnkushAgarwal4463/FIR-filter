#!/usr/bin/env python3
"""Combine FIR run artifacts into a complete expected-configuration CSV.

Missing/failed configurations remain explicit rows. Missing measurements remain
blank; this script never converts unavailable values to zero.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

BASE_CONFIGS = [
    (taps, width, util)
    for taps, widths in ((4, (8, 16)), (8, (8, 16)), (12, (8,)))
    for width in widths
    for util in (20, 30, 40, 50)
]
DEFAULT_CLOCK_PERIODS = (5.0, 7.5, 10.0, 12.5, 15.0)

CORE_COLUMNS = [
    "run_id", "configuration_status", "job_outcome",
    "last_completed_stage", "failure_reason", "feature_artifact_status",
    "taps", "data_width", "coeff_width", "acc_width", "utilization",
    "place_density", "clock_period", "clock_period_ns", "frequency_mhz",
]


def fmt(value: float) -> str:
    return f"{float(value):g}"


def make_run_id(taps: int, width: int, util: int, period: float) -> str:
    acc_width = width + 8 + 8
    return (
        f"fir_t{taps}_dw{width}_cw8_aw{acc_width}"
        f"_u{util}_clk{fmt(period)}"
    )


def read_csv_row(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        return rows[0] if rows else None
    except (OSError, csv.Error):
        return None


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def load_artifacts(root: Path):
    features: dict[str, dict[str, Any]] = {}
    statuses: dict[str, dict[str, Any]] = {}

    if not root.exists():
        return features, statuses

    for path in root.rglob("features.csv"):
        row = read_csv_row(path)
        if not row:
            continue
        run_id = row.get("run_id") or path.parent.name
        if run_id:
            features.setdefault(str(run_id), row)

    for path in root.rglob("*.json"):
        if path.parent.name == "matrix-status" or path.name.endswith(".json"):
            record = read_json(path)
            if not record:
                continue
            run_id = record.get("run_id") or path.stem
            if run_id and (
                "job_outcome" in record
                or "last_completed_stage" in record
                or "failure_reason" in record
            ):
                statuses.setdefault(str(run_id), record)

    return features, statuses


def expected_runs(args):
    if args.run_mode == "single":
        configs = [(args.single_taps, args.single_data_width, args.single_utilization)]
        periods = [args.single_clock_period]
    else:
        configs = BASE_CONFIGS
        periods = args.clock_periods

    rows = []
    for taps, width, util in configs:
        for period in periods:
            rows.append((int(taps), int(width), int(util), float(period)))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-mode", choices=("sweep", "single"), default="sweep")
    parser.add_argument("--single-taps", type=int, default=8)
    parser.add_argument("--single-data-width", type=int, default=8)
    parser.add_argument("--single-utilization", type=int, default=40)
    parser.add_argument("--single-clock-period", type=float, default=10.0)
    parser.add_argument(
        "--clock-periods",
        default="5,7.5,10,12.5,15",
        help="Comma-separated clock periods in ns for sweep mode.",
    )
    args = parser.parse_args()

    if args.run_mode == "sweep":
        try:
            args.clock_periods = [float(x.strip()) for x in args.clock_periods.split(",") if x.strip()]
        except ValueError:
            parser.error("--clock-periods must be comma-separated numbers.")
        if not args.clock_periods or any(x <= 0 for x in args.clock_periods):
            parser.error("All clock periods must be greater than zero.")
    else:
        if args.single_clock_period <= 0:
            parser.error("--single-clock-period must be greater than zero.")
        args.clock_periods = [args.single_clock_period]

    features, statuses = load_artifacts(args.artifacts)
    expected = expected_runs(args)

    rows: list[dict[str, Any]] = []
    extra_columns: set[str] = set()

    for taps, width, util, period in expected:
        acc_width = width + 8 + 8
        run_id = make_run_id(taps, width, util, period)
        feature = features.get(run_id, {})
        status = statuses.get(run_id, {})

        feature_received = bool(feature)
        outcome = status.get("job_outcome", "")
        stage = status.get("last_completed_stage", "")
        failure = status.get("failure_reason", "")

        if feature_received and (outcome == "success" or stage == "completed"):
            config_status = "completed"
        elif feature_received:
            config_status = "features_received_status_unconfirmed"
        elif outcome:
            config_status = "failed_or_incomplete"
        else:
            config_status = "missing_artifact"

        row = {
            "run_id": run_id,
            "configuration_status": config_status,
            "job_outcome": outcome,
            "last_completed_stage": stage,
            "failure_reason": failure,
            "feature_artifact_status": "received" if feature_received else "missing",
            "taps": taps,
            "data_width": width,
            "coeff_width": 8,
            "acc_width": acc_width,
            "utilization": util,
            "place_density": util / 100.0,
            "clock_period": period,
            "clock_period_ns": period,
            "frequency_mhz": 1000.0 / period,
        }

        # Preserve the extractor's actual columns. Canonical configuration
        # fields above take precedence over duplicate feature-file metadata.
        for key, value in feature.items():
            if key not in row:
                row[key] = value
                extra_columns.add(key)
        rows.append(row)

    columns = CORE_COLUMNS + sorted(extra_columns - set(CORE_COLUMNS))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    completed = sum(r["configuration_status"] == "completed" for r in rows)
    print(f"Expected configurations: {len(rows)}")
    print(f"Feature artifacts received: {sum(r['feature_artifact_status'] == 'received' for r in rows)}")
    print(f"Completed configurations confirmed: {completed}")
    print(f"Combined CSV: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

