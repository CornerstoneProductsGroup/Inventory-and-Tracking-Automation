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
import json
import logging
import os
import re
import sys
import time
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
MANIFEST_FILE = HERE / "sps_pull_manifest.json"
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
  // Leave window.print alone. SPS opens Chrome's print preview; we pick
  // Save as PDF there and use the Windows Save As dialog.
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


# SPS often puts the sign-in form in an iframe, and the address can already be
# /fulfillment while that form is still on screen.
USERNAME_SELECTORS = (
    "input[name='username']",
    "input#username",
    "input[type='email']",
    "input[name='email']",
    "input[name='identifier']",
    "input#okta-signin-username",
)
PASSWORD_SELECTORS = (
    "input[name='password']",
    "input#password",
    "input[type='password']",
)
NEXT_SELECTORS = (
    "button._button-login-id",
    "button[type='submit']",
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button:has-text('Log in')",
    "button:has-text('Sign in')",
)
SUBMIT_SELECTORS = (
    "button._button-login-password",
    "button[type='submit']",
    "button:has-text('Continue')",
    "button:has-text('Sign in')",
    "button:has-text('Log in')",
)


def _env_credentials() -> tuple[str, str]:
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE, override=False)
    except Exception as exc:
        log.warning("Could not load %s (%s)", ENV_FILE, exc)
    return (os.getenv("SPS_USERNAME") or "").strip(), (os.getenv("SPS_PASSWORD") or "").strip()


def _find_visible(page, selectors):
    """First visible match on the page or in any frame. Returns (frame, locator)."""
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if loc.count() and loc.is_visible():
                    return frame, loc
            except Exception:
                continue
    return None, None


def _click_visible(page, selectors) -> bool:
    frame, loc = _find_visible(page, selectors)
    if loc is None:
        return False
    try:
        loc.click(timeout=5_000)
    except Exception:
        loc.click(timeout=5_000, force=True)
    return True


def _looks_like_login_url(url: str) -> bool:
    u = (url or "").lower()
    return any(x in u for x in ("login", "signin", "sign-in", "/auth", "sso", "okta", "microsoftonline", "adfs"))


def _login_form_visible(page) -> bool:
    if _find_visible(page, USERNAME_SELECTORS)[1] is not None:
        return True
    if _find_visible(page, PASSWORD_SELECTORS)[1] is not None:
        return True
    return _looks_like_login_url(page.url)


def _app_ready(page) -> bool:
    """True only when the Fulfillment app is up, not merely when the address says /fulfillment."""
    if _looks_like_login_url(page.url) or _find_visible(page, USERNAME_SELECTORS + PASSWORD_SELECTORS)[1] is not None:
        return False
    url = (page.url or "").lower()
    if "commerce.spscommerce.com" not in url:
        return False
    for frame in page.frames:
        try:
            tile = frame.get_by_text(re.compile(r"New\s+Orders"))
            if tile.count() and tile.first.is_visible():
                return True
        except Exception:
            continue
    return "/home/apps" in url


def sign_in_with_env(page) -> bool:
    """Type the Inventory Submissions/.env SPS login. Returns False if the form is not ready."""
    username, password = _env_credentials()
    if not username or not password:
        log.warning("Missing SPS_USERNAME or SPS_PASSWORD in %s", ENV_FILE)
        return False
    _frame, user = _find_visible(page, USERNAME_SELECTORS)
    if user is None:
        return False
    log.info("Signing in to SPS with SPS_USERNAME from Inventory Submissions/.env (%s)", page.url)
    user.click(timeout=3_000)
    user.fill("")
    user.fill(username)
    if not _click_visible(page, NEXT_SELECTORS):
        user.press("Enter")
    _frame, pwd = None, None
    for _ in range(30):
        _frame, pwd = _find_visible(page, PASSWORD_SELECTORS)
        if pwd is not None:
            break
        page.wait_for_timeout(500)
    if pwd is None:
        raise RuntimeError("SPS accepted the username step but the password field never appeared")
    pwd.click(timeout=3_000)
    pwd.fill("")
    pwd.fill(password)
    if not _click_visible(page, SUBMIT_SELECTORS):
        pwd.press("Enter")
    page.wait_for_load_state("domcontentloaded")
    return True


