
#!/usr/bin/env python3
"""
Combine results from the FIR physical-design configuration sweep.

Expected artifact layout (searched recursively):
    artifacts/
        <artifact-name>/
            fir_project/
                matrix-status/
                    <run_id>.json
                runs/
                    <run_id>/
                        features.csv

The script:
- Generates the expected configuration matrix.
- Finds status JSON and feature CSV files recursively.
- Matches records by run_id, with safe path-based fallbacks.
- Detects duplicate feature records.
- Preserves missing configurations in the output.
- Never fabricates physical-design measurements.
- Writes combined_fir_features.csv.
- Adds a GitHub Actions step summary when GITHUB_STEP_SUMMARY is set.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_TAPS = (4, 8, 12)
DEFAULT_DATA_WIDTHS = (8, 16)
DEFAULT_UTILIZATIONS = (20, 30, 40, 50)
DEFAULT_COEFF_WIDTH = 8
DEFAULT_CLOCK_PERIOD = 10.0


CORE_COLUMNS = [
    "run_id",
    "configuration_status",
    "job_outcome",
    "last_completed_stage",
    "failure_reason",
    "feature_artifact_status",
    "taps",
    "data_width",
    "coeff_width",
    "acc_width",
    "utilization",
    "clock_period",
]


def expected_configurations(
    mode: str = "sweep",
    clock_period: float = DEFAULT_CLOCK_PERIOD,
) -> list[dict[str, Any]]:
    """Return the expected configuration matrix."""

    if clock_period <= 0:
        raise ValueError("Clock period must be greater than zero.")

    if mode == "single":
        configurations = [(8, 8, 40)]
    else:
        configurations = [
            (taps, data_width, utilization)
            for taps in DEFAULT_TAPS
            for data_width in DEFAULT_DATA_WIDTHS
            for utilization in DEFAULT_UTILIZATIONS
        ]

    result = []

    for taps, data_width, utilization in configurations:
        coeff_width = DEFAULT_COEFF_WIDTH

        # Must match the accumulator-width rule used by the FIR RTL.
        acc_width = data_width + coeff_width + 8

        run_id = (
            f"fir_t{taps}"
            f"_dw{data_width}"
            f"_cw{coeff_width}"
            f"_aw{acc_width}"
            f"_u{utilization}"
            f"_clk{clock_period:g}"
        )

        result.append(
            {
                "run_id": run_id,
                "taps": taps,
                "data_width": data_width,
                "coeff_width": coeff_width,
                "acc_width": acc_width,
                "utilization": utilization,
                "clock_period": clock_period,
            }
        )

    return result


def read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, returning None when it cannot be read safely."""

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)

        if not isinstance(data, dict):
            print(
                f"WARNING: JSON root is not an object: {path}",
                file=sys.stderr,
            )
            return None

        return data

    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: Cannot read {path}: {exc}", file=sys.stderr)
        return None


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read all rows from a CSV file."""

    try:
        with path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            return list(csv.DictReader(handle))

    except OSError as exc:
        print(f"WARNING: Cannot read {path}: {exc}", file=sys.stderr)
        return []


def discover_status_files(artifacts_dir: Path) -> list[Path]:
    """Find status JSON files under matrix-status directories."""

    if not artifacts_dir.exists():
        return []

    return sorted(
        path
        for path in artifacts_dir.rglob("*.json")
        if path.parent.name == "matrix-status"
    )


def discover_feature_files(artifacts_dir: Path) -> list[Path]:
    """Find feature CSV files in the downloaded artifacts."""

    if not artifacts_dir.exists():
        return []

    return sorted(
        path
        for path in artifacts_dir.rglob("features.csv")
        if path.parent.name != "matrix-status"
    )


def extract_run_id(
    data: dict[str, Any],
    fallback: str | None = None,
) -> str | None:
    """Extract run_id from common JSON field names."""

    for key in ("run_id", "runId", "configuration_id", "config_id"):
        value = data.get(key)

        if value is not None and str(value).strip():
            return str(value).strip()

    return fallback


def load_statuses(
    artifacts_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Load status records and warn about duplicate run IDs."""

    statuses: dict[str, dict[str, Any]] = {}

    for path in discover_status_files(artifacts_dir):
        data = read_json(path)

        if data is None:
            continue

        run_id = extract_run_id(data, path.stem)

        if not run_id:
            print(
                f"WARNING: No run ID found in status file: {path}",
                file=sys.stderr,
            )
            continue

        if run_id in statuses:
            print(
                f"WARNING: Duplicate status for {run_id}; "
                f"keeping the first record. Duplicate: {path}",
                file=sys.stderr,
            )
            continue

        statuses[run_id] = data

    return statuses


