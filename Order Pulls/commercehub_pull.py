"""
CommerceHub (Rithum OrderStream) daily file pull.

Opens OrderStream, picks the profile, then for each retailer row on the
Packing Slips page (PDF) and the Order Files page (.neworders -> .csv):
clicks Download and saves the file as "<Retailer> <M-D-YYYY>.<ext>" in that
retailer's folder.

This is menu 0. Each CommerceHub login ("account") has its own browser
profile (not the inventory or tracking browsers), its own .env credentials
from Inventory Submissions/.env, its own retailer list, log and manifest:

    us      RITHUM_USERNAME / RITHUM_PASSWORD        (Depot, Lowe's, ...)
    canada  RITHUM_CA_USERNAME / RITHUM_CA_PASSWORD  (Canada retailers)

Usage:
    python commercehub_pull.py --dry-run                    # US: list what it WOULD download
    python commercehub_pull.py                              # US: real run
    python commercehub_pull.py --account canada --dry-run   # Canada: list only
    python commercehub_pull.py --account canada             # Canada: real run

Partners that are not in an account's RETAILERS list are never downloaded;
the log shows them as "SKIP unknown partner '<name>'" so you can add them.
"""

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
BASE = Path(r"\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\1-Orders Before Extraction")
CSV_BASE = BASE / "6-CSV Order Files"

# Key = partner name exactly as shown in the PARTNER column on CommerceHub.
# "name" = what goes in the file name. Check the three marked VERIFY the
# first time they show up on the site.
US_RETAILERS = {
    "The Home Depot Inc": {
        "name": "Depot",
        "pdf": BASE / "1-Depot",
        "csv": CSV_BASE / "Depot",
    },
    "The Home Depot Special Orders": {
        "name": "Depot Special Order",              # VERIFY file-name label
        "pdf": BASE / "Depot Special Orders",
        "csv": CSV_BASE / "Depot Special Order",
    },
    "Lowe's": {
        "name": "Lowe's",
        "pdf": BASE / "2-Lowe's",
        "csv": CSV_BASE / "Lowe's",
    },
    "Tractor Supply Company": {                     # VERIFY partner name on site
        "name": "Tractor Supply",
        "pdf": BASE / "3-Tractor Supply",
        "csv": CSV_BASE / "Tractor Supply",
    },
    "Grainger": {                                   # VERIFY partner name on site
        "name": "Grainger",
        "pdf": BASE / "4a-Grainger",
        "csv": CSV_BASE / "Grainger",
    },
}

# Canada login. Same layout as US_RETAILERS. A partner with no "csv" folder
# is PDF-only: if a CSV row ever shows up for it, it is logged and left in
# CommerceHub (not downloaded). Canada is Home Depot only, Post Protector
# orders for now.
CANADA_BASE = Path(r"\\svr01\Cornerstone\Dot Com Packing Slips\15-Home Depot Canada")
CANADA_RETAILERS = {
    "The Home Depot Canada": {
        "name": "Canada",                           # -> "Canada 10-11-2026.pdf"
        "pdf": CANADA_BASE / "Post Protector",
        "csv": None,                                # packing slips only, no CSV
    },
}

HERE = Path(__file__).resolve().parent

# One entry per CommerceHub login. Browser profiles are separate so both
# logins stay signed in and never mix sessions.
#   profile: OrderStream profile to pick on the "Select OrderStream Profile"
#            screen. None = pick it automatically if there is only one, else
#            log the choices and wait for you to pick in the browser.
ACCOUNTS = {
    "us": {
        "label": "CommerceHub",
        "env_user": "RITHUM_USERNAME",
        "env_pass": "RITHUM_PASSWORD",
        "profile": "Cornerstone Products Group - (1)",
        "retailers": US_RETAILERS,
        "kinds": ("pdf", "csv"),
        # Separate from inventory/tracking and from the old pull-orders Edge profiles.
        "browser_dir": Path.home() / ".commercehub_pull_browser",
        "log": HERE / "commercehub_pull.log",
        "manifest": HERE / "commercehub_pull_manifest.json",
    },
    "canada": {
        "label": "CommerceHub Canada",
        "env_user": "RITHUM_CA_USERNAME",
        "env_pass": "RITHUM_CA_PASSWORD",
        "profile": None,                            # VERIFY on first Canada run
        "retailers": CANADA_RETAILERS,
        "kinds": ("pdf",),                          # Canada has packing slips only
        "browser_dir": Path.home() / ".commercehub_pull_browser_canada",
        "log": HERE / "commercehub_canada_pull.log",
        "manifest": HERE / "commercehub_canada_pull_manifest.json",
    },
}