def ensure_logged_in(page):
    page.goto("https://commerce.spscommerce.com", wait_until="domcontentloaded")
    deadline = dt.datetime.now() + dt.timedelta(minutes=LOGIN_WAIT_MINUTES)
    asked = False
    submitted = False
    while dt.datetime.now() < deadline:
        if _app_ready(page):
            if not page.url.startswith(DASHBOARD_URL):
                page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                if not _app_ready(page) and _login_form_visible(page):
                    continue
            return
        if not submitted and _find_visible(page, USERNAME_SELECTORS)[1] is not None:
            try:
                submitted = sign_in_with_env(page)
            except Exception as exc:
                log.error("SPS .env sign-in failed: %s", exc)
            continue
        if not submitted:
            _click_visible(page, (
                "a:has-text('Log in')",
                "button:has-text('Log in')",
                "a:has-text('Sign in')",
                "button:has-text('Sign in')",
                "a[href*='signin']",
            ))
        if not asked:
            log.warning(
                "SPS is not in Fulfillment yet (%s). Signing in from .env; finish any verification code in the browser (waiting %d min)...",
                page.url,
                LOGIN_WAIT_MINUTES,
            )
            asked = True
        page.wait_for_timeout(2000)
    try:
        (HERE / "sps_debug_login.html").write_text(page.content(), encoding="utf-8")
        log.error("Login page snapshot: %s", HERE / "sps_debug_login.html")
        log.error("Frame URLs: %s", [f.url for f in page.frames])
    except Exception:
        pass
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


_PRINT_SKIP_BUTTONS = {"save", "cancel", "print", "more settings", "see more..."}


def _uia_name(ctrl) -> str:
    try:
        return (ctrl.window_text() or "").strip()
    except Exception:
        return ""


def _click_uia(ctrl) -> None:
    try:
        ctrl.click_input()
    except Exception:
        ctrl.click()


def _chrome_print_windows():
    from pywinauto import Desktop

    desktop = Desktop(backend="uia")
    found = []
    for win in desktop.windows():
        try:
            cls = win.element_info.class_name or ""
            title = win.window_text() or ""
        except Exception:
            continue
        if "Chrome" in cls or "SPS" in title or "Print" in title:
            found.append(win)
    return found


def _preview_buttons(win):
    try:
        return list(win.descendants(control_type="Button"))
    except Exception:
        return []


def _find_print_preview():
    """Chrome print preview: Cancel plus Save (Save as PDF) or Print (a real printer)."""
    for win in _chrome_print_windows():
        buttons = _preview_buttons(win)
        names = {_uia_name(b).lower() for b in buttons}
        if "cancel" in names and ("save" in names or "print" in names):
            return win, buttons
    return None, []


def _list_items(win):
    items = []
    for kind in ("ListItem", "MenuItem"):
        try:
            items.extend(win.descendants(control_type=kind))
        except Exception:
            pass
    return items


def _click_save_as_pdf_item(win) -> bool:
    pools = [win]
    try:
        from pywinauto import Desktop

        pools.extend(Desktop(backend="uia").windows())
    except Exception:
        pass
    seen = set()
    for pool in pools:
        key = id(pool)
        if key in seen:
            continue
        seen.add(key)
        for item in _list_items(pool):
            if _uia_name(item) == "Save as PDF":
                log.info("CHECK print destination: Save as PDF")
                _click_uia(item)
                time.sleep(0.4)
                return True
    return False


def _destination_button(buttons):
    for btn in buttons:
        name = _uia_name(btn)
        low = name.lower()
        if not name or low in _PRINT_SKIP_BUTTONS:
            continue
        if (
            name == "Save as PDF"
            or "dpi" in low
            or "printer" in low
            or low.startswith("zebra")
            or low.startswith("toshiba")
            or low.startswith("brother")
        ):
            return btn
    return None


def _choose_save_as_pdf(win, buttons) -> None:
    if _click_save_as_pdf_item(win):
        return
    dest = _destination_button(buttons)
    if dest is None:
        raise RuntimeError("Print destination dropdown not found")
    if _uia_name(dest) == "Save as PDF":
        log.info("CHECK print destination already Save as PDF")
        return
    log.info("CHECK opening print destinations (currently %s)", _uia_name(dest))
    _click_uia(dest)
    time.sleep(0.5)
    if not _click_save_as_pdf_item(win):
        raise RuntimeError("Save as PDF was not in the print destination list")


def _click_preview_save(win) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for btn in _preview_buttons(win):
            if _uia_name(btn) != "Save":
                continue
            try:
                enabled = btn.is_enabled()
            except Exception:
                enabled = True
            if not enabled:
                continue
            log.info("CHECK clicking Save on the print window")
            _click_uia(btn)
            return
        time.sleep(0.3)
    raise RuntimeError("Save button on the print window did not become ready")


