"""
SPS Commerce daily order pull (Tractor Supply, Grainger).

For each retailer, one at a time:
  Fulfillment Dashboard -> "New Orders" tile -> check every row whose Sender
  matches the retailer -> bottom bar "..." -> Print  (PDF captured and saved)
  -> cloud icon -> "Combine documents into one CSV file" -> Download
  -> wait for it in SPS's download menu (top-right badge) -> click its cloud
  icon (opens a cdn.spscommerce.com tab) -> CSV saved -> uncheck orders
Files are saved as "<Retailer> <M-D-YYYY>.pdf / .csv" in that retailer's folders.

This is menu 0. It uses its own browser profile (not the inventory or
tracking browsers, and not sps_playwright_storage.json) and signs in with
SPS_USERNAME / SPS_PASSWORD from Inventory Submissions/.env.

Usage:
    python sps_pull.py --dry-run   # lists the orders it WOULD select; clicks nothing
    python sps_pull.py             # real run (Print moves the orders out of New)

Every run writes sps_debug_<retailer>.html next to the script (a snapshot of
the SPS screen) so selectors can be fixed quickly if SPS changes its layout.
"""

import argparse
import base64
import datetime as dt
import logging
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
BASE = Path(r"\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\1-Orders Before Extraction")
CSV_BASE = BASE / "6-CSV Order Files"

# "sender" = text that appears in the Sender column (case-insensitive "contains").
RETAILERS = [
    {"sender": "Tractor Supply", "name": "Tractor Supply",
     "pdf": BASE / "3-Tractor Supply", "csv": CSV_BASE / "Tractor Supply"},
    {"sender": "Grainger", "name": "Grainger",
     "pdf": BASE / "4a-Grainger", "csv": CSV_BASE / "Grainger"},
]

DASHBOARD_URL = "https://commerce.spscommerce.com/fulfillment"
# Separate from inventory/tracking and from the old pull-orders Edge profile.
BROWSER_PROFILE_DIR = Path.home() / ".sps_pull_browser"
HERE = Path(__file__).resolve().parent
LOG_FILE = HERE / "sps_pull.log"
ENV_FILE = HERE.parent / "Inventory Submissions" / ".env"
LOGIN_WAIT_MINUTES = 5
PDF_WAIT_SECONDS = 90
CSV_READY_SECONDS = 180   # how long to wait for SPS to build the CSV
# ----------------------------------------------------------------------------

log = logging.getLogger("sps")

# Runs in every frame before SPS's own code. Stops the Windows print dialog
# and keeps a copy of any PDF that SPS builds for printing.
CAPTURE_JS = r"""
(() => {
  if (window.__spsCapture) return; window.__spsCapture = true;
  window.__pdfs = []; window.__printCalls = 0;
  window.print = function () { window.__printCalls++; };
  const keep = (blob) => {
    try {
      blob.slice(0, 5).text().then(h => {
        if (blob.type === 'application/pdf' || h.startsWith('%PDF')) {
          const r = new FileReader();
          r.onload = () => window.__pdfs.push(r.result);
          r.readAsDataURL(blob);
        }
      });
    } catch (e) {}
  };
  const orig = URL.createObjectURL;
  URL.createObjectURL = function (obj) {
    if (obj instanceof Blob) keep(obj);
    return orig.apply(this, arguments);
  };
})();
"""


def today_label(d=None):
    d = d or dt.date.today()
    return f"{d.month}-{d.day}-{d.year}"


def parse_order_date(raw: str) -> dt.date:
    text = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date {raw!r}; use YYYY-MM-DD or MM/DD/YYYY.")


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        p = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not p.exists():
            return p
        n += 1


def app_frame(page):
    """The SPS Fulfillment app lives in an iframe named app-fulfillment-*."""
    for _ in range(60):
        for f in page.frames:
            if f.name.startswith("app-fulfillment") or "sps-cdn.com" in f.url:
                return f
        page.wait_for_timeout(500)
    raise RuntimeError("SPS Fulfillment app frame not found")


def _env_credentials() -> tuple[str, str]:
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE, override=False)
    except Exception as exc:
        log.warning("Could not load %s (%s)", ENV_FILE, exc)
    return (os.getenv("SPS_USERNAME") or "").strip(), (os.getenv("SPS_PASSWORD") or "").strip()


def _login_form_visible(page) -> bool:
    try:
        return page.locator("input[name='username']").first.is_visible()
    except Exception:
        return False


