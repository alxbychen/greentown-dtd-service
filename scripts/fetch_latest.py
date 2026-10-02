#!/usr/bin/env python3
"""Fetch one latest market/rate observation and write a reviewable JSON payload.

This command is intentionally opt-in.  It never modifies historical inputs.
The caller must provide the latest share count and financial statement values
because those are not safely inferred from a price feed.
"""

from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dtd_service.sources import DataSourceError, build_market_cap_input, fetch_hkma_12m_rate, fetch_yahoo_daily_quote  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shares-outstanding-m", type=float, required=True, help="Shares outstanding in millions")
    parser.add_argument("--current-liabilities", type=float, required=True)
    parser.add_argument("--long-term-borrowings", type=float, required=True)
    parser.add_argument("--total-liabilities", type=float, required=True)
    parser.add_argument("--total-assets", type=float, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "incoming" / "latest_input.json")
    args = parser.parse_args()
    try:
        quote = fetch_yahoo_daily_quote()
        rate = fetch_hkma_12m_rate()
        row = build_market_cap_input(
            quote,
            rate,
            args.shares_outstanding_m,
            {
                "BS_CUR_LIAB(HKD)": args.current_liabilities,
                "BS_LT_BORROW(HKD)": args.long_term_borrowings,
                "BS_TOT_LIAB2(HKD)": args.total_liabilities,
                "BS_TOT_ASSET(HKD)": args.total_assets,
            },
        )
    except DataSourceError as exc:
        print(f"latest data fetch failed: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"input": row, "market_source": quote["source"], "rate_source": rate["source"]}, indent=2) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
