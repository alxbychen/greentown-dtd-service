#!/usr/bin/env python3
"""Archive public unadjusted 03900.HK daily closes without calculating DTD."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dtd_service.pipeline import archive_market_quotes  # noqa: E402
from dtd_service.sources import DataSourceError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="First date, YYYYMMDD")
    parser.add_argument("--end", required=True, help="Last date, YYYYMMDD")
    args = parser.parse_args()
    try:
        start = datetime.strptime(args.start, "%Y%m%d").date()
        end = datetime.strptime(args.end, "%Y%m%d").date()
        summary = archive_market_quotes(ROOT, start, end)
    except (DataSourceError, ValueError, OSError) as exc:
        print(f"archive failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
