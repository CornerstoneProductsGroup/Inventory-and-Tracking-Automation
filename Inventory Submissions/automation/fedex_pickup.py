"""FedEx pickup scheduling: book next-business-day Ground pickups from today's Lowe's CSV.

Two pickups, each optional:

* ``warehouse``      – SKUs whose vendor is in ``warehouse_vendors.json`` (our location).
                       Uses the account's default pickup address as-is.
* ``postprotector``  – SKUs whose vendor is Post Protector (their location).
                       Changes the pickup address before filling the form.

Totals come from the same ``Lowe's M-D-YYYY Output.csv`` the FedEx batch uploads:
column AB = packages (labels) for the row, AC = total weight for the row, AG = SKU.

Standalone on purpose: nothing in ``fedex_batch_shipping.run_fedex_batch`` calls this yet.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

from automation.fedex_batch_shipping import (
    _load_config,
    _login_if_needed,
    _maybe_accept_fedex_cookies,
    _open_fedex_browser,
    _save_fedex_session,
)
from automation.fedex_credentials import FedexCredentials, env_file_path, load_fedex_credentials
from automation.fedex_lowes_csv import resolve_upload_csv
from automation.fedex_reference import vendor_for_sku
from automation.warehouse_print_vendors import (
    is_warehouse_print_vendor,
    load_warehouse_print_vendors,
)

_INVENTORY_ROOT = Path(__file__).resolve().parent.parent
_STATE_PATH = _INVENTORY_ROOT / "fedex_pickup_state.json"
_SCREENSHOT_DIR = _INVENTORY_ROOT / "screenshots" / "fedex_pickup"

DEFAULT_PICKUP_URL = "https://www.fedex.com/shippingplus/en-us/pickup/schedule-pickup"

# CSV columns (0-based): AB=27, AC=28, AG=32
COL_PACKAGES = 27
COL_WEIGHT = 28
COL_SKU = 32

WAREHOUSE = "warehouse"
POSTPROTECTOR = "postprotector"
LOCATIONS = (WAREHOUSE, POSTPROTECTOR)
LOCATION_LABELS = {WAREHOUSE: "Our Warehouse", POSTPROTECTOR: "Post Protector"}

DEFAULT_PICKUP_SETTINGS: dict[str, Any] = {
    "pickup_url": DEFAULT_PICKUP_URL,
    "service": "GROUND",
    "earliest_time": "10:00 AM",
    "latest_time": "4:00 PM",
    "pickup_instructions": "No instructions",
    "warehouse": {
        # Text that must appear on the address card before booking our pickup.
        "expected_address_text": "1106 E Turner",
    },
    "postprotector": {
        # Vendor names (from vendor_map_lowes.xlsx) that belong to the Post Protector pickup.
        "vendors": ["Post Protector"],
        "address": {
            "contact_name": "Chris Elliott",
            "company": "Cornerstone Post Protector",
            "phone": "4193528688",
            "phone_extension": "1210",
            "country": "United States",
            "address_line_1": "513 Napoleon Rd",
            "address_line_2": "",
            "address_line_3": "",
            "zip": "43402",
            "state": "Ohio",
            "city": "Bowling Green",
            "residential": False,
        },
    },
}


def _log(msg: str) -> None:
    print(f"[fedex/pickup] {msg}", flush=True)


class FedexPickupError(Exception):
    pass


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def default_pickup_config_path() -> Path:
    local = _INVENTORY_ROOT / "fedex_pickup.json"
    if local.is_file():
        return local
    return _INVENTORY_ROOT / "fedex_pickup.example.json"


def load_pickup_settings(path: Path | None = None) -> dict[str, Any]:
    src = path or default_pickup_config_path()
    settings = dict(DEFAULT_PICKUP_SETTINGS)
    if src.is_file():
        with src.open(encoding="utf-8") as f:
            data = json.load(f)
        settings = _deep_merge(DEFAULT_PICKUP_SETTINGS, data)
        _log(f"Pickup settings: {src.name}")
    else:
        _log("Pickup settings: built-in defaults")
    env_url = (os.environ.get("FEDEX_PICKUP_URL") or "").strip()
    if env_url:
        settings["pickup_url"] = env_url
    return settings


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def next_business_day(today: date | None = None) -> date:
    """Next weekday after ``today`` (Friday → Monday). Never the same day."""
    d = (today or date.today()) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def pickup_date_option_text(d: date) -> str:
    """FedEx dropdown text, e.g. 'Monday, October 5'."""
    return f"{d:%A}, {d:%B} {d.day}"


def _parse_option_date(text: str, reference: date) -> date | None:
    m = re.match(r"^\s*[A-Za-z]+,\s*([A-Za-z]+)\s+(\d{1,2})\s*$", text or "")
    if not m:
        return None
    for year in (reference.year, reference.year + 1):
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {year}", "%B %d %Y").date()
        except ValueError:
            return None
        if d >= reference - timedelta(days=7):
            return d
    return None


# ---------------------------------------------------------------------------
# CSV → pickup totals
# ---------------------------------------------------------------------------


@dataclass
class PickupTotals:
    location: str
    packages: int = 0
    weight_lb: float = 0.0
    rows: int = 0
    by_vendor: dict[str, list[float]] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return LOCATION_LABELS.get(self.location, self.location)

    @property
    def weight_for_form(self) -> int:
        return max(1, int(math.ceil(self.weight_lb))) if self.packages else 0

    def add(self, vendor: str, packages: int, weight: float) -> None:
        self.packages += packages
        self.weight_lb += weight
        self.rows += 1
        bucket = self.by_vendor.setdefault(vendor, [0, 0.0])
        bucket[0] += packages
        bucket[1] += weight


@dataclass
class PickupPlan:
    csv_path: Path
    pickup_date: date
    totals: dict[str, PickupTotals]
    other_vendors: dict[str, list[float]]
    unmapped_skus: list[str]


def _to_number(raw: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", (raw or "").replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _read_csv_rows(path: Path) -> list[list[str]]:
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with path.open(encoding=enc, newline="") as f:
                return list(csv.reader(f))
        except UnicodeDecodeError as exc:
            last_err = exc
    raise FedexPickupError(f"Could not read {path.name}: {last_err}")


def _vendor_for_csv_sku(sku: str) -> str | None:
    try:
        return vendor_for_sku(sku)
    except (KeyError, ValueError):
        return None


def build_pickup_plan(
    csv_path: Path,
    settings: dict[str, Any],
    *,
    pickup_date: date | None = None,
) -> PickupPlan:
    load_warehouse_print_vendors()
    pp_vendors = {
        str(v).strip().casefold()
        for v in (settings.get("postprotector", {}).get("vendors") or [])
        if str(v).strip()
    }
    totals = {loc: PickupTotals(loc) for loc in LOCATIONS}
    other: dict[str, list[float]] = {}
    unmapped: list[str] = []

    rows = _read_csv_rows(csv_path)
    for line_no, row in enumerate(rows, start=1):
        if len(row) <= COL_SKU:
            continue
        sku = (row[COL_SKU] or "").strip()
        packages = _to_number(row[COL_PACKAGES])
        weight = _to_number(row[COL_WEIGHT])
        if not sku or packages is None:
            continue  # header or blank row
        pk = int(round(packages))
        wt = weight or 0.0
        vendor = _vendor_for_csv_sku(sku)
        if vendor is None:
            unmapped.append(f"line {line_no}: {sku}")
            continue
        if vendor.strip().casefold() in pp_vendors:
            totals[POSTPROTECTOR].add(vendor, pk, wt)
        elif is_warehouse_print_vendor(vendor):
            totals[WAREHOUSE].add(vendor, pk, wt)
        else:
            bucket = other.setdefault(vendor, [0, 0.0])
            bucket[0] += pk
            bucket[1] += wt

    return PickupPlan(
        csv_path=csv_path,
        pickup_date=pickup_date or next_business_day(),
        totals=totals,
        other_vendors=other,
        unmapped_skus=unmapped,
    )


def print_pickup_plan(plan: PickupPlan, locations: list[str]) -> None:
    _log(f"CSV: {plan.csv_path}")
    _log(f"Pickup date: {pickup_date_option_text(plan.pickup_date)} ({plan.pickup_date.isoformat()})")
    for loc in LOCATIONS:
        t = plan.totals[loc]
        flag = "" if loc in locations else "  (not selected this run)"
        _log(
            f"  {t.label}: {t.packages} package(s), {t.weight_lb:g} lb "
            f"→ form weight {t.weight_for_form}{flag}"
        )
        for vendor, (pk, wt) in sorted(t.by_vendor.items()):
            _log(f"      {vendor}: {int(pk)} pkg, {wt:g} lb")
    if plan.other_vendors:
        names = ", ".join(sorted(plan.other_vendors))
        _log(f"  Not picked up by us (other vendors): {names}")
    if plan.unmapped_skus:
        _log(f"  WARN: {len(plan.unmapped_skus)} SKU(s) not in vendor map (ignored):")
        for item in plan.unmapped_skus[:20]:
            _log(f"      {item}")


# ---------------------------------------------------------------------------
# State (avoid double-booking)
# ---------------------------------------------------------------------------


def _state_key(pickup_date: date, location: str) -> str:
    return f"{pickup_date.isoformat()}|{location}"


def _load_state() -> dict[str, Any]:
    if not _STATE_PATH.is_file():
        return {"pickups": {}}
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("pickups"), dict):
            return data
    except Exception:
        pass
    return {"pickups": {}}


def get_booked(pickup_date: date, location: str) -> dict[str, Any] | None:
    return _load_state()["pickups"].get(_state_key(pickup_date, location))


def record_booking(pickup_date: date, location: str, entry: dict[str, Any]) -> None:
    data = _load_state()
    entry = dict(entry)
    entry["recorded_at"] = datetime.now(timezone.utc).isoformat()
    data["pickups"][_state_key(pickup_date, location)] = entry
    _STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------

_MARK_FIELD_JS = """
([labelText, scope, token]) => {
  const norm = s => (s || '').toUpperCase().replace(/\\(REQUIRED\\)/g, '')
      .replace(/\\*/g, '').replace(/\\s+/g, ' ').trim();
  const want = norm(labelText);
  let root = document;
  if (scope === 'panel') {
    let el = document.querySelector('[data-test-id="save-button"]');
    while (el && el.querySelectorAll('input').length < 5) el = el.parentElement;
    if (!el) return null;
    root = el;
  }
  const visible = el => !!(el && el.getClientRects().length);
  const labels = [...root.querySelectorAll('label')]
      .filter(l => visible(l) && norm(l.innerText) === want);
  if (!labels.length) return null;
  const label = labels[labels.length - 1];
  let id = label.htmlFor || '';
  if (!id && label.id && label.id.endsWith('-label')) id = label.id.slice(0, -6);
  let ctrl = id ? document.getElementById(id) : null;
  if (!ctrl) {
    const box = label.closest('div') || label.parentElement;
    ctrl = box ? box.querySelector('input, select, textarea') : null;
  }
  if (!ctrl) return null;
  ctrl.setAttribute('data-pickup-field', token);
  return token;
}
"""

_token_counter = 0


def _field(page: Page, label_text: str, *, scope: str = "page"):
    global _token_counter
    _token_counter += 1
    token = f"f{_token_counter}"
    found = page.evaluate(_MARK_FIELD_JS, [label_text, scope, token])
    if not found:
        raise FedexPickupError(f"Pickup form field not found: {label_text!r} ({scope})")
    return page.locator(f'[data-pickup-field="{token}"]')


def _fill(page: Page, label_text: str, value: str, *, scope: str = "page") -> None:
    loc = _field(page, label_text, scope=scope)
    loc.fill("")
    if value:
        loc.fill(str(value))
    loc.press("Tab")
    actual = loc.input_value()
    if (actual or "").strip() != str(value).strip():
        _log(f"WARN: {label_text} shows {actual!r} after entering {value!r}")


def _select_by_aria(page: Page, aria: str, text: str) -> str:
    sel = page.locator(f'select[aria-label="{aria}"]').last
    sel.wait_for(state="attached", timeout=20_000)
    sel.select_option(label=text)
    page.wait_for_timeout(400)
    return _selected_text(page, aria)


def _selected_text(page: Page, aria: str) -> str:
    return page.locator(f'select[aria-label="{aria}"]').last.evaluate(
        "s => (s.options[s.selectedIndex] || {}).text || ''"
    ).strip()


def _option_texts(page: Page, aria: str) -> list[str]:
    return page.locator(f'select[aria-label="{aria}"]').last.evaluate(
        "s => [...s.options].map(o => o.text.trim()).filter(Boolean)"
    )


_TIME_RE = re.compile(r"^\s*0?(\d{1,2})[:.](\d{2})\s*([AaPp])\.?\s*[Mm]\.?\s*$")


def _canonical_time(text: str) -> str:
    """'10:00 AM', '10:00\u00a0am', '04:00 P.M.' → '10:00 AM' / '4:00 PM'."""
    raw = (text or "").replace("\u00a0", " ").replace("\u202f", " ").strip()
    match = _TIME_RE.match(raw)
    if not match:
        return ""
    hour = int(match.group(1))
    if hour < 1 or hour > 12:
        return ""
    ampm = "AM" if match.group(3).upper() == "A" else "PM"
    return f"{hour}:{match.group(2)} {ampm}"


def _visible_select(page: Page, aria: str):
    """The on-screen dropdown. A later hidden copy of the same label has no options."""
    loc = page.locator(f'select[aria-label="{aria}"]')
    count = loc.count()
    if count == 0:
        raise FedexPickupError(f"Dropdown not found: {aria}")
    for i in range(count - 1, -1, -1):
        item = loc.nth(i)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return loc.last


def _select_records(sel) -> list[dict[str, Any]]:
    return sel.evaluate(
        """s => [...s.options].map(o => ({
            text: (o.textContent || '').replace(/\\s+/g, ' ').trim(),
            value: o.value || '',
            disabled: !!o.disabled
        }))"""
    )


def _match_time_option(options: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    target = _canonical_time(wanted)
    if not target:
        raise FedexPickupError(f"Pickup time {wanted!r} is not a time like '10:00 AM'")
    for opt in options:
        if opt.get("disabled"):
            continue
        if _canonical_time(opt.get("text") or "") == target:
            return opt
        if _canonical_time(opt.get("value") or "") == target:
            return opt
    return None


def _select_time(page: Page, aria: str, wanted: str) -> str:
    """Pick a pickup time.

    Playwright's select_option waits until an option with that exact label is
    visible. On this form the Earliest list often has no match under that
    check, so it sat for two minutes and Latest was never changed. Read the
    options ourselves and set the value the way Angular expects.
    """
    sel = _visible_select(page, aria)
    try:
        sel.scroll_into_view_if_needed(timeout=5_000)
    except Exception:
        pass

    opened = False
    options: list[dict[str, Any]] = []
    deadline = time.time() + 20
    while time.time() < deadline:
        options = _select_records(sel)
        if _match_time_option(options, wanted):
            break
        usable = [
            opt
            for opt in options
            if _canonical_time(opt.get("text") or "") or _canonical_time(opt.get("value") or "")
        ]
        if not usable and not opened:
            _log(f"{aria}: list is empty, opening it so FedEx fills the times…")
            sel.evaluate(
                """s => {
                    s.focus();
                    s.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    s.click();
                }"""
            )
            opened = True
            page.wait_for_timeout(700)
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            continue
        page.wait_for_timeout(400)

    match = _match_time_option(options, wanted)
    if not match:
        shown: list[str] = []
        for opt in options:
            label = opt.get("text") or opt.get("value") or "(blank)"
            if opt.get("disabled"):
                label = f"{label} (disabled)"
            shown.append(label)
        raise FedexPickupError(f"{aria}: {wanted!r} is not in the list. FedEx offered: {shown}")

    sel.evaluate(
        """(s, spec) => {
            const opt = [...s.options].find(o =>
                o.value === spec.value &&
                (o.textContent || '').replace(/\\s+/g, ' ').trim() === spec.text);
            if (!opt) return;
            const desc = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value');
            if (desc && desc.set) desc.set.call(s, opt.value);
            else s.value = opt.value;
            opt.selected = true;
            s.dispatchEvent(new Event('input', { bubbles: true }));
            s.dispatchEvent(new Event('change', { bubbles: true }));
            s.dispatchEvent(new Event('blur', { bubbles: true }));
        }""",
        {"text": match.get("text") or "", "value": match.get("value") or ""},
    )
    page.wait_for_timeout(500)
    got = sel.evaluate(
        "s => ((s.options[s.selectedIndex] || {}).textContent || '').replace(/\\s+/g, ' ').trim()"
    )
    if _canonical_time(got) != _canonical_time(wanted):
        label = (match.get("text") or "").strip()
        if label:
            sel.select_option(label=label, timeout=8_000)
        else:
            sel.select_option(value=match.get("value") or "", timeout=8_000)
        page.wait_for_timeout(400)
        got = sel.evaluate(
            "s => ((s.options[s.selectedIndex] || {}).textContent || '').replace(/\\s+/g, ' ').trim()"
        )
    if _canonical_time(got) != _canonical_time(wanted):
        raise FedexPickupError(f"{aria} shows {got!r}, expected {wanted!r}")
    return got


def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=10_000)
    except Exception:
        return ""


def _screenshot(page: Page, location: str, tag: str) -> Path:
    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = _SCREENSHOT_DIR / f"{stamp}_{location}_{tag}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        _log(f"Screenshot: {path}")
    except Exception as exc:
        _log(f"WARN: screenshot failed: {exc}")
    return path


def _open_pickup_form(page: Page, cfg: dict[str, Any], settings: dict[str, Any]) -> None:
    url = settings.get("pickup_url") or DEFAULT_PICKUP_URL
    _log(f"Opening Schedule a Pickup: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=120_000)
    _maybe_accept_fedex_cookies(page, cfg, peel_overlays=False)
    try:
        page.locator('[data-test-id="pickup-submit"]').wait_for(state="visible", timeout=60_000)
    except Exception as exc:
        raise FedexPickupError(
            f"Schedule a Pickup form did not load (URL: {page.url}). Is the FedEx session signed in?"
        ) from exc
    page.locator('[data-test-id="pickup-address-edit-button"]').wait_for(
        state="visible", timeout=30_000
    )
    page.wait_for_timeout(1500)


def _address_card_text(page: Page) -> str:
    return page.locator('[data-test-id="pickup-address-edit-button"]').evaluate(
        """b => { let el = b; for (let i = 0; i < 6 && el; i++) {
             el = el.parentElement;
             if (el && el.innerText && el.innerText.length > 40) return el.innerText; }
           return ''; }"""
    )


def _change_address(page: Page, addr: dict[str, Any]) -> None:
    _log("Changing pickup address…")
    page.locator('[data-test-id="pickup-address-edit-button"]').click()
    page.locator('[data-test-id="save-button"]').wait_for(state="visible", timeout=20_000)
    page.wait_for_timeout(800)

    _fill(page, "CONTACT NAME", addr.get("contact_name", ""), scope="panel")
    _fill(page, "COMPANY", addr.get("company", ""), scope="panel")
    _fill(page, "PHONE NUMBER", addr.get("phone", ""), scope="panel")
    _fill(page, "PHONE EXTENSION", addr.get("phone_extension", ""), scope="panel")

    country = addr.get("country") or "United States"
    if _selected_text(page, "Country/Territory") != country:
        _select_by_aria(page, "Country/Territory", country)
        page.wait_for_timeout(1000)

    _fill(page, "ADDRESS LINE 1", addr.get("address_line_1", ""), scope="panel")
    _fill(page, "ADDRESS LINE 2", addr.get("address_line_2", ""), scope="panel")
    _fill(page, "ADDRESS LINE 3", addr.get("address_line_3", ""), scope="panel")
    _fill(page, "ZIP CODE", addr.get("zip", ""), scope="panel")
    page.wait_for_timeout(1500)  # ZIP may auto-fill city/state

    state = addr.get("state") or ""
    if state and _selected_text(page, "State or province") != state:
        _select_by_aria(page, "State or province", state)
        page.wait_for_timeout(600)
    _fill(page, "CITY", addr.get("city", ""), scope="panel")

    want_residential = bool(addr.get("residential", False))
    try:
        box = _field(page, "This is a residential address", scope="panel")
        if box.is_checked() != want_residential:
            box.evaluate(
                "c => { const l = document.querySelector(`label[for='${c.id}']`); (l || c).click(); }"
            )
            page.wait_for_timeout(300)
        if box.is_checked() != want_residential:
            _log("WARN: could not set the residential checkbox")
    except FedexPickupError:
        _log("WARN: residential checkbox not found")

    page.locator('[data-test-id="save-button"]').click()
    try:
        page.locator('[data-test-id="save-button"]').wait_for(state="hidden", timeout=20_000)
    except Exception as exc:
        raise FedexPickupError(
            "Address panel did not close after SAVE — FedEx may have rejected the address."
        ) from exc
    page.wait_for_timeout(1500)

    card = _address_card_text(page)
    line1 = (addr.get("address_line_1") or "").strip()
    if line1 and line1.lower() not in card.lower():
        raise FedexPickupError(
            f"Address card does not show {line1!r} after saving. Card text: {card!r}"
        )
    _log(f"Pickup address now: {' / '.join(x.strip() for x in card.splitlines() if x.strip())}")


def _select_ground(page: Page) -> None:
    radio = page.locator('[data-test-id="service-category-radio-button-GROUND"]')
    if not radio.is_checked():
        page.locator('[data-test-id="service-category-label-GROUND"]').click()
        page.wait_for_timeout(2500)
    if not radio.is_checked():
        raise FedexPickupError("Could not select FedEx Ground.")
    _log("Service: FedEx Ground")


def _choose_pickup_date(page: Page, wanted: date) -> date:
    target = pickup_date_option_text(wanted)
    deadline = time.time() + 20
    options: list[str] = []
    while time.time() < deadline:
        options = _option_texts(page, "Pickup date")
        if target in options:
            _select_by_aria(page, "Pickup date", target)
            return wanted
        if len(options) > 1:
            break
        page.wait_for_timeout(500)

    # Requested day not offered (holiday, etc.) → next weekday that is offered.
    candidates: list[tuple[date, str]] = []
    for text in options:
        d = _parse_option_date(text, wanted)
        if d and d > wanted and d.weekday() < 5:
            candidates.append((d, text))
    if not candidates:
        raise FedexPickupError(
            f"Pickup date {target!r} not offered and no later weekday found. Options: {options}"
        )
    candidates.sort()
    d, text = candidates[0]
    _log(f"WARN: {target!r} not offered by FedEx — using {text!r} instead.")
    _select_by_aria(page, "Pickup date", text)
    return d


def _fill_pickup_details(
    page: Page, totals: PickupTotals, settings: dict[str, Any], pickup_date: date
) -> date:
    _select_ground(page)

    _fill(page, "PACKAGES", str(totals.packages))
    _fill(page, "TOTAL WEIGHT", str(totals.weight_for_form))
    try:
        unit = _field(page, "MEASUREMENT SYSTEM")
        if unit.evaluate("s => (s.options[s.selectedIndex] || {}).text || ''").strip() != "lb":
            unit.select_option(label="lb")
    except FedexPickupError:
        pass

    instr = settings.get("pickup_instructions") or "No instructions"
    got = _select_by_aria(page, "Pickup Instructions", instr)
    if got != instr:
        raise FedexPickupError(f"Pickup Instructions shows {got!r}, expected {instr!r}")

    actual_date = _choose_pickup_date(page, pickup_date)
    page.wait_for_timeout(1200)

    earliest = settings.get("earliest_time") or "10:00 AM"
    latest = settings.get("latest_time") or "4:00 PM"
    got_early = _select_time(page, "Earliest possible time", earliest)
    page.wait_for_timeout(800)
    got_late = _select_time(page, "Latest possible time", latest)
    page.wait_for_timeout(1500)

    _log(
        f"Form: Ground, {totals.packages} pkg, {totals.weight_for_form} lb, "
        f"{_selected_text(page, 'Pickup date')}, {got_early}–{got_late}, {instr}"
    )
    text = _body_text(page)
    costs = [ln.strip() for ln in text.splitlines() if "$" in ln]
    if costs:
        _log(f"Pickup cost shown: {' | '.join(costs[:4])}")
    return actual_date


_CONFIRM_RE = re.compile(
    r"confirmation\s*(?:number|no\.?|#)?\s*[:#]?\s*([A-Z]{0,4}\d[A-Z0-9-]{3,})", re.IGNORECASE
)


def _submit_and_confirm(page: Page, location: str) -> dict[str, Any]:
    _log("Clicking SCHEDULE PICKUP…")
    page.locator('[data-test-id="pickup-submit"]').click()
    deadline = time.time() + 90
    text = ""
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        text = _body_text(page)
        m = _CONFIRM_RE.search(text)
        if m:
            shot = _screenshot(page, location, "confirmed")
            return {"status": "confirmed", "confirmation": m.group(1), "screenshot": str(shot)}
        if re.search(r"pickup (?:is |has been )?(?:scheduled|confirmed)", text, re.IGNORECASE):
            break
    shot = _screenshot(page, location, "after_submit")
    dump = shot.with_suffix(".txt")
    try:
        dump.write_text(text, encoding="utf-8")
        _log(f"Page text saved: {dump}")
    except OSError:
        pass
    return {"status": "submitted_unconfirmed", "confirmation": "", "screenshot": str(shot)}


def _wait_for_enter(prompt: str) -> None:
    if os.environ.get("FEDEX_PICKUP_NO_WAIT") or not sys.stdin or not sys.stdin.isatty():
        return
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_fedex_pickups(
    *,
    config_path: Path,
    pickup_settings_path: Path | None = None,
    locations: list[str],
    dry_run: bool = False,
    plan_only: bool = False,
    order_date: date | None = None,
    csv_path: Path | None = None,
    pickup_date: date | None = None,
    force: bool = False,
    manual_login: bool = False,
    skip_auto_login: bool = False,
    wait_at_end: bool = True,
) -> int:
    settings = load_pickup_settings(pickup_settings_path)
    upload_csv = resolve_upload_csv(order_date=order_date, explicit_path=csv_path)
    plan = build_pickup_plan(upload_csv, settings, pickup_date=pickup_date)
    print_pickup_plan(plan, locations)

    todo: list[str] = []
    for loc in locations:
        t = plan.totals[loc]
        if t.packages <= 0:
            _log(f"{t.label}: no packages today — skipping.")
            continue
        booked = get_booked(plan.pickup_date, loc)
        if booked and not force and not dry_run:
            _log(
                f"{t.label}: already booked for {plan.pickup_date.isoformat()} "
                f"({booked.get('status')}, confirmation {booked.get('confirmation') or 'n/a'}). "
                "Skipping — use --force to book again."
            )
            continue
        todo.append(loc)

    if plan_only:
        _log("Plan only — FedEx not opened.")
        return 0
    if not todo:
        _log("Nothing to schedule.")
        return 0

    cfg = _load_config(config_path)
    manual_login = manual_login or (os.environ.get("FEDEX_MANUAL_LOGIN", "").strip().lower() in ("1", "true", "yes"))
    skip_auto_login = skip_auto_login or (
        os.environ.get("FEDEX_SKIP_AUTO_LOGIN", "").strip().lower() in ("1", "true", "yes")
    )
    creds: FedexCredentials | None = None
    if not manual_login:
        try:
            creds = load_fedex_credentials(cfg)
            _log(f"FedEx credentials loaded for {creds.username!r} (from {env_file_path()})")
        except ValueError as exc:
            raise FedexPickupError(str(exc)) from exc

    browser_cfg = cfg.get("browser", {})
    slow_mo = int(browser_cfg.get("slow_mo_ms", 0))
    default_timeout = int(browser_cfg.get("default_timeout_ms", 120_000))
    mode = "DRY RUN (will stop before SCHEDULE PICKUP)" if dry_run else "LIVE (will submit)"
    _log(f"Mode: {mode}. Pickups: {', '.join(LOCATION_LABELS[l] for l in todo)}")

    failures = 0
    with sync_playwright() as p:
        browser, context, page, persistent = _open_fedex_browser(
            p, cfg, headless=False, slow_mo=slow_mo
        )
        page.set_default_timeout(default_timeout)
        try:
            _login_if_needed(
                page, cfg, creds, manual_login=manual_login, skip_auto_login=skip_auto_login
            )
            for loc in todo:
                totals = plan.totals[loc]
                _log(f"===== {totals.label} =====")
                try:
                    _open_pickup_form(page, cfg, settings)
                    if loc == POSTPROTECTOR:
                        _change_address(page, settings["postprotector"]["address"])
                    else:
                        expect = (settings.get("warehouse", {}).get("expected_address_text") or "").strip()
                        card = _address_card_text(page)
                        if expect and expect.lower() not in card.lower():
                            raise FedexPickupError(
                                f"Default address does not contain {expect!r}: {card!r}"
                            )
                        _log("Using default (warehouse) address.")
                    actual_date = _fill_pickup_details(page, totals, settings, plan.pickup_date)

                    if dry_run:
                        _screenshot(page, loc, "dry_run")
                        _log(f"{totals.label}: DRY RUN complete — SCHEDULE PICKUP not clicked.")
                        if wait_at_end:
                            _wait_for_enter(
                                f"Check the {totals.label} form in the browser, then press Enter to continue… "
                            )
                        continue

                    result = _submit_and_confirm(page, loc)
                    result.update(
                        {
                            "location": loc,
                            "pickup_date": actual_date.isoformat(),
                            "packages": totals.packages,
                            "weight_lb": totals.weight_for_form,
                            "csv": upload_csv.name,
                        }
                    )
                    record_booking(plan.pickup_date, loc, result)
                    if result["status"] == "confirmed":
                        _log(f"{totals.label}: BOOKED — confirmation {result['confirmation']}")
                    else:
                        _log(
                            f"{totals.label}: submitted, but no confirmation number was found on the page. "
                            "Check the screenshot / Manage pickups. Recorded as submitted so it is not re-booked."
                        )
                    if wait_at_end:
                        _wait_for_enter("Press Enter to continue… ")
                except Exception as exc:
                    failures += 1
                    _log(f"ERROR ({totals.label}): {exc}")
                    _screenshot(page, loc, "error")
                    if wait_at_end:
                        _wait_for_enter("Check the browser, then press Enter to continue… ")

            _save_fedex_session(context, uses_persistent_profile=persistent)
        finally:
            context.close()
            if browser is not None:
                browser.close()

    return 1 if failures else 0
