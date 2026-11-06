
#!/usr/bin/env python3
"""Combine FIR run artifacts into a complete expected-configuration CSV.

Supports both features.csv and fir_features.csv.
Matches run IDs with or without the _tr0/_tr1 suffix.
Missing configurations remain explicit rows.
Missing measurements remain blank; unavailable values are never converted to zero.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
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
    "run_id",
    "configuration_status",
    "job_outcome",
    "last_completed_stage",
    "failure_reason",
    "feature_artifact_status",
    "feature_source_file",
    "status_source_file",
    "taps",
    "data_width",
    "coeff_width",
    "acc_width",
    "utilization",
    "place_density",
    "clock_period",
    "clock_period_ns",
    "frequency_mhz",
]


def fmt(value: float) -> str:
    return f"{float(value):g}"


def make_run_id(
    taps: int,
    width: int,
    util: int,
    period: float,
) -> str:
    acc_width = width + 8 + 8
    return (
        f"fir_t{taps}_dw{width}_cw8_aw{acc_width}"
        f"_u{util}_clk{fmt(period)}"
    )


def normalize_run_id(run_id: str) -> str:
    """Remove diagnostic suffixes to obtain the base configuration ID."""
    return re.sub(r"_tr[01]$", "", str(run_id).strip())


def read_csv_row(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            row = next(reader, None)
            return row
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

    feature_sources: dict[str, str] = {}
    status_sources: dict[str, str] = {}

    if not root.exists():
        print(f"WARNING: Artifact directory does not exist: {root}")
        return features, statuses, feature_sources, status_sources

    # Support both known feature extractor output filenames.
    feature_paths = sorted(
        path
        for path in root.rglob("*.csv")
        if path.name.lower() in {"features.csv", "fir_features.csv"}
    )

    for path in feature_paths:
        row = read_csv_row(path)
        if not row:
            continue

        raw_id = row.get("run_id") or path.parent.name
        run_id = normalize_run_id(str(raw_id))

        if not run_id:
            continue

        # Keep the first valid feature file found for each configuration.
        if run_id not in features:
            features[run_id] = row
            feature_sources[run_id] = str(path)

    # Read status records from matrix-status and other JSON artifacts.
    for path in sorted(root.rglob("*.json")):
        record = read_json(path)
        if not record:
            continue

        if not any(
            key in record
            for key in (
                "job_outcome",
                "last_completed_stage",
                "failure_reason",
            )
        ):
            continue

        raw_id = record.get("run_id") or path.stem
        run_id = normalize_run_id(str(raw_id))

        if not run_id:
            continue

        if run_id not in statuses:
            statuses[run_id] = record
            status_sources[run_id] = str(path)

    return features, statuses, feature_sources, status_sources


def expected_runs(args):
    if args.run_mode == "single":
        configs = [
            (
                args.single_taps,
                args.single_data_width,
                args.single_utilization,
            )
        ]
        periods = [args.single_clock_period]
    else:
        configs = BASE_CONFIGS
        periods = args.clock_periods

    return [
        (int(taps), int(width), int(util), float(period))
        for taps, width, util in configs
        for period in periods
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Combine FIR physical-design run artifacts."
    )

    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)

    parser.add_argument(
        "--run-mode",
        choices=("sweep", "single"),
        default="sweep",
    )

    parser.add_argument("--single-taps", type=int, default=8)
    parser.add_argument("--single-data-width", type=int, default=8)
    parser.add_argument("--single-utilization", type=int, default=40)
    parser.add_argument("--single-clock-period", type=float, default=10.0)

    parser.add_argument(
        "--clock-periods",
        default="5,7.5,10,12.5,15",
        help="Comma-separated clock periods in ns.",
    )

    args = parser.parse_args()

    if args.run_mode == "sweep":
        try:
            args.clock_periods = [
                float(value.strip())
                for value in args.clock_periods.split(",")
                if value.strip()
            ]
        except ValueError:
            parser.error("--clock-periods must contain valid numbers.")

        if not args.clock_periods or any(
            period <= 0 for period in args.clock_periods
        ):
            parser.error("All clock periods must be greater than zero.")

    else:
        if (
            args.single_taps <= 0
            or args.single_data_width <= 0
            or args.single_utilization <= 0
            or args.single_clock_period <= 0
        ):
            parser.error("Single-run configuration values must be positive.")

        args.clock_periods = [args.single_clock_period]

    (
        features,
        statuses,
        feature_sources,
        status_sources,
    ) = load_artifacts(args.artifacts)

    expected = expected_runs(args)

    rows: list[dict[str, Any]] = []
    extra_columns: set[str] = set()

    for taps, width, util, period in expected:
        base_run_id = make_run_id(taps, width, util, period)

        feature = features.get(base_run_id, {})
        status = statuses.get(base_run_id, {})

        feature_received = bool(feature)
        outcome = status.get("job_outcome", "")
        stage = status.get("last_completed_stage", "")
        failure = status.get("failure_reason", "")

        if feature_received and str(outcome).lower() == "success":
            config_status = "completed"
        elif feature_received:
            config_status = "features_received_status_unconfirmed"
        elif outcome:
            config_status = "failed_or_incomplete"
        else:
            config_status = "missing_artifact"

        # Preserve the actual diagnostic run ID when available.
        actual_run_id = (
            status.get("run_id")
            or feature.get("run_id")
            or base_run_id
        )

        row = {
            "run_id": actual_run_id,
            "configuration_status": config_status,
            "job_outcome": outcome,
            "last_completed_stage": stage,
            "failure_reason": failure,
            "feature_artifact_status": (
                "received" if feature_received else "missing"
            ),
            "feature_source_file": feature_sources.get(base_run_id, ""),
            "status_source_file": status_sources.get(base_run_id, ""),
            "taps": taps,
            "data_width": width,
            "coeff_width": 8,
            "acc_width": width + 16,
            "utilization": util,
            "place_density": util / 100.0,
            "clock_period": period,
            "clock_period_ns": period,
            "frequency_mhz": 1000.0 / period,
        }

        # Copy actual extractor measurements without inventing values.
        for key, value in feature.items():
            if key not in row:
                row[key] = value
                extra_columns.add(key)

        rows.append(row)

    columns = CORE_COLUMNS + sorted(
        extra_columns - set(CORE_COLUMNS)
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=columns,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)

    completed = sum(
        row["configuration_status"] == "completed"
        for row in rows
    )

    received = sum(
        row["feature_artifact_status"] == "received"
        for row in rows
    )

    failed = sum(
        row["configuration_status"] == "failed_or_incomplete"
        for row in rows
    )

    missing = sum(
        row["configuration_status"] == "missing_artifact"
        for row in rows
    )

    print("----------------------------------------")
    print(f"Expected configurations: {len(rows)}")
    print(f"Feature artifacts received: {received}")
    print(f"Completed configurations confirmed: {completed}")
    print(f"Failed/incomplete configurations: {failed}")
    print(f"Missing configurations: {missing}")
    print(f"Feature files discovered: {len(features)}")
    print(f"Status files discovered: {len(statuses)}")
    print(f"Combined CSV: {args.output}")
    print("----------------------------------------")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
