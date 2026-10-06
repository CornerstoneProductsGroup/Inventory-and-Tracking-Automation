"""CLI: schedule next-business-day FedEx Ground pickups from today's Lowe's CSV.

Standalone (not part of the FedEx batch run yet).

Examples:
    python run_fedex_pickup.py --plan-only
    python run_fedex_pickup.py --location postprotector --dry-run
    python run_fedex_pickup.py --location warehouse
    python run_fedex_pickup.py                      # both pickups, live
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_CONFIG = _HERE / "fedex_batch.json"
if not _DEFAULT_CONFIG.is_file():
    _DEFAULT_CONFIG = _HERE / "fedex_batch.example.json"


def _parse_date(raw: str) -> date:
    text = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date {raw!r}; use YYYY-MM-DD or MM/DD/YYYY.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Schedule FedEx Ground pickups (Our Warehouse / Post Protector) from today's Lowe's CSV."
    )
    parser.add_argument(
        "--location",
        choices=("both", "warehouse", "postprotector"),
        default="both",
        help="Which pickup(s) to schedule (default: both).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fill the FedEx form and screenshot it, but do NOT click SCHEDULE PICKUP.",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Only read the CSV and print package/weight totals (no browser).",
    )
    parser.add_argument("--csv", type=Path, default=None, help="Lowe's Output CSV to use instead of today's.")
    parser.add_argument("--date", metavar="DATE", default=None, help="Order date for the default CSV name.")
    parser.add_argument(
        "--pickup-date",
        metavar="DATE",
        default=None,
        help="Override pickup date (default: next business day).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Book even if fedex_pickup_state.json says this pickup was already booked.",
    )
    parser.add_argument("--no-wait", action="store_true", help="Do not pause for Enter between steps.")
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help=f"FedEx browser/login config (default: {_DEFAULT_CONFIG.name}).",
    )
    parser.add_argument(
        "--pickup-config",
        type=Path,
        default=None,
        help="Pickup settings JSON (default: fedex_pickup.json, else fedex_pickup.example.json).",
    )
    parser.add_argument("--manual-login", action="store_true", help="Type FedEx credentials yourself.")
    parser.add_argument(
        "--skip-auto-login",
        action="store_true",
        help="Use the saved session only; prompt for manual login if it expired.",
    )
    args = parser.parse_args()

    os.chdir(_HERE)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    try:
        from dotenv import load_dotenv

        load_dotenv(_HERE / ".env")
    except ImportError:
        pass

    try:
        order_date = _parse_date(args.date) if args.date else None
        pickup_date = _parse_date(args.pickup_date) if args.pickup_date else None
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

    if not args.config.is_file():
        print(f"ERROR: Config not found: {args.config}")
        return 1

    from automation.fedex_lowes_csv import LowesCsvSkip
    from automation.fedex_pickup import (
        LOCATIONS,
        PickupRunReport,
        print_pickup_closing,
        run_fedex_pickups,
    )

    locations = list(LOCATIONS) if args.location == "both" else [args.location]

    try:
        report = run_fedex_pickups(
            config_path=args.config.resolve(),
            pickup_settings_path=args.pickup_config.resolve() if args.pickup_config else None,
            locations=locations,
            dry_run=args.dry_run,
            plan_only=args.plan_only,
            order_date=order_date,
            csv_path=args.csv.resolve() if args.csv else None,
            pickup_date=pickup_date,
            force=args.force,
            manual_login=args.manual_login,
            skip_auto_login=args.skip_auto_login,
            wait_at_end=not args.no_wait,
        )
    except LowesCsvSkip as skip:
        message = (
            f"No Lowe's Output CSV for today (newest is {skip.top_filename!r}) — nothing to schedule."
        )
        print(f"[fedex/pickup] {message}")
        report = PickupRunReport(kind="no_csv", fatal_error=message)
        report.save()
        print_pickup_closing(report)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        report = PickupRunReport(fatal_error=str(exc))
        report.save()
        print_pickup_closing(report)
        return 1

    print_pickup_closing(report)
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