# Set from --account in main().
ACCOUNT = ACCOUNTS["us"]
RETAILERS = US_RETAILERS
ENV_FILE = Path(__file__).resolve().parent.parent / "Inventory Submissions" / ".env"
INVENTORY_DIR = ENV_FILE.parent

HOME_URL = "https://dsm.commercehub.com/dsm/gotoHome.do"
PAGES = [
    # (label, url, file kind, saved extension)
    ("Packing Slips", "https://dsm.commercehub.com/dsm/gotoViewPackslips.do", "pdf", ".pdf"),
    ("Order Files", "https://dsm.commercehub.com/dsm/gotoViewOrders.do", "csv", ".csv"),
]
LOGIN_WAIT_MINUTES = 5
DOWNLOAD_TIMEOUT_MS = 60_000
# ----------------------------------------------------------------------------

log = logging.getLogger("chub")
ACCOUNT_KEY = "us"


def today_label(d=None):
    d = d or dt.date.today()
    return f"{d.month}-{d.day}-{d.year}"  # 10-7-2026, no leading zeros


def parse_order_date(raw: str) -> dt.date:
    text = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date {raw!r}; use YYYY-MM-DD or MM/DD/YYYY.")


def unique_path(path: Path) -> Path:
    """Never overwrite: add (2), (3)... if the name is taken."""
    if not path.exists():
        return path
    n = 2
    while True:
        p = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not p.exists():
            return p
        n += 1


def match_retailer(partner: str):
    p = partner.strip().lower()
    for key, cfg in RETAILERS.items():
        if key.lower() == p:
            return key, cfg
    return None, None


def _load_env():
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE, override=False)
    except Exception as exc:
        log.warning("Could not load %s (%s)", ENV_FILE, exc)


def _env_credentials() -> tuple[str, str]:
    _load_env()
    return ((os.getenv(ACCOUNT["env_user"]) or "").strip(),
            (os.getenv(ACCOUNT["env_pass"]) or "").strip())


def sign_in_with_env(page) -> bool:
    """Type the Inventory Submissions/.env CommerceHub login. Returns False if it is missing."""
    username, password = _env_credentials()
    if not username or not password:
        log.warning("Missing %s or %s in %s", ACCOUNT["env_user"], ACCOUNT["env_pass"], ENV_FILE)
        return False
    if str(INVENTORY_DIR) not in sys.path:
        sys.path.insert(0, str(INVENTORY_DIR))
    from automation.commercehub_login import perform_commercehub_login

    log.info("Signing in to %s with %s from Inventory Submissions/.env",
             ACCOUNT["label"], ACCOUNT["env_user"])
    perform_commercehub_login(
        page, username, password, log=lambda message: log.info("%s", message)
    )
    return True


def ensure_logged_in(page):
    """Get to OrderStream home. Use .env credentials, then wait if a code or profile is still needed."""
    page.goto(HOME_URL)
    deadline = dt.datetime.now() + dt.timedelta(minutes=LOGIN_WAIT_MINUTES)
    asked = False
    tried_env = False
    while dt.datetime.now() < deadline:
        url = page.url
        if "switch-account" in url or "switch-identity" in url:
            if select_profile(page):
                page.wait_for_url("**/dsm/**", timeout=60_000)
                continue
            page.wait_for_timeout(2000)  # waiting for a manual pick
            continue
        if "dsm.commercehub.com/dsm" in url and "login" not in url.lower():
            return
        on_login = "login" in url or "sso.auth" in url
        if on_login and not tried_env:
            tried_env = True
            try:
                if sign_in_with_env(page):
                    continue
            except Exception as exc:
                log.error("CommerceHub .env sign-in failed: %s", exc)
        if on_login and not asked:
            log.warning(
                "Still on the CommerceHub sign-in page. Finish it in the browser window (waiting %d min)...",
                LOGIN_WAIT_MINUTES,
            )
            asked = True
        page.wait_for_timeout(2000)
    raise RuntimeError("Timed out waiting for CommerceHub sign-in / profile selection.")


