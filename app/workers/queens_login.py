"""One-time, manual login capture for the Queen's SWEP career portal.

Run this yourself. It opens a REAL, VISIBLE browser window. Log in with
your Queen's SSO (and MFA) exactly as normal -- nothing here sees or
handles your credentials. Once you're on the SWEP portal, come back to the
terminal and press Enter; your session cookies get saved locally so future
runs can reuse them without logging in again.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

SWEP_URL = "https://careers.sso.queensu.ca/myAccount/swep/SWEP.htm"
COOKIES_PATH = str(Path(__file__).parent / "queens_session.json")

with sync_playwright() as p:
    # Microsoft SSO blocks automation-controlled browsers ("this browser may not
    # be secure"). These flags hide the automation banner so the login goes through.
    browser = p.chromium.launch(
        headless=False, channel="chrome",
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    context = browser.new_context()
    page = context.new_page()
    page.goto(SWEP_URL, timeout=60000)

    input("Log in with Queen's SSO + MFA in the browser window, then press "
          "Enter here once you're on the SWEP portal...")

    context.storage_state(path=COOKIES_PATH)
    print(f"Session saved to {COOKIES_PATH}")
    browser.close()
