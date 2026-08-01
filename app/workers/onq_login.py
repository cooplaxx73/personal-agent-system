"""One-time, manual login capture for onQ.

Run this yourself. It opens a REAL, VISIBLE browser window. Log in and
complete MFA exactly as you normally would -- nothing here sees or handles
your credentials. Once you land on the onQ dashboard, come back to this
terminal and press Enter; your session cookies get saved locally so future
runs can reuse them without logging in again.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

ONQ_URL = "https://onq.queensu.ca/d2l/home"  # correct me if this isn't right
COOKIES_PATH = str(Path(__file__).parent / "onq_session.json")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, channel="chrome")
    context = browser.new_context()
    page = context.new_page()
    page.goto(ONQ_URL, timeout=60000)

    input("Log in and complete MFA in the browser window, then press Enter here once you're on your onQ dashboard...")

    context.storage_state(path=COOKIES_PATH)
    print(f"Session saved to {COOKIES_PATH}")
    browser.close()
