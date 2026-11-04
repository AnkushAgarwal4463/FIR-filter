
#!/usr/bin/env python3
"""Combine per-configuration FIR artifacts into one master CSV."""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Any


UTILIZATIONS = (20, 30, 40, 50)
COEFF_WIDTH = 8


def expected_configurations() -> list[dict[str, Any]]:
    """Build the expected configuration list for sweep or single mode."""
    mode = os.environ.get("RUN_MODE", "sweep").strip().lower()
    clock = float(os.environ.get("CLK_PERIOD", "10.0"))

    if clock <= 0:
        raise ValueError("CLK_PERIOD must be positive.")

    if mode == "single":
        configs = [{
            "taps": int(os.environ.get("SINGLE_TAPS", "8")),
            "data_width": int(os.environ.get("SINGLE_DATA_WIDTH", "8")),
            "utilization": int(os.environ.get("SINGLE_UTIL", "40")),
        }]
    elif mode == "sweep":
        configs = [
            {
                "taps": taps,
                "data_width": width,
                "utilization": util,
            }
            for taps, widths in (
                (4, (8, 16)),
                (8, (8, 16)),
                (12, (8,)),
            )
            for width in widths
            for util in UTILIZATIONS
        ]
    else:
        raise ValueError(
            f"Unsupported RUN_MODE={mode!r}; expected 'sweep' or 'single'."
        )

    for config in configs:
        taps = config["taps"]
        width = config["data_width"]
        util = config["utilization"]

        if taps not in (4, 8, 12):
            raise ValueError(f"Unsupported TAPS={taps}.")
        if width not in (8, 16):
            raise ValueError(f"Unsupported DATA_WIDTH={width}.")
        if util not in UTILIZATIONS:
            raise ValueError(
                f"Unsupported utilization={util}; expected {UTILIZATIONS}."
            )

        acc_width = width + COEFF_WIDTH + 8

        config.update({
            "coeff_width": COEFF_WIDTH,
            "acc_width": acc_width,
            "place_density": util / 100.0,
            "clk_period_ns": clock,
            "run_id": (
                f"fir_t{taps}_dw{width}_cw{COEFF_WIDTH}_"
                f"aw{acc_width}_u{util}_clk{clock:g}"
            ),
        })

    run_ids = [config["run_id"] for config in configs]
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("Generated configuration list contains duplicate run IDs.")

    return configs


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    """Read all CSV rows; return an empty list if the file is unavailable."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error, UnicodeError) as exc:
        print(f"WARNING: Could not read {path}: {exc}")
        return []


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object safely."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: Could not read {path}: {exc}")
        return {}


def merge_feature_rows(
    feature_files: list[Path],
) -> dict[str, dict[str, Any]]:
    """Index feature rows by run ID without silently discarding duplicates."""
    features: dict[str, dict[str, Any]] = {}

    for path in sorted(feature_files):
        rows = read_csv_rows(path)

        if not rows:
            print(f"WARNING: No feature row found in {path}")
            continue

        for row in rows:
            run_id = str(row.get("run_id") or path.parent.name).strip()

            if not run_id:
                print(f"WARNING: Skipping row with no run ID in {path}")
                continue

            row["run_id"] = run_id

            if run_id not in features:
                features[run_id] = row
                continue

            # Preserve existing non-empty values. Fill missing values from
            # duplicate artifacts, and warn if populated values disagree.
            existing = features[run_id]
            conflicts = []

            for key, value in row.items():
                current = existing.get(key)

                if current in (None, "") and value not in (None, ""):
                    existing[key] = value
                elif (
                    value not in (None, "")
                    and current not in (None, "")
                    and str(current) != str(value)
                ):
                    conflicts.append(key)

            if conflicts:
                print(
                    f"WARNING: Conflicting duplicate feature rows for {run_id} "
                    f"in {path}; retained existing values for: "
                    + ", ".join(conflicts)
                )

    return features


def combine(artifact_root: Path, output: Path) -> int:
    """Create one row per expected configuration."""
    statuses: dict[str, dict[str, Any]] = {}

    if artifact_root.is_dir():
        for path in sorted(artifact_root.rglob("*.json")):
            if path.parent.name != "matrix-status":
                continue

            record = read_json(path)
            run_id = str(record.get("run_id") or path.stem).strip()

            if not run_id:
                print(f"WARNING: Ignoring status file without run ID: {path}")
                continue

            if run_id in statuses:
                print(
                    f"WARNING: Duplicate status records for {run_id}; "
                    f"retaining the first record."
                )
                continue

            statuses[run_id] = record

        feature_files = list(artifact_root.rglob("features.csv"))
        features = merge_feature_rows(feature_files)
    else:
        print(f"WARNING: Artifact directory does not exist: {artifact_root}")
        features = {}

    configs = expected_configurations()
    expected_ids = {config["run_id"] for config in configs}

    for run_id in sorted(set(features) - expected_ids):
        print(f"WARNING: Feature artifact has unexpected run ID: {run_id}")

    for run_id in sorted(set(statuses) - expected_ids):
        print(f"WARNING: Status artifact has unexpected run ID: {run_id}")

    rows: list[dict[str, Any]] = []

    for config in configs:
        run_id = config["run_id"]
        feature = features.get(run_id, {})
        status = statuses.get(run_id, {})

        row: dict[str, Any] = {}

        # Start with measured feature fields.
        row.update(feature)

        # Add status fields without allowing them to overwrite measurements.
        for key, value in status.items():
            if key in {
                "run_id",
                "taps",
                "data_width",
                "coeff_width",
                "acc_width",
                "utilization",
                "place_density",
                "clk_period_ns",
            }:
                continue

            if key in row and row[key] not in (None, ""):
                row[f"status_{key}"] = value
            else:
                row[key] = value

        # Expected configuration is authoritative.
        row.update(config)

        if not status:
            row["job_outcome"] = "no_status_artifact"
            row["last_completed_stage"] = row.get(
                "last_completed_stage", "unknown"
            )
            row["failure_reason"] = (
                "No status artifact was received for this configuration."
            )
        elif not feature:
            row["feature_artifact_status"] = "missing"
        else:
            row["feature_artifact_status"] = "received"

        rows.append(row)

    # Put identifiers and core experimental parameters first.
    priority = [
        "run_id",
        "job_outcome",
        "last_completed_stage",
        "failure_reason",
        "feature_artifact_status",
        "taps",
        "data_width",
        "coeff_width",
        "acc_width",
        "utilization",
        "place_density",
        "clk_period_ns",
    ]

    all_fields: set[str] = set()
    for row in rows:
        all_fields.update(row.keys())

    fieldnames = [key for key in priority if key in all_fields]
    fieldnames.extend(sorted(all_fields - set(fieldnames)))

    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)

    succeeded = sum(
        str(row.get("job_outcome", "")).lower() == "success"
        for row in rows
    )
    failed = sum(
        str(row.get("job_outcome", "")).lower() == "failure"
        for row in rows
    )
    missing_status = sum(
        row.get("job_outcome") == "no_status_artifact"
        for row in rows
    )
    missing_features = sum(
        row.get("feature_artifact_status") != "received"
        for row in rows
    )

    print(f"Combined CSV: {output}")
    print(f"Expected configurations: {len(configs)}")
    print(f"Rows written: {len(rows)}")
    print(f"Successful jobs: {succeeded}")
    print(f"Failed jobs: {failed}")
    print(f"Missing status artifacts: {missing_status}")
    print(f"Missing feature artifacts: {missing_features}")

    summary_path = (
        os.environ.get("SUMMARY_FILE")
        or os.environ.get("GITHUB_STEP_SUMMARY")
    )

    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("# Combined FIR physical-design results\n\n")
            summary.write(f"CSV: `{output}`\n\n")
            summary.write(
                f"Expected: {len(configs)}; rows: {len(rows)}; "
                f"success: {succeeded}; failure: {failed}; "
                f"missing status: {missing_status}; "
                f"missing features: {missing_features}.\n\n"
            )
            summary.write(
                "| Run ID | Taps | Data width | Utilization | "
                "Outcome | Last stage | Failure detail |\n"
            )
            summary.write(
                "|---|---:|---:|---:|---|---|---|\n"
            )

            def safe(value: Any) -> str:
                return (
                    str(value if value is not None else "")
                    .replace("|", "\\|")
                    .replace("\n", " ")
                )

            for row in rows:
                summary.write(
                    f"| {safe(row.get('run_id'))} "
                    f"| {safe(row.get('taps'))} "
                    f"| {safe(row.get('data_width'))} "
                    f"| {safe(row.get('utilization'))} "
                    f"| {safe(row.get('job_outcome'))} "
                    f"| {safe(row.get('last_completed_stage'))} "
                    f"| {safe(row.get('failure_reason'))} |\n"
                )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path("artifacts"),
        help="Root directory containing downloaded configuration artifacts.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("combined_fir_features.csv"),
        help="Output master CSV path.",
    )
    args = parser.parse_args()

    try:
        return combine(args.artifacts, args.output)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
