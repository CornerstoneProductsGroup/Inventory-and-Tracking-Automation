"""CLI: morning order pull (CommerceHub PDF/CSV, then SPS Tractor/Grainger).

Menu 0. Runs the scripts in the repo's Order Pulls folder. Those scripts keep
their own browser profiles and sign in with Inventory Submissions/.env.
They do not share a browser with inventory, tracking, or invoice reports.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ORDER_PULLS = _HERE.parent / "Order Pulls"


def _parse_date(raw: str):
    text = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date {raw!r}; use YYYY-MM-DD or MM/DD/YYYY.")


def _run_script(name: str, extra: list[str]) -> int:
    script = _ORDER_PULLS / name
    if not script.is_file():
        print(f"ERROR: Missing {script}")
        return 1
    print(f"\n=== {name} ===", flush=True)
    completed = subprocess.run([sys.executable, str(script), *extra], cwd=str(_ORDER_PULLS))
    return int(completed.returncode or 0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pull morning orders: CommerceHub packing slips + CSVs, "
            "then SPS Tractor Supply and Grainger PDF + CSV."
        )
    )
    parser.add_argument(
        "--date",
        metavar="DATE",
        default=None,
        help="Order date for file names (default: today). Formats: YYYY-MM-DD or MM/DD/YYYY.",
    )
    parser.add_argument("--skip-commercehub", action="store_true")
    parser.add_argument("--skip-sps", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="List files and orders; click nothing")
    args = parser.parse_args()

    if args.date:
        try:
            _parse_date(args.date)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1

    extra: list[str] = []
    if args.dry_run:
        extra.append("--dry-run")
    if args.date:
        extra.extend(["--date", args.date.strip()])

    codes: list[int] = []
    if not args.skip_commercehub:
        codes.append(_run_script("commercehub_pull.py", extra))
    if not args.skip_sps:
        codes.append(_run_script("sps_pull.py", extra))
    if not codes:
        print("Nothing to run (both CommerceHub and SPS were skipped).")
        return 1
    return 0 if all(code == 0 for code in codes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