_profile_choices_logged = False


def select_profile(page) -> bool:
    """Click this account's OrderStream profile. Returns False while waiting for a manual pick."""
    global _profile_choices_logged
    wanted = ACCOUNT["profile"]
    if wanted:
        log.info("Selecting profile: %s", wanted)
        page.get_by_role("link", name=wanted, exact=True).click()
        return True
    links = page.locator("a").filter(has_text="(")
    try:
        links.first.wait_for(timeout=15_000)
    except PWTimeout:
        pass
    names = [t.strip() for t in links.all_inner_texts() if t.strip()]
    if len(names) == 1:
        log.info("Selecting the only profile: %s", names[0])
        links.first.click()
        return True
    if not _profile_choices_logged:
        log.warning("Several OrderStream profiles for %s: %s. Pick one in the browser, then set "
                    "ACCOUNTS['%s']['profile'] in commercehub_pull.py to skip this next time.",
                    ACCOUNT["label"], " | ".join(names) or "(none found)", ACCOUNT_KEY)
        _profile_choices_logged = True
    return False


def show_all_rows(page):
    sel = page.locator("select[name='fileDownloadTable_length']")
    if sel.count():
        sel.select_option("-1")
        page.wait_for_timeout(1000)


def read_rows(page):
    """Return [{partner, file, count, fileid}] from the download table."""
    rows = []
    for tr in page.locator("#fileDownloadTable tbody tr").all():
        cells = tr.locator(":scope > td")
        if cells.count() < 5:
            continue
        dl = tr.locator("div.fileDownloadLink")
        if not dl.count():
            continue
        rows.append({
            "partner": cells.nth(0).inner_text().strip(),
            "file": cells.nth(1).inner_text().strip(),
            "count": cells.nth(3).inner_text().strip(),
            "fileid": dl.first.get_attribute("data-fileid"),
        })
    return rows


def click_download(page, fileid):
    """Click a row's Download button and return the Playwright Download."""
    btn = page.locator(f"div#dl-status-{fileid} button")
    try:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as info:
            btn.click()
            # Some accounts get a confirm dialog after clicking Download.
            page.wait_for_timeout(1500)
            for label in ("Download", "OK", "Continue", "Yes"):
                b = page.locator("[role='dialog'] button, .ui-dialog button").filter(has_text=label)
                if b.count() and b.first.is_visible():
                    b.first.click()
                    break
        return info.value
    except PWTimeout:
        raise RuntimeError(f"No download started for file {fileid}")


def open_table(page, url, label) -> bool:
    """Load a download page. False (logged) if its file table never shows up."""
    page.goto(url)
    try:
        page.wait_for_selector("#fileDownloadTable", timeout=30_000)
    except PWTimeout:
        log.warning("No file table on the %s page (%s); treating it as no files waiting.",
                    label, page.url)
        return False
    show_all_rows(page)
    return True


