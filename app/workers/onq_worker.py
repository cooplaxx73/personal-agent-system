"""Read onQ course pages using a saved login session, and extract deadlines
via the local LLM cleaner.

NOTE: this is a first-draft skeleton. It's built generically around common
Brightspace/D2L page patterns, but every school's onQ instance is customized
differently -- expect to debug the actual selectors together once you've
run onq_login.py and we can see your real dashboard structure.

Runs deliberately slowly and one page at a time -- this reads only your own
logged-in view, never in parallel, with real pauses between pages.
"""

import time
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from dateutil import parser as dateparser
from playwright.sync_api import sync_playwright
from llm_clean import extract_deadlines
import obsidian_writer

ONQ_URL = "https://onq.queensu.ca/d2l/home"
COOKIES_PATH = str(Path(__file__).parent / "onq_session.json")
DB_PATH = Path(__file__).parent / "onq_deadlines.db"

REQUEST_DELAY_SECONDS = 4  # deliberate pacing, never hammer the site


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_deadlines (
            item_key TEXT PRIMARY KEY,
            course TEXT,
            title TEXT,
            type TEXT,
            date TEXT,
            first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def is_new(item_key: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM seen_deadlines WHERE item_key = ?", (item_key,)).fetchone()
    conn.close()
    return row is None


def mark_seen(item_key: str, course: str, title: str, type_: str, date: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO seen_deadlines (item_key, course, title, type, date) VALUES (?, ?, ?, ?, ?)",
        (item_key, course, title, type_, date),
    )
    conn.commit()
    conn.close()


def run():
    init_db()
    new_items = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(storage_state=COOKIES_PATH)
        page = context.new_page()

        page.goto(ONQ_URL, timeout=30000)
        time.sleep(REQUEST_DELAY_SECONDS)

        # Best-effort: Brightspace course homepages commonly live under /d2l/home/<id>
        course_links = set()
        for a in page.query_selector_all("a[href*='/d2l/home/']"):
            href = a.get_attribute("href")
            if href and re.search(r"/d2l/home/\d+", href):
                course_links.add(href)

        print(f"Found {len(course_links)} candidate course links (verify this looks right)")

        for href in course_links:
            url = href if href.startswith("http") else f"https://onq.queensu.ca{href}"
            page.goto(url, timeout=30000)
            time.sleep(REQUEST_DELAY_SECONDS)

            raw_text = page.inner_text("body")
            items = extract_deadlines(raw_text[:6000])  # cap length fed to the LLM

            for item in items:
                item_key = f"{item.get('course', url)}:{item.get('title', '')}:{item.get('date', '')}"
                if is_new(item_key):
                    mark_seen(item_key, item.get("course", ""), item.get("title", ""),
                              item.get("type", ""), item.get("date", ""))
                    new_items.append(item)

        browser.close()

    if new_items and obsidian_writer.VAULT_PATH:
        lines = [f"- **{i.get('title', '')}** ({i.get('type', '')}) -- {i.get('date', '')} [{i.get('course', '')}]"
                 for i in new_items]
        obsidian_writer.append_note("Deadlines", "New deadlines found", "\n".join(lines))

    return new_items


def get_upcoming_reminders(days_ahead: int = 2) -> list[dict]:
    """Deadlines (assignments/exams) due between now and `days_ahead` from now,
    inclusive -- meant to be checked once a day and pushed as a reminder."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT course, title, type, date FROM seen_deadlines WHERE type IN ('assignment', 'exam')"
    ).fetchall()
    conn.close()

    now = datetime.now()
    horizon = now + timedelta(days=days_ahead)
    upcoming = []

    for course, title, type_, date_str in rows:
        try:
            due = dateparser.parse(date_str)
        except (ValueError, TypeError, OverflowError):
            continue
        if now.date() <= due.date() <= horizon.date():
            upcoming.append({
                "course": course,
                "title": title,
                "type": type_,
                "date": due.isoformat(),
                "days_left": (due.date() - now.date()).days,
            })

    return sorted(upcoming, key=lambda x: x["days_left"])


def check_session_valid() -> bool:
    """Quick check: does the saved onQ session still work, or has it
    expired and need you to run onq_login.py again?"""
    if not Path(COOKIES_PATH).exists():
        return False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(storage_state=COOKIES_PATH)
        page = context.new_page()
        page.goto(ONQ_URL, timeout=30000)
        time.sleep(2)
        current_url = page.url
        page_text = page.inner_text("body")[:500].lower()
        browser.close()

    looks_like_login = (
        "sign in" in page_text or "log in" in page_text
        or "password" in page_text or "login" in current_url.lower()
    )
    return not looks_like_login


def get_reminder_touchpoints(early_days: int = 2) -> list[dict]:
    """The specific two-touch reminder pattern: an early warning at
    `early_days` out, plus a day-of reminder -- not a daily nag for every
    day in between."""
    all_upcoming = get_upcoming_reminders(days_ahead=early_days)
    return [r for r in all_upcoming if r["days_left"] in (0, early_days)]


if __name__ == "__main__":
    import json
    results = run()
    with open("onq_results.txt", "w", encoding="utf-8") as f:
        f.write(json.dumps(results, indent=2))
    print(f"done, {len(results)} new items, see onq_results.txt")
