from datetime import datetime
from pathlib import Path

from automation.commercehub_timeouts import (
    chain_fast,
    navigation_timeout_ms,
    rithum_ibl_timeout_ms,
    rithum_profile_timeout_ms,
)
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from automation.commercehub_login import (
    DEFAULT_PROFILE_TEXT,
    click_commercehub_profile,
    perform_commercehub_login,
)
from automation.config import load_settings


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _save_screenshot(page, name: str) -> None:
    shots_dir = Path("screenshots")
    shots_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shots_dir / f"{_timestamp()}_{name}.png"), full_page=True)


def _click_first_available_profile(page, timeout_ms: int) -> None:
    if click_commercehub_profile(page, DEFAULT_PROFILE_TEXT, timeout_ms=timeout_ms):
        return

    profile_candidates = [
        "button:has-text('Select')",
        "input[type='submit'][value*='Select']",
        "a:has-text('Select')",
        "button:has-text('Continue')",
        "input[type='submit'][value*='Continue']",
        "a:has-text('Continue')",
    ]

    for selector in profile_candidates:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=1500):
                locator.click(timeout=timeout_ms)
                return
        except Exception:
            continue


def _perform_login(page, username: str, password: str, timeout_ms: int) -> None:
    perform_commercehub_login(page, username, password, timeout_ms=timeout_ms)


def run_rithum_inventory_on_authenticated_page(page, settings) -> None:
    """
    Submit inventory update on DSM after the user is already logged in and has a session.
    If the profile chooser is visible (fresh login / hub landing), clicks Cornerstone (or first profile).
    If it is not shown — e.g. chained run after Lowe's login already opened a session — skips straight to IBL.
    """
    chain_fast_mode = chain_fast()
    nav_ms = navigation_timeout_ms()
    try:
        if chain_fast_mode:
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(350)
        else:
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(900)

        profile_ms = rithum_profile_timeout_ms()
        try:
            click_commercehub_profile(page, DEFAULT_PROFILE_TEXT, timeout_ms=profile_ms)
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            print(
                "Rithum inventory: profile chooser not visible (session already inside app). "
                "Opening inventory update directly."
            )

        page.goto(
            "https://dsm.commercehub.com/dsm/gotoUpdateInventory.do",
            wait_until="domcontentloaded",
            timeout=nav_ms,
        )
        ibl_wait = rithum_ibl_timeout_ms()
        page.locator("#selectAllIBL").wait_for(state="visible", timeout=ibl_wait)
        page.locator("#selectAllIBL").check()
        page.locator("#iblsubmit").click()
        page.wait_for_load_state("domcontentloaded")

        page.locator("input[name='skudates'][value='1']").wait_for(state="visible", timeout=ibl_wait)
        page.locator("input[name='skudates'][value='1']").check()
        _save_screenshot(page, "pre_submit")

        page.locator("#submitButton").wait_for(state="visible", timeout=ibl_wait)
        page.locator("#submitButton").click()
        page.wait_for_load_state("domcontentloaded")
        _save_screenshot(page, "submitted")

        print("Rithum inventory update submitted successfully.")
    except PlaywrightTimeoutError as exc:
        _save_screenshot(page, "timeout_error")
        raise RuntimeError(f"Timed out during automation: {exc}") from exc
    except Exception as exc:
        _save_screenshot(page, "general_error")
        raise RuntimeError(f"Automation failed: {exc}") from exc


def run_rithum_inventory_update() -> None:
    settings = load_settings()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=settings.headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(settings.timeout_ms)

        try:
            page.goto(settings.rithum_url, wait_until="domcontentloaded")
            _perform_login(page, settings.rithum_username, settings.rithum_password, settings.timeout_ms)
            _save_screenshot(page, "after_login")

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(3000)

            run_rithum_inventory_on_authenticated_page(page, settings)
        except PlaywrightTimeoutError as exc:
            _save_screenshot(page, "timeout_error")
            raise RuntimeError(f"Timed out during automation: {exc}") from exc
        except Exception as exc:
            _save_screenshot(page, "general_error")
            raise RuntimeError(f"Automation failed: {exc}") from exc
        finally:
            context.close()
            browser.close()