def sign_in_with_env(page) -> bool:
    """Type the Inventory Submissions/.env SPS login. Returns False if it is missing."""
    username, password = _env_credentials()
    if not username or not password:
        log.warning("Missing SPS_USERNAME or SPS_PASSWORD in %s", ENV_FILE)
        return False
    log.info("Signing in to SPS with SPS_USERNAME from Inventory Submissions/.env")
    page.locator("input[name='username']").wait_for(state="visible", timeout=30_000)
    page.locator("input[name='username']").fill(username)
    page.locator("button._button-login-id").click()
    page.locator("input[name='password']").wait_for(state="visible", timeout=30_000)
    page.locator("input[name='password']").fill(password)
    page.locator("button._button-login-password").click()
    page.wait_for_load_state("domcontentloaded")
    return True


def ensure_logged_in(page):
    page.goto(DASHBOARD_URL)
    deadline = dt.datetime.now() + dt.timedelta(minutes=LOGIN_WAIT_MINUTES)
    asked = False
    tried_env = False
    while dt.datetime.now() < deadline:
        if page.url.startswith("https://commerce.spscommerce.com/fulfillment") and not _login_form_visible(page):
            return
        if not tried_env and _login_form_visible(page):
            tried_env = True
            try:
                if sign_in_with_env(page):
                    continue
            except Exception as exc:
                log.error("SPS .env sign-in failed: %s", exc)
        if not asked:
            log.warning(
                "Still on the SPS sign-in page. Finish it in the browser window, including any verification code (waiting %d min)...",
                LOGIN_WAIT_MINUTES,
            )
            asked = True
        page.wait_for_timeout(2000)
    raise RuntimeError("Timed out waiting for SPS sign-in.")


def open_new_orders(page):
    page.goto(DASHBOARD_URL)
    f = app_frame(page)
    tile = f.get_by_text(re.compile(r"^\s*New\s+Orders\s*$"))
    tile.first.wait_for(timeout=60_000)
    tile.first.click()
    page.wait_for_url("**/transactions/list/**", timeout=60_000)
    f = app_frame(page)
    f.get_by_text("Matching Results").first.wait_for(timeout=60_000)
    page.wait_for_timeout(2000)  # let the table fill
    return f


def order_rows(f):
    """[(row_locator, document_id, sender)] for Order rows in the table."""
    out = []
    for tr in f.locator("tr").all():
        cells = tr.locator(":scope > td")
        n = cells.count()
        if n < 5:
            continue
        texts = [cells.nth(i).inner_text().strip() for i in range(n)]
        if not any(t == "Order" for t in texts):
            continue
        doc_id = next((t for t in texts if re.fullmatch(r"\d{6,}", t)), "")
        # Sender column is the one after Status; find it by header name.
        sender = ""
        headers = [h.inner_text().strip() for h in f.locator("thead th").all()]
        if "Sender" in headers and headers.index("Sender") < n:
            sender = texts[headers.index("Sender")]
        out.append((tr, doc_id, sender.replace("\n", " ")))
    return out


def clear_selection(f):
    btn = f.get_by_text("Clear Selected")
    if btn.count() and btn.first.is_visible():
        btn.first.click()
        f.page.wait_for_timeout(500)


def tick(row):
    cb = row.locator("input[type=checkbox]")
    if cb.count():
        if not cb.first.is_checked():
            cb.first.check(force=True)
    else:
        row.locator(":scope > td").first.click()


def selected_count(f):
    t = f.get_by_text(re.compile(r"Items? Selected")).first
    try:
        bar = t.locator("xpath=..").inner_text()
        m = re.search(r"(\d+)", bar)
        return int(m.group(1)) if m else -1
    except Exception:
        return -1


def bottom_bar_buttons(f):
    """Icon buttons in the bar that shows 'N Items Selected'."""
    label = f.get_by_text(re.compile(r"Items? Selected")).first
    bar = label.locator("xpath=ancestor::*[count(.//button) >= 3][1]")
    return [b for b in bar.locator("button").all()
            if "Clear Selected" not in (b.inner_text() or "")]


def click_more(f):
    for sel in ("button[title*='More' i]", "button[aria-label*='More' i]",
                "button:has-text('•••')", "button:has-text('...')"):
        loc = f.locator(sel)
        for b in loc.all():
            if b.is_visible() and b.bounding_box() and b.bounding_box()["y"] > 500:
                b.click()
                return
    btns = bottom_bar_buttons(f)
    if not btns:
        raise RuntimeError("Bottom bar '...' button not found")
    btns[-1].click()  # '...' is the last icon before Clear Selected


def click_cloud(f):
    for sel in ("button[title*='Download' i]", "button[aria-label*='Download' i]"):
        for b in f.locator(sel).all():
            if b.is_visible():
                b.click()
                return
    btns = bottom_bar_buttons(f)
    if len(btns) < 2:
        raise RuntimeError("Bottom bar cloud/download button not found")
    btns[-2].click()  # cloud icon sits just left of '...'


