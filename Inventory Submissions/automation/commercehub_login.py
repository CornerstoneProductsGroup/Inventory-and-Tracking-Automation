"""
CommerceHub / Rithum DSM login (Playwright sync + async).

New MUI login (2026): identifier → Continue → password → Sign in.
Falls back to legacy Auth0 / j_username fields when present.
"""
from __future__ import annotations

import re
import time
from typing import Callable

IDENTIFIER_INPUT_SELECTORS: tuple[str, ...] = (
    'input[data-test-id="input-identifier"]',
    'input[name="identifier"]',
    "#username",
    "input[name='username']",
    "#j_username",
    "input[name='j_username']",
    'input[type="email"]',
)

PASSWORD_INPUT_SELECTORS: tuple[str, ...] = (
    'input[data-test-id="input-password"]',
    'input[name="password"]',
    "#password",
    "#j_password",
    "input[name='j_password']",
    'input[type="password"]',
)

CONTINUE_BUTTON_SELECTORS: tuple[str, ...] = (
    "button:has-text('Continue')",
    "[role='button']:has-text('Continue')",
    "button._button-login-id",
    "button[data-action-button-primary='true']",
    "button[type='submit']:has-text('Continue')",
)

SIGN_IN_BUTTON_SELECTORS: tuple[str, ...] = (
    'button[data-test-id="submit-btn"]',
    'button[aria-label="Sign in"]',
    'button[aria-label="Sign In"]',
    "button:has-text('Sign in')",
    "button:has-text('Sign In')",
    "button._button-login-password",
    "button[type='submit']:has-text('Sign in')",
)

LEGACY_SUBMIT_SELECTORS: tuple[str, ...] = (
    "#loginButton",
    "input[type='submit'][name='submit']",
    "input[type='submit'][value*='Log In']",
    "input[type='submit'][value*='Login']",
    "button[type='submit']",
    "button:has-text('Log In')",
    "button:has-text('Login')",
    "input[type='submit']",
)

# Post-login profile chooser (2026 MUI identity links + legacy DSM anchors).
PROFILE_LINK_SELECTORS: tuple[str, ...] = (
    "a[href*='handleLogin.do'][href*='identityKey=']",
    "a[class*='_identityLink']",
    "a.application-identity-item",
)

POST_LOGIN_READY_SELECTORS: tuple[str, ...] = PROFILE_LINK_SELECTORS + (
    '[data-test="dsmMenu-orders"]',
)

COMMERCEHUB_HOME_READY_SELECTOR = '[data-test="dsmMenu-orders"]'

DEFAULT_PROFILE_TEXT = "Cornerstone Products Group"


def _log_msg(log: Callable[[str], None] | None, msg: str) -> None:
    if log:
        log(msg)


def _sync_first_visible(page, selectors: tuple[str, ...], *, timeout_ms: int):
    per_sel = max(400, timeout_ms // max(1, len(selectors)))
    last_err: Exception | None = None
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=per_sel)
            return loc
        except Exception as exc:
            last_err = exc
            continue
    if last_err is not None:
        raise last_err
    raise RuntimeError(f"CommerceHub login: no visible control for {selectors!r}")


def _sync_try_visible(page, selectors: tuple[str, ...], *, timeout_ms: int = 800):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except Exception:
            continue
    return None


def _sync_click_first(page, selectors: tuple[str, ...], *, timeout_ms: int) -> None:
    loc = _sync_first_visible(page, selectors, timeout_ms=timeout_ms)
    try:
        loc.click(timeout=min(8_000, timeout_ms))
    except Exception:
        loc.click(timeout=min(8_000, timeout_ms), force=True)


