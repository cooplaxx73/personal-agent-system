"""Read job postings from the Queen's SWEP career portal using a saved
login session.

NOTE: this is a first-draft skeleton. SWEP's listing page structure is
unknown until we see it logged in, so the extraction is generic -- expect
to debug the actual selectors together after you've run queens_login.py.

Reads only your own logged-in view, one page at a time, with real pauses.
Writes new postings to the vault (Jobs) and dedupes via the shared store.
"""

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from matching import matches_criteria, looks_blocked
from job_store import init_db, is_new, mark_seen
import obsidian_writer

SWEP_URL = "https://careers.sso.queensu.ca/myAccount/swep/SWEP.htm"
COOKIES_PATH = str(Path(__file__).parent / "queens_session.json")
REQUEST_DELAY_SECONDS = 3


def _extract_postings(page) -> list[dict]:
    """Best-effort generic extraction. SWEP portals are often table-based,
    so we try rows first, then fall back to links. We'll tighten this once
    we see the real page."""
    postings = []

    # Try table rows (common for these portals)
    rows = page.query_selector_all("table tr")
    for row in rows:
        cells = row.query_selector_all("td")
        if len(cells) < 2:
            continue
        text = row.inner_text().strip().replace("\n", " ")
        link_el = row.query_selector("a[href]")
        href = link_el.get_attribute("href") if link_el else None
        title = link_el.inner_text().strip() if link_el else cells[0].inner_text().strip()
        if title:
            postings.append({"title": title, "raw": text, "link": href})

    return postings


def check_session_valid() -> bool:
    """Does the saved Queen's SWEP session still work, or has it expired and
    need queens_login.py run again? Mirrors onq_worker.check_session_valid --
    without this an expired session just returns zero postings and looks like
    "no jobs today", which is indistinguishable from working correctly."""
    if not Path(COOKIES_PATH).exists():
        return False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(storage_state=COOKIES_PATH)
            page = context.new_page()
            page.goto(SWEP_URL, timeout=30000)
            time.sleep(2)
            current_url = page.url
            page_text = page.inner_text("body")[:500].lower()
            browser.close()
    except Exception:  # noqa: BLE001 - treat an unreachable portal as "can't confirm"
        return True

    if looks_blocked(page_text):
        # NOT an expired session -- the WAF refused us. Saying "session expired"
        # here would send the user off to re-login for no reason.
        return False

    looks_like_login = (
        "sign in" in page_text or "log in" in page_text
        or "password" in page_text or "netid" in page_text
        or "login" in current_url.lower()
    )
    return not looks_like_login


def check_access() -> tuple[bool, str]:
    """(ok, problem) for the health check -- separates 'blocked by Cloudflare'
    from 'session expired', because the fixes are completely different."""
    if not Path(COOKIES_PATH).exists():
        return False, "Queen's session not set up -- run queens_login.py"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"])
            page = browser.new_context(storage_state=COOKIES_PATH).new_page()
            page.goto(SWEP_URL, timeout=30000)
            time.sleep(2)
            text, url = page.inner_text("body")[:1500], page.url
            browser.close()
    except Exception:  # noqa: BLE001 - portal down is not the user's problem to fix
        return True, ""
    if looks_blocked(text):
        return False, ("Queen's job portal is blocking this server (Cloudflare) -- "
                       "SWEP scraping cannot run from the cloud VM")
    low = text.lower()
    if ("sign in" in low or "log in" in low or "password" in low
            or "netid" in low or "login" in url.lower()):
        return False, "Queen's SWEP session has expired -- run queens_login.py again"
    return True, ""


def run(
    role_keywords: list[str] | None = None,
    require_intern: bool = False,   # SWEP is student-focused already
    locations: list[str] | None = None,
):
    if not Path(COOKIES_PATH).exists():
        return {"error": "Queen's session not set up yet -- run queens_login.py first"}

    init_db()
    new_matches = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(storage_state=COOKIES_PATH)
        page = context.new_page()

        page.goto(SWEP_URL, timeout=30000)
        time.sleep(REQUEST_DELAY_SECONDS)

        postings = _extract_postings(page)
        print(f"Found {len(postings)} candidate rows (verify this looks right)")

        for post in postings:
            title = post.get("title", "")
            link = post.get("link") or SWEP_URL
            job_key = f"queens:{title}:{link}"
            # SWEP location isn't reliably structured; match on title + raw text
            haystack = f"{title} {post.get('raw', '')}"
            if matches_criteria(haystack, "kingston", role_keywords, require_intern,
                                locations or ["kingston", "toronto", "remote", "ontario"]) \
                    and is_new(job_key):
                mark_seen(job_key, "queens-swep", title, "Queen's SWEP", "Kingston", link)
                new_matches.append({"source": "queens-swep", "title": title, "link": link})

        browser.close()

    if new_matches and obsidian_writer.VAULT_PATH:
        lines = [f"- **{m['title']}** (Queen's SWEP) -- {m['link']}" for m in new_matches]
        obsidian_writer.append_note("Jobs", "New postings found (Queen's SWEP)", "\n".join(lines))

    return new_matches


if __name__ == "__main__":
    import json
    result = run()
    with open("queens_results.txt", "w", encoding="utf-8") as f:
        f.write(json.dumps(result, indent=2))
    print("done, see queens_results.txt")