def process_page(page, label, url, kind, ext, date_label, dry_run, summary, manifest):
    log.info("--- %s ---", label)
    if not open_table(page, url, label):
        return
    rows = read_rows(page)
    if not rows:
        log.info("No files waiting.")
        return

    for r in rows:
        key, cfg = match_retailer(r["partner"])
        if not cfg:
            log.warning("SKIP unknown partner '%s' (%s, %s) - add it to RETAILERS",
                        r["partner"], r["file"], r["count"])
            summary.append(("SKIPPED", label, r["partner"], r["file"], "unknown partner"))
            continue

        if not cfg.get(kind):
            log.warning("SKIP %s %s (%s, %s): no %s folder set for this partner; left in CommerceHub",
                        r["partner"], kind.upper(), r["file"], r["count"], kind.upper())
            summary.append(("SKIPPED", label, r["partner"], r["file"], f"no {kind.upper()} folder"))
            continue

        log.info("CHECK saw %s %s: %s (%s)", cfg["name"], kind.upper(), r["file"], r["count"])
        manifest["seen"].append({
            "name": cfg["name"],
            "kind": kind,
            "partner": r["partner"],
            "file": r["file"],
            "count": r["count"],
        })
        dest = unique_path(Path(cfg[kind]) / f"{cfg['name']} {date_label}{ext}")
        if dry_run:
            log.info("[dry run] %s | %s | %s -> %s", r["partner"], r["file"], r["count"], dest)
            summary.append(("DRY RUN", label, r["partner"], r["file"], str(dest)))
            continue

        try:
            Path(cfg[kind]).mkdir(parents=True, exist_ok=True)
            dl = click_download(page, r["fileid"])
            tmp = Path(dl.path())
            shutil.copyfile(tmp, dest)
            log.info("CHECK saved %s %s -> %s", cfg["name"], kind.upper(), dest)
            summary.append(("SAVED", label, r["partner"], r["file"], str(dest)))
            manifest["saved"].append({
                "name": cfg["name"],
                "kind": kind,
                "path": str(dest),
            })
        except Exception as e:  # keep going with the other retailers
            log.error("FAILED %s | %s: %s", r["partner"], r["file"], e)
            summary.append(("FAILED", label, r["partner"], r["file"], str(e)))
            manifest["failed"].append({
                "name": cfg["name"],
                "kind": kind,
                "error": str(e),
            })
        # The table can change after a download; reload before the next row.
        if not open_table(page, url, label):
            break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="list files, click nothing")
    ap.add_argument("--headless", action="store_true", help="no browser window (only once signed in)")
    ap.add_argument("--date", default=None, help="file-name date (YYYY-MM-DD or MM/DD/YYYY); default today")
    ap.add_argument("--account", choices=sorted(ACCOUNTS), default="us",
                    help="which CommerceHub login to pull (default: us)")
    args = ap.parse_args()

    global ACCOUNT, ACCOUNT_KEY, RETAILERS
    ACCOUNT_KEY = args.account
    ACCOUNT = ACCOUNTS[args.account]
    RETAILERS = ACCOUNT["retailers"]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(ACCOUNT["log"], encoding="utf-8")],
    )

    # A login that is not set up yet is skipped, not failed, so menu 0 keeps
    # working for the other accounts.
    if args.account != "us":
        _load_env()
        if not (os.getenv(ACCOUNT["env_user"]) or "").strip():
            log.warning("SKIPPED %s: %s is not set in %s", ACCOUNT["label"],
                        ACCOUNT["env_user"], ENV_FILE)
            return
        if not RETAILERS:
            log.warning("%s has no retailers configured yet: nothing will be downloaded; "
                        "partners found are listed as SKIP lines below.", ACCOUNT["label"])
    try:
        order_date = parse_order_date(args.date) if args.date else None
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)
    date_label = today_label(order_date)
    log.info("%s pull %s%s", ACCOUNT["label"], date_label, " (DRY RUN)" if args.dry_run else "")

    summary = []
    manifest = {"account": args.account, "date": date_label, "seen": [], "saved": [], "failed": []}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(ACCOUNT["browser_dir"]), headless=args.headless, accept_downloads=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ensure_logged_in(page)
            for label, url, kind, ext in PAGES:
                if kind not in ACCOUNT["kinds"]:
                    log.info("--- %s --- skipped (%s has no %s files)", label, ACCOUNT["label"], kind.upper())
                    continue
                process_page(page, label, url, kind, ext, date_label, args.dry_run, summary, manifest)
        finally:
            ctx.close()

    if not args.dry_run:
        ACCOUNT["manifest"].write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        log.info("Wrote %s", ACCOUNT["manifest"].name)
    seen_names = sorted({item["name"] for item in manifest["seen"]})
    log.info("CHECK %s files seen: %s", ACCOUNT["label"],
             ", ".join(seen_names) if seen_names else "(none)")

    log.info("=== Summary ===")
    for s in summary:
        log.info(" | ".join(s))
    if any(s[0] == "FAILED" for s in summary):
        sys.exit(1)


if __name__ == "__main__":
    main()