def collect_pdfs(page):
    found = []
    for fr in page.frames:
        try:
            found += fr.evaluate("window.__pdfs ? window.__pdfs.splice(0) : []")
        except Exception:
            pass
    return found


def print_to_pdf(page, f, dest: Path, net_pdfs):
    collect_pdfs(page)      # discard anything left over
    net_pdfs.clear()
    click_more(f)
    f.get_by_text(re.compile(r"^\s*Print\s*$")).first.click()
    for _ in range(PDF_WAIT_SECONDS * 2):
        page.wait_for_timeout(500)
        pdfs = collect_pdfs(page)
        if pdfs:
            data = base64.b64decode(pdfs[-1].split(",", 1)[1])
            break
        if net_pdfs:
            data = net_pdfs[-1]
            break
    else:
        raise RuntimeError("Print clicked but no PDF was captured")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return len(data)


ENTRY_RE = re.compile(r"Document Download\s*-\s*[^\n|]+")


def download_entries(f):
    """[(title, row_locator)] in SPS's download menu, newest first."""
    out = []
    f.evaluate("document.querySelectorAll('[data-chub-row]').forEach(e => e.removeAttribute('data-chub-row'))")
    for i, t in enumerate(f.get_by_text(ENTRY_RE).all()):
        try:
            if not t.is_visible():
                continue
            title = ENTRY_RE.search(t.inner_text()).group(0).strip()
            # Tag this entry's own container: the largest ancestor that still
            # holds only this one entry. It has no button yet while building.
            t.evaluate("""(el, i) => {
                let n = el;
                while (n.parentElement &&
                       (n.parentElement.innerText.match(/Document Download/g) || []).length === 1) {
                    n = n.parentElement;
                }
                n.setAttribute('data-chub-row', String(i));
            }""", i)
            out.append((title, f.locator(f"[data-chub-row='{i}']")))
        except Exception:
            pass
    return out


def open_download_menu(f):
    """Top-right badge + list icon (next to the Fulfillment tabs)."""
    if download_entries(f):
        return
    cands = []
    for sel in ("button[aria-label*='download' i]", "button[title*='download' i]",
                "[class*='download' i] button", "button"):
        for b in f.locator(sel).all():
            try:
                box = b.bounding_box()
                if b.is_visible() and box and box["y"] < 140:
                    cands.append((box["x"], b))
            except Exception:
                pass
        if cands and sel != "button":
            break
    for _, b in sorted(cands, key=lambda c: -c[0]):  # right-most first
        b.click()
        f.page.wait_for_timeout(800)
        if download_entries(f):
            return
        f.page.keyboard.press("Escape")
    raise RuntimeError("SPS download menu (top right) not found")


def close_download_menu(f):
    f.page.keyboard.press("Escape")
    f.page.wait_for_timeout(300)


def save_from_cloud(page, row, dest: Path):
    """The row's cloud icon opens a new tab (cdn.spscommerce.com) with the file."""
    ctx = page.context
    btn = row.locator("button").last
    got = {}

    def on_dl(d):
        got.setdefault("dl", d)

    page.on("download", on_dl)
    with ctx.expect_page(timeout=30_000) as pinfo:
        btn.click()
    popup = pinfo.value
    popup.on("download", on_dl)
    for _ in range(40):
        if "dl" in got:
            got["dl"].save_as(dest)
            break
        page.wait_for_timeout(500)
    else:  # file shown inline instead of downloaded: fetch it directly
        resp = ctx.request.get(popup.url)
        if not resp.ok:
            raise RuntimeError(f"CSV fetch failed: HTTP {resp.status}")
        dest.write_bytes(resp.body())
    try:
        popup.close()
    except Exception:
        pass


def reselect(f, ids):
    """Orders normally stay checked after Print; re-check by ID if not."""
    if selected_count(f) == len(ids):
        return
    clear_selection(f)
    for tr, doc_id, _ in order_rows(f):
        if doc_id in ids:
            tick(tr)
    if selected_count(f) not in (-1, len(ids)):
        raise RuntimeError("Could not re-select the orders for the CSV")