def load_features(
    artifacts_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Load feature records keyed by run ID."""

    features: dict[str, dict[str, Any]] = {}

    for path in discover_feature_files(artifacts_dir):
        rows = read_csv_rows(path)

        if not rows:
            print(
                f"WARNING: No feature rows found in {path}",
                file=sys.stderr,
            )
            continue

        if len(rows) > 1:
            print(
                f"WARNING: {path} contains {len(rows)} rows; "
                "expected one row per run. Each row will be checked.",
                file=sys.stderr,
            )

        for index, row in enumerate(rows, start=1):
            run_id = (
                row.get("run_id")
                or row.get("config_id")
                or path.parent.name
            )

            run_id = str(run_id).strip()

            if not run_id:
                print(
                    f"WARNING: Missing run ID in {path}, row {index}",
                    file=sys.stderr,
                )
                continue

            if run_id in features:
                existing = features[run_id]

                if existing != row:
                    print(
                        f"WARNING: Conflicting duplicate feature record "
                        f"for {run_id}; keeping the first record. "
                        f"Duplicate: {path}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"WARNING: Identical duplicate feature record "
                        f"for {run_id}: {path}",
                        file=sys.stderr,
                    )

                continue

            features[run_id] = dict(row)

    return features


def get_status_value(
    status: dict[str, Any],
    keys: tuple[str, ...],
    default: str = "",
) -> str:
    """Return the first non-empty status field."""

    for key in keys:
        value = status.get(key)

        if value is not None and str(value).strip():
            return str(value)

    return default


def combine(
    artifacts_dir: Path,
    output_path: Path,
    mode: str = "sweep",
    clock_period: float = DEFAULT_CLOCK_PERIOD,
) -> list[dict[str, Any]]:
    """Combine configuration metadata, job status, and feature records."""

    configurations = expected_configurations(mode, clock_period)
    statuses = load_statuses(artifacts_dir)
    features = load_features(artifacts_dir)

    combined_rows: list[dict[str, Any]] = []

    for config in configurations:
        run_id = config["run_id"]
        status = statuses.get(run_id)
        feature = features.get(run_id)

        # Begin with the actual extracted features, if available.
        row: dict[str, Any] = dict(feature or {})

        # Metadata from the expected configuration is authoritative.
        # Do not allow an artifact to change which configuration it represents.
        row.update(config)

        # Set feature status independently of status JSON availability.
        row["feature_artifact_status"] = (
            "received" if feature is not None else "missing"
        )

        if status is None:
            row["configuration_status"] = "missing_status"
            row["job_outcome"] = "no_status_artifact"
            row["last_completed_stage"] = "unknown"
            row["failure_reason"] = (
                "No readable status JSON found for this configuration."
            )
        else:
            row["configuration_status"] = get_status_value(
                status,
                ("configuration_status", "status", "state"),
                default="status_record_found",
            )

            row["job_outcome"] = get_status_value(
                status,
                ("job_outcome", "outcome", "conclusion"),
                default="unknown",
            )

            row["last_completed_stage"] = get_status_value(
                status,
                ("last_completed_stage", "last_stage", "stage"),
                default="unknown",
            )

            row["failure_reason"] = get_status_value(
                status,
                ("failure_reason", "error", "message"),
                default="",
            )

        if feature is None and status is not None:
            if not row["failure_reason"]:
                row["failure_reason"] = (
                    "No feature CSV found for this configuration."
                )

        # Preserve actual feature values; missing measurements stay blank.
        combined_rows.append(row)

    # Place key columns first and preserve every other discovered field.
    extra_columns: list[str] = []

    for row in combined_rows:
        for key in row:
            if key not in CORE_COLUMNS and key not in extra_columns:
                extra_columns.append(key)

    fieldnames = CORE_COLUMNS + extra_columns

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(combined_rows)

    received_features = sum(
        row["feature_artifact_status"] == "received"
        for row in combined_rows
    )
    received_statuses = sum(
        row["configuration_status"] != "missing_status"
        for row in combined_rows
    )

    print(f"Expected configurations: {len(configurations)}")
    print(f"Status records matched: {received_statuses}")
    print(f"Feature records matched: {received_features}")
    print(f"Missing status records: {len(configurations) - received_statuses}")
    print(f"Missing feature records: {len(configurations) - received_features}")
    print(f"Output CSV: {output_path}")

    write_github_summary(
        combined_rows,
        output_path,
        received_statuses,
        received_features,
    )

    return combined_rows


def write_github_summary(
    rows: list[dict[str, Any]],
    output_path: Path,
    status_count: int,
    feature_count: int,
) -> None:
    """Write a concise summary when running in GitHub Actions."""

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")

    if not summary_path:
        return

    total = len(rows)

    lines = [
        "## FIR Physical-Design Results",
        "",
        f"- Expected configurations: **{total}**",
        f"- Status records matched: **{status_count}/{total}**",
        f"- Feature records matched: **{feature_count}/{total}**",
        f"- Missing status records: **{total - status_count}**",
        f"- Missing feature records: **{total - feature_count}**",
        f"- Combined CSV: `{output_path}`",
        "",
        "| Run ID | Utilization | Status | Features |",
        "|---|---:|---|---|",
    ]

    for row in rows:
        run_id = str(row.get("run_id", "")).replace("|", "\\|")
        utilization = row.get("utilization", "")
        status = str(row.get("job_outcome", "")).replace("|", "\\|")
        feature_status = row.get("feature_artifact_status", "")

        lines.append(
            f"| {run_id} | {utilization} | {status} | {feature_status} |"
        )

    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Combine FIR physical-design sweep results."
    )

    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path("artifacts"),
        help="Root directory containing downloaded GitHub artifacts.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("combined_fir_features.csv"),
        help="Output combined CSV path.",
    )

    parser.add_argument(
        "--mode",
        choices=("sweep", "single"),
        default="sweep",
        help="Use the complete sweep or one 8-tap, 8-bit, 40%%-utilization run.",
    )

    parser.add_argument(
        "--clock-period",
        type=float,
        default=DEFAULT_CLOCK_PERIOD,
        help="Clock period used in run IDs. Must match the flow configuration.",
    )

    args = parser.parse_args()

    try:
        rows = combine(
            artifacts_dir=args.artifacts,
            output_path=args.output,
            mode=args.mode,
            clock_period=args.clock_period,
        )
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Missing results are preserved and reported. They are not silently
    # replaced with fabricated measurements.
    if not rows:
        print("ERROR: No expected configurations were generated.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
