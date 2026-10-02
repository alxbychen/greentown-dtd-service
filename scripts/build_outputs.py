#!/usr/bin/env python3
"""Build the reproducible historical output and comparison summary."""

from pathlib import Path
import sys
import csv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dtd_service.core import run_batch, write_json  # noqa: E402


def write_comparison_detail(output_path: Path, detail_path: Path) -> None:
    """Write comparable rows ordered by absolute computed-versus-CRI gap."""

    with output_path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    comparable = []
    for row in rows:
        if row.get("dtd_difference") in (None, ""):
            continue
        difference = float(row["dtd_difference"])
        row = {
            "Date": row["Date"],
            "computed_dtd": row["dtd"],
            "cri_dtd": row["cri_dtd"],
            "dtd_difference": row["dtd_difference"],
            "absolute_difference": f"{abs(difference):.12f}",
            "parameter_source": row["parameter_source"],
            "flags": row["flags"],
        }
        comparable.append((abs(difference), row))
    comparable.sort(key=lambda item: (-item[0], item[1]["Date"]))
    fields = [
        "rank", "Date", "computed_dtd", "cri_dtd", "dtd_difference",
        "absolute_difference", "parameter_source", "flags",
    ]
    detail_path.parent.mkdir(parents=True, exist_ok=True)
    with detail_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for rank, (_, row) in enumerate(comparable, start=1):
            writer.writerow({"rank": rank, **row})


def main() -> None:
    prepared = ROOT / "data" / "processed" / "prepared_inputs.csv"
    summary = run_batch(
        prepared if prepared.exists() else ROOT / "historical_inputs.csv",
        ROOT / "parameters.csv",
        ROOT / "data" / "processed" / "computed_dtd.csv",
        ROOT / "full_historical_dtd_output.csv",
    )
    summary["output_path"] = "data/processed/computed_dtd.csv"
    write_json(ROOT / "data" / "processed" / "comparison_summary.json", summary)
    write_comparison_detail(
        ROOT / "data" / "processed" / "computed_dtd.csv",
        ROOT / "data" / "processed" / "comparison_detail.csv",
    )
    print(summary)


if __name__ == "__main__":
    main()