def download_csv(page, f, dest: Path, ids):
    n_orders = len(ids)
    # Remember what's already in SPS's download list (Print adds one too).
    try:
        open_download_menu(f)
        before = {t for t, _ in download_entries(f)}
        close_download_menu(f)
    except RuntimeError:
        before = set()
    reselect(f, ids)

    click_cloud(f)
    f.get_by_text("Combine documents into one CSV file").first.click()
    f.get_by_role("button", name=re.compile(r"^\s*Download\s*$")).last.click()
    log.info("CSV requested; waiting for SPS to build it...")

    # Wait for a NEW entry to appear in the menu and finish building.
    deadline = dt.datetime.now() + dt.timedelta(seconds=CSV_READY_SECONDS)
    while dt.datetime.now() < deadline:
        page.wait_for_timeout(3000)
        try:
            open_download_menu(f)
        except RuntimeError:
            continue
        new = [(t, row) for t, row in download_entries(f) if t not in before]
        if new:
            title, row = new[0]  # newest is on top
            txt = row.inner_text()
            m = re.search(r"(\d+)\s+transactions?", txt)
            if m and int(m.group(1)) != n_orders:
                log.warning("Newest download '%s' has %s transactions, expected %d",
                            title, m.group(1), n_orders)
            cloud = row.locator("button")
            if (cloud.count() and cloud.last.is_visible() and cloud.last.is_enabled()
                    and "Expires" in txt):
                dest.parent.mkdir(parents=True, exist_ok=True)
                save_from_cloud(page, row, dest)
                close_download_menu(f)
                return title
        close_download_menu(f)
    raise RuntimeError(f"CSV not ready in SPS download menu after {CSV_READY_SECONDS}s")


def snapshot(f, name):
    try:
        (HERE / f"sps_debug_{name}.html").write_text(f.content(), encoding="utf-8")
    except Exception:
        pass


def process_retailer(page, r, date_label, dry_run, net_pdfs, summary):
    log.info("--- %s ---", r["name"])
    f = open_new_orders(page)
    clear_selection(f)
    rows = [x for x in order_rows(f) if r["sender"].lower() in x[2].lower()]
    snapshot(f, r["name"].replace(" ", "_"))
    if not rows:
        log.info("No new orders.")
        summary.append(("NONE", r["name"], "", ""))
        return
    ids = [x[1] for x in rows]
    log.info("%d new order(s): %s", len(ids), ", ".join(ids))

    pdf_dest = unique_path(Path(r["pdf"]) / f"{r['name']} {date_label}.pdf")
    csv_dest = unique_path(Path(r["csv"]) / f"{r['name']} {date_label}.csv")
    if dry_run:
        log.info("[dry run] would print -> %s", pdf_dest)
        log.info("[dry run] would CSV   -> %s", csv_dest)
        summary.append(("DRY RUN", r["name"], ", ".join(ids), f"{pdf_dest} | {csv_dest}"))
        return

    for tr, _, _ in rows:
        tick(tr)
    n = selected_count(f)
    if n not in (-1, len(rows)):
        raise RuntimeError(f"Expected {len(rows)} selected, SPS shows {n}")

    size = print_to_pdf(page, f, pdf_dest, net_pdfs)
    log.info("Saved PDF (%d bytes) -> %s", size, pdf_dest)
    summary.append(("SAVED PDF", r["name"], ", ".join(ids), str(pdf_dest)))

    page.wait_for_timeout(1500)
    title = download_csv(page, f, csv_dest, ids)
    log.info("Saved CSV (%s) -> %s", title, csv_dest)
    summary.append(("SAVED CSV", r["name"], ", ".join(ids), str(csv_dest)))
    clear_selection(f)  # uncheck before the next retailer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="list orders, click nothing")
    ap.add_argument("--headless", action="store_true", help="no browser window (only once signed in)")
    ap.add_argument("--date", default=None, help="file-name date (YYYY-MM-DD or MM/DD/YYYY); default today")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG_FILE, encoding="utf-8")])
    try:
        order_date = parse_order_date(args.date) if args.date else None
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)
    date_label = today_label(order_date)
    log.info("SPS pull %s%s", date_label, " (DRY RUN)" if args.dry_run else "")

    summary, failed = [], False
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR), headless=args.headless, accept_downloads=True,
            viewport={"width": 1400, "height": 900})
        ctx.add_init_script(CAPTURE_JS)
        net_pdfs = []

        def on_response(resp):
            try:
                if "application/pdf" in (resp.headers.get("content-type") or ""):
                    net_pdfs.append(resp.body())
            except Exception:
                pass
        ctx.on("response", on_response)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ensure_logged_in(page)
            for r in RETAILERS:
                try:
                    process_retailer(page, r, date_label, args.dry_run, net_pdfs, summary)
                except Exception as e:
                    failed = True
                    log.error("FAILED %s: %s", r["name"], e)
                    summary.append(("FAILED", r["name"], "", str(e)))
        finally:
            ctx.close()

    log.info("=== Summary ===")
    for s in summary:
        log.info(" | ".join(s))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