async def _async_first_visible(page, selectors: tuple[str, ...], *, timeout_ms: int):
    per_sel = max(400, timeout_ms // max(1, len(selectors)))
    last_err: Exception | None = None
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            await loc.wait_for(state="visible", timeout=per_sel)
            return loc
        except Exception as exc:
            last_err = exc
            continue
    if last_err is not None:
        raise last_err
    raise RuntimeError(f"CommerceHub login: no visible control for {selectors!r}")


async def _async_try_visible(page, selectors: tuple[str, ...], *, timeout_ms: int = 800):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() == 0:
                continue
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return loc
        except Exception:
            continue
    return None


async def _async_click_first(page, selectors: tuple[str, ...], *, timeout_ms: int) -> None:
    loc = await _async_first_visible(page, selectors, timeout_ms=timeout_ms)
    try:
        await loc.click(timeout=min(8_000, timeout_ms))
    except Exception:
        await loc.click(timeout=min(8_000, timeout_ms), force=True)


def perform_commercehub_login(
    page,
    username: str,
    password: str,
    *,
    timeout_ms: int = 60_000,
    log: Callable[[str], None] | None = None,
) -> None:
    """Fill CommerceHub credentials on the current login page (sync Playwright Page)."""
    if not username or not password:
        raise ValueError("CommerceHub username and password are required.")

    _log_msg(log, "Entering CommerceHub username…")
    user_loc = _sync_first_visible(page, IDENTIFIER_INPUT_SELECTORS, timeout_ms=timeout_ms)
    user_loc.fill(username)

    pwd_loc = _sync_try_visible(page, PASSWORD_INPUT_SELECTORS, timeout_ms=900)
    if pwd_loc is not None:
        _log_msg(log, "Legacy single-page login — entering password…")
        pwd_loc.fill(password)
        try:
            _sync_click_first(
                page,
                SIGN_IN_BUTTON_SELECTORS + LEGACY_SUBMIT_SELECTORS,
                timeout_ms=10_000,
            )
        except Exception:
            page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded")
        return

    _log_msg(log, "Clicking Continue…")
    _sync_click_first(page, CONTINUE_BUTTON_SELECTORS, timeout_ms=min(20_000, timeout_ms))

    _log_msg(log, "Entering password…")
    pwd_loc = _sync_first_visible(page, PASSWORD_INPUT_SELECTORS, timeout_ms=timeout_ms)
    pwd_loc.fill(password)

    _log_msg(log, "Clicking Sign in…")
    try:
        _sync_click_first(page, SIGN_IN_BUTTON_SELECTORS, timeout_ms=min(20_000, timeout_ms))
    except Exception:
        try:
            _sync_click_first(page, LEGACY_SUBMIT_SELECTORS, timeout_ms=8_000)
        except Exception:
            page.keyboard.press("Enter")

    page.wait_for_load_state("domcontentloaded")


async def perform_commercehub_login_async(
    page,
    username: str,
    password: str,
    *,
    timeout_ms: int = 60_000,
    log: Callable[[str], None] | None = None,
) -> None:
    """Async Playwright variant used by invoice report export."""
    if not username or not password:
        raise ValueError("CommerceHub username and password are required.")

    _log_msg(log, "Entering CommerceHub username…")
    user_loc = await _async_first_visible(page, IDENTIFIER_INPUT_SELECTORS, timeout_ms=timeout_ms)
    await user_loc.fill(username)

    pwd_loc = await _async_try_visible(page, PASSWORD_INPUT_SELECTORS, timeout_ms=900)
    if pwd_loc is not None:
        _log_msg(log, "Legacy single-page login — entering password…")
        await pwd_loc.fill(password)
        try:
            await _async_click_first(
                page,
                SIGN_IN_BUTTON_SELECTORS + LEGACY_SUBMIT_SELECTORS,
                timeout_ms=10_000,
            )
        except Exception:
            await page.keyboard.press("Enter")
        await page.wait_for_load_state("domcontentloaded")
        return

    _log_msg(log, "Clicking Continue…")
    await _async_click_first(page, CONTINUE_BUTTON_SELECTORS, timeout_ms=min(20_000, timeout_ms))

    _log_msg(log, "Entering password…")
    pwd_loc = await _async_first_visible(page, PASSWORD_INPUT_SELECTORS, timeout_ms=timeout_ms)
    await pwd_loc.fill(password)

    _log_msg(log, "Clicking Sign in…")
    try:
        await _async_click_first(page, SIGN_IN_BUTTON_SELECTORS, timeout_ms=min(20_000, timeout_ms))
    except Exception:
        try:
            await _async_click_first(page, LEGACY_SUBMIT_SELECTORS, timeout_ms=8_000)
        except Exception:
            await page.keyboard.press("Enter")

    await page.wait_for_load_state("domcontentloaded")


# Selenium helpers (standalone depot_tracking1 / home_depot_invoice scripts).
SELENIUM_IDENTIFIER_CSS = (
    'input[data-test-id="input-identifier"], input[name="identifier"], '
    "#username, input[name='username'], #j_username"
)
SELENIUM_PASSWORD_CSS = (
    'input[data-test-id="input-password"], input[name="password"], '
    "#password, #j_password"
)
SELENIUM_CONTINUE_XPATH = (
    "//button[contains(normalize-space(.), 'Continue')]"
    " | //*[@role='button'][contains(normalize-space(.), 'Continue')]"
)
SELENIUM_SIGN_IN_CSS = (
    'button[data-test-id="submit-btn"], button[aria-label="Sign in"], '
    'button[aria-label="Sign In"], button._button-login-password'
)


def perform_commercehub_selenium_login(driver, username: str, password: str, *, wait_sec: int = 30) -> None:
    """Two-step CommerceHub login for legacy Selenium scripts."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, wait_sec)
    id_el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SELENIUM_IDENTIFIER_CSS)))
    id_el.clear()
    id_el.send_keys(username)

    try:
        pwd_el = driver.find_element(By.CSS_SELECTOR, SELENIUM_PASSWORD_CSS)
        if pwd_el.is_displayed():
            pwd_el.clear()
            pwd_el.send_keys(password)
            sign_in = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, SELENIUM_SIGN_IN_CSS)))
            sign_in.click()
            return
    except Exception:
        pass

    continue_btn = wait.until(EC.element_to_be_clickable((By.XPATH, SELENIUM_CONTINUE_XPATH)))
    continue_btn.click()

    pwd_el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SELENIUM_PASSWORD_CSS)))
    pwd_el.clear()
    pwd_el.send_keys(password)

    sign_in = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, SELENIUM_SIGN_IN_CSS)))
    sign_in.click()


SELENIUM_PROFILE_CSS = (
    "a[href*='handleLogin.do'][href*='identityKey='], "
    "a[class*='_identityLink'], a.application-identity-item"
)


def _profile_text_pattern(profile_text: str | None) -> re.Pattern[str] | None:
    needle = (profile_text or "").strip()
    if not needle:
        return None
    return re.compile(re.escape(needle), re.I)


def _sync_try_profile_link(page, profile_text: str | None = None):
    pattern = _profile_text_pattern(profile_text)
    for sel in PROFILE_LINK_SELECTORS:
        base = page.locator(sel)
        candidates = [base]
        if pattern is not None:
            candidates.insert(0, base.filter(has_text=pattern))
        for loc in candidates:
            try:
                if loc.count() == 0:
                    continue
                if loc.first.is_visible(timeout=400):
                    return loc.first
            except Exception:
                continue
    return None


async def _async_try_profile_link(page, profile_text: str | None = None):
    pattern = _profile_text_pattern(profile_text)
    for sel in PROFILE_LINK_SELECTORS:
        base = page.locator(sel)
        candidates = [base]
        if pattern is not None:
            candidates.insert(0, base.filter(has_text=pattern))
        for loc in candidates:
            try:
                if await loc.count() == 0:
                    continue
                if await loc.first.is_visible(timeout=400):
                    return loc.first
            except Exception:
                continue
    return None


def _sync_on_commercehub_home(page, *, timeout_ms: int = 2_500) -> bool:
    try:
        page.locator(COMMERCEHUB_HOME_READY_SELECTOR).first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except Exception:
        return False


async def _async_on_commercehub_home(page, *, timeout_ms: int = 2_500) -> bool:
    try:
        await page.locator(COMMERCEHUB_HOME_READY_SELECTOR).first.wait_for(
            state="visible", timeout=timeout_ms
        )
        return True
    except Exception:
        return False


def click_commercehub_profile(
    page,
    profile_text: str = DEFAULT_PROFILE_TEXT,
    *,
    profile_url: str | None = None,
    timeout_ms: int = 20_000,
    log: Callable[[str], None] | None = None,
) -> bool:
    """
    Open the DSM profile after credential login. Returns True if a profile was clicked
    or profile_url was opened; False if already on home (no chooser).
    """
    if profile_url:
        _log_msg(log, "Opening CommerceHub profile via URL…")
        page.goto(profile_url, wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        return True

    if _sync_on_commercehub_home(page, timeout_ms=3_000):
        _log_msg(log, "CommerceHub home already loaded; skipping profile click.")
        return False

    _log_msg(log, f"Choosing CommerceHub profile: {profile_text!r}…")
    deadline = time.monotonic() + timeout_ms / 1000.0
    profile_loc = None
    while time.monotonic() < deadline and profile_loc is None:
        profile_loc = _sync_try_profile_link(page, profile_text)
        if profile_loc is None:
            if _sync_on_commercehub_home(page, timeout_ms=800):
                _log_msg(log, "CommerceHub home loaded while waiting for profile chooser.")
                return False
            page.wait_for_timeout(250)

    if profile_loc is None:
        profile_loc = _sync_try_profile_link(page, None)
    if profile_loc is None:
        raise RuntimeError(
            f"CommerceHub profile chooser not found (looked for {profile_text!r}). "
            "Set COMMERCEHUB_PROFILE_URL to the full handleLogin.do?identityKey=… link."
        )

    try:
        profile_loc.click(timeout=min(12_000, timeout_ms))
    except Exception:
        profile_loc.click(timeout=min(12_000, timeout_ms), force=True)
    page.wait_for_load_state("domcontentloaded")
    _log_msg(log, "CommerceHub profile clicked.")
    return True


async def click_commercehub_profile_async(
    page,
    profile_text: str = DEFAULT_PROFILE_TEXT,
    *,
    profile_url: str | None = None,
    timeout_ms: int = 20_000,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Async variant for invoice report export."""
    if profile_url:
        _log_msg(log, "Opening CommerceHub profile via URL…")
        await page.goto(profile_url, wait_until="domcontentloaded")
        await page.wait_for_load_state("domcontentloaded")
        return True

    if await _async_on_commercehub_home(page, timeout_ms=3_000):
        _log_msg(log, "CommerceHub home already loaded; skipping profile click.")
        return False

    _log_msg(log, f"Choosing CommerceHub profile: {profile_text!r}…")
    deadline = time.monotonic() + timeout_ms / 1000.0
    profile_loc = None
    while time.monotonic() < deadline and profile_loc is None:
        profile_loc = await _async_try_profile_link(page, profile_text)
        if profile_loc is None:
            if await _async_on_commercehub_home(page, timeout_ms=800):
                _log_msg(log, "CommerceHub home loaded while waiting for profile chooser.")
                return False
            await page.wait_for_timeout(250)

    if profile_loc is None:
        profile_loc = await _async_try_profile_link(page, None)
    if profile_loc is None:
        raise RuntimeError(
            f"CommerceHub profile chooser not found (looked for {profile_text!r}). "
            "Set COMMERCEHUB_PROFILE_URL to the full handleLogin.do?identityKey=… link."
        )

    try:
        await profile_loc.click(timeout=min(12_000, timeout_ms))
    except Exception:
        await profile_loc.click(timeout=min(12_000, timeout_ms), force=True)
    await page.wait_for_load_state("domcontentloaded")
    _log_msg(log, "CommerceHub profile clicked.")
    return True


def perform_commercehub_selenium_profile_click(
    driver,
    profile_text: str = DEFAULT_PROFILE_TEXT,
    *,
    wait_sec: int = 30,
) -> None:
    """Click profile row on CommerceHub identity chooser (Selenium)."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    wait = WebDriverWait(driver, wait_sec)
    links = wait.until(
        EC.presence_of_all_elements_located((By.CSS_SELECTOR, SELENIUM_PROFILE_CSS))
    )
    needle = (profile_text or "").strip().lower()
    chosen = None
    for link in links:
        try:
            if not link.is_displayed():
                continue
            txt = (link.text or "").strip()
            if not needle or needle in txt.lower():
                chosen = link
                break
        except Exception:
            continue
    if chosen is None and links:
        chosen = links[0]
    if chosen is None:
        raise RuntimeError(f"CommerceHub profile link not found for {profile_text!r}.")
    wait.until(EC.element_to_be_clickable(chosen))
    chosen.click()
