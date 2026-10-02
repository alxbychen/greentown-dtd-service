#!/usr/bin/env python3
"""Backfill or incrementally update sourced inputs and recompute DTD."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dtd_service.pipeline import update_data  # noqa: E402
from dtd_service.sources import DataSourceError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="First date to fetch, YYYYMMDD; defaults to day after stored cutoff")
    parser.add_argument("--end", help="Last date to fetch, YYYYMMDD; defaults to latest completed HK day")
    args = parser.parse_args()
    try:
        summary = update_data(ROOT, args.start, args.end)
    except (DataSourceError, ValueError, OSError) as exc:
        print(f"update failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
