"""
CommerceHub (Rithum OrderStream) daily file pull.

Opens OrderStream, picks the profile, then for each retailer row on the
Packing Slips page (PDF) and the Order Files page (.neworders -> .csv):
clicks Download and saves the file as "<Retailer> <M-D-YYYY>.<ext>" in that
retailer's folder.

This is menu 0. It uses its own browser profile (not the inventory or
tracking browsers) and signs in with RITHUM_USERNAME / RITHUM_PASSWORD from
Inventory Submissions/.env.

Usage:
    python commercehub_pull.py --dry-run   # list what it WOULD download, clicks nothing
    python commercehub_pull.py             # real run (pulls the orders in CommerceHub)
"""

import argparse
import datetime as dt
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
PROFILE_NAME = "Cornerstone Products Group - (1)"

BASE = Path(r"\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\1-Orders Before Extraction")
CSV_BASE = BASE / "6-CSV Order Files"

# Key = partner name exactly as shown in the PARTNER column on CommerceHub.
# "name" = what goes in the file name. Check the three marked VERIFY the
# first time they show up on the site.
RETAILERS = {
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

# Separate from inventory/tracking and from the old pull-orders Edge profiles.
BROWSER_PROFILE_DIR = Path.home() / ".commercehub_pull_browser"
LOG_FILE = Path(__file__).with_name("commercehub_pull.log")
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


def _env_credentials() -> tuple[str, str]:
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE, override=False)
    except Exception as exc:
        log.warning("Could not load %s (%s)", ENV_FILE, exc)
    return (os.getenv("RITHUM_USERNAME") or "").strip(), (os.getenv("RITHUM_PASSWORD") or "").strip()


def sign_in_with_env(page) -> bool:
    """Type the Inventory Submissions/.env CommerceHub login. Returns False if it is missing."""
    username, password = _env_credentials()
    if not username or not password:
        log.warning("Missing RITHUM_USERNAME or RITHUM_PASSWORD in %s", ENV_FILE)
        return False
    if str(INVENTORY_DIR) not in sys.path:
        sys.path.insert(0, str(INVENTORY_DIR))
    from automation.commercehub_login import perform_commercehub_login

    log.info("Signing in to CommerceHub with RITHUM_USERNAME from Inventory Submissions/.env")
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
            log.info("Selecting profile: %s", PROFILE_NAME)
            page.get_by_role("link", name=PROFILE_NAME, exact=True).click()
            page.wait_for_url("**/dsm/**", timeout=60_000)
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


def process_page(page, label, url, kind, ext, date_label, dry_run, summary):
    log.info("--- %s ---", label)
    page.goto(url)
    page.wait_for_selector("#fileDownloadTable", timeout=30_000)
    show_all_rows(page)
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
            log.info("Saved %s | %s (%s) -> %s", r["partner"], r["file"], r["count"], dest)
            summary.append(("SAVED", label, r["partner"], r["file"], str(dest)))
        except Exception as e:  # keep going with the other retailers
            log.error("FAILED %s | %s: %s", r["partner"], r["file"], e)
            summary.append(("FAILED", label, r["partner"], r["file"], str(e)))
        # The table can change after a download; reload before the next row.
        page.goto(url)
        page.wait_for_selector("#fileDownloadTable", timeout=30_000)
        show_all_rows(page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="list files, click nothing")
    ap.add_argument("--headless", action="store_true", help="no browser window (only once signed in)")
    ap.add_argument("--date", default=None, help="file-name date (YYYY-MM-DD or MM/DD/YYYY); default today")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )
    try:
        order_date = parse_order_date(args.date) if args.date else None
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)
    date_label = today_label(order_date)
    log.info("CommerceHub pull %s%s", date_label, " (DRY RUN)" if args.dry_run else "")

    summary = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR), headless=args.headless, accept_downloads=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ensure_logged_in(page)
            for label, url, kind, ext in PAGES:
                process_page(page, label, url, kind, ext, date_label, args.dry_run, summary)
        finally:
            ctx.close()

    log.info("=== Summary ===")
    for s in summary:
        log.info(" | ".join(s))
    if any(s[0] == "FAILED" for s in summary):
        sys.exit(1)


if __name__ == "__main__":
    main()