def _save_via_print_preview(dest: Path) -> bool:
    """Destination = Save as PDF, click Save, then Windows Save As into dest."""
    inv = Path(__file__).resolve().parent.parent / "Inventory Submissions"
    if str(inv) not in sys.path:
        sys.path.insert(0, str(inv))
    from automation.windows_save_as import fill_save_as_dialog

    deadline = time.monotonic() + 25
    win = None
    buttons = []
    while time.monotonic() < deadline:
        win, buttons = _find_print_preview()
        if win is not None:
            break
        time.sleep(0.4)
    if win is None:
        return False
    _choose_save_as_pdf(win, buttons)
    win, buttons = _find_print_preview()
    if win is None:
        raise RuntimeError("Print window closed before Save could be clicked")
    _click_preview_save(win)
    log.info("CHECK Save As dialog — folder %s, file %s", dest.parent, dest.name)
    if not fill_save_as_dialog(dest, timeout_s=60):
        raise RuntimeError(f"Save As did not write {dest}")
    _dismiss_print_preview()
    log.info("CHECK back on the order page; leaving the same orders checked")
    return True


def _dismiss_print_preview() -> None:
    """Close a leftover print preview so the order list is visible. Do not change checks."""
    win, buttons = _find_print_preview()
    if win is None:
        return
    for btn in buttons:
        if _uia_name(btn) == "Cancel":
            log.info("CHECK closing the print window")
            _click_uia(btn)
            time.sleep(0.5)
            return


def print_to_pdf(page, f, dest: Path, net_pdfs):
    collect_pdfs(page)      # discard anything left over
    net_pdfs.clear()
    click_more(f)
    f.get_by_text(re.compile(r"^\s*Print\s*$")).first.click()
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if _save_via_print_preview(dest):
            return dest.stat().st_size
    except Exception as exc:
        log.error("Print preview save failed: %s", exc)
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


def download_csv(page, f, dest: Path, ids):
    n_orders = len(ids)
    # The orders are still checked after the PDF save. Do not clear or re-check them.
    log.info("CHECK CSV download using the %d order(s) already checked", n_orders)
    try:
        open_download_menu(f)
        before = {t for t, _ in download_entries(f)}
        close_download_menu(f)
    except RuntimeError:
        before = set()

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


def process_retailer(page, r, date_label, dry_run, net_pdfs, summary, manifest):
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
    log.info("CHECK saw %s: %d order(s): %s", r["name"], len(ids), ", ".join(ids))
    manifest["seen"].append({"name": r["name"], "orders": ids})

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
    log.info("CHECK saved %s PDF (%d bytes) -> %s", r["name"], size, pdf_dest)
    summary.append(("SAVED PDF", r["name"], ", ".join(ids), str(pdf_dest)))
    manifest["saved"].append({"name": r["name"], "kind": "pdf", "path": str(pdf_dest)})

    page.wait_for_timeout(1500)
    title = download_csv(page, f, csv_dest, ids)
    log.info("CHECK saved %s CSV (%s) -> %s", r["name"], title, csv_dest)
    summary.append(("SAVED CSV", r["name"], ", ".join(ids), str(csv_dest)))
    manifest["saved"].append({"name": r["name"], "kind": "csv", "path": str(csv_dest)})
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
    manifest = {"date": date_label, "seen": [], "saved": [], "failed": []}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR), headless=args.headless, accept_downloads=True,
            viewport={"width": 1400, "height": 900},
            args=[
                "--disable-features=BlockThirdPartyCookies,TrackingProtection3pcd",
                "--disable-blink-features=AutomationControlled",
            ])
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
                    process_retailer(page, r, date_label, args.dry_run, net_pdfs, summary, manifest)
                except Exception as e:
                    failed = True
                    log.error("FAILED %s: %s", r["name"], e)
                    summary.append(("FAILED", r["name"], "", str(e)))
                    manifest["failed"].append({"name": r["name"], "error": str(e)})
        finally:
            ctx.close()

    if not args.dry_run:
        MANIFEST_FILE.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        log.info("Wrote %s", MANIFEST_FILE.name)
    seen_names = [item["name"] for item in manifest["seen"]]
    log.info("CHECK SPS retailers seen: %s", ", ".join(seen_names) if seen_names else "(none)")

    log.info("=== Summary ===")
    for s in summary:
        log.info(" | ".join(s))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
