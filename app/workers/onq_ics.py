"""onQ deadlines via the D2L calendar ICS feed -- no login, no scraping.

Replaces onq_worker's Playwright scrape. The old approach reused saved D2L
session cookies, but those are SESSION cookies with no expiry, so they died
roughly daily and needed a browser + MFA to refresh. The ICS feed is a tokenised
URL that just keeps working, so this needs no credentials at all.

Feed quirks this handles:
- Line folding: ICS wraps long lines with a leading space/tab continuation.
- Due times are UTC end-of-day (a Jan 4 23:59 EST deadline arrives as
  20260105T045959Z). Parsing that naively puts the deadline a day late, so we
  convert to America/Toronto before taking the calendar date.
- The feed is "All Courses" and includes a lot of non-academic noise (residence
  yoga, coffee meet-ups). Coursework is identified by LOCATION starting with a
  course code, e.g. "MREN 103 Mechatronics and Robotics Design I W26".
"""
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import obsidian_writer

ICS_URL = os.environ.get("ONQ_ICS_URL", "")
DB_PATH = str(Path(__file__).parent / "onq_deadlines.db")
CACHE_PATH = Path("/tmp/onq_feed_cache.ics")
CACHE_SECONDS = 900          # be polite to Queen's; the digest only runs daily
LOCAL_TZ = ZoneInfo("America/Toronto")

# "MREN 103 Mechatronics ...", "APSC 172 Calculus II W26"
COURSE_RE = re.compile(r"^([A-Z]{2,5})\s*(\d{3}[A-Z]?)\b")

# Order matters -- first match wins. NB "final" on its own is NOT an exam signal:
# "P1 - Final Report" is an assignment, so exams must say exam/midterm explicitly.
TYPE_HINTS = [
    ("exam", ("exam", "midterm")),
    ("quiz", ("quiz", "test")),
    ("assignment", ("assignment", "lab", "report", "workshop", "slides", "memo",
                    "submission", "dropbox", "homework", "problem set", "due",
                    "discussion", "resume", "survey", "demo")),
]


# --------------------------------------------------------------- ICS parsing
def _unfold(raw: str) -> str:
    """ICS continuation lines start with a space or tab; join them back."""
    return re.sub(r"\r?\n[ \t]", "", raw)


def _unescape(v: str) -> str:
    return (v.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")).strip()


def _parse_dt(value: str, params: str):
    """Return a date. Handles VALUE=DATE (all-day) and UTC timestamps, converting
    UTC to Toronto first so end-of-day deadlines don't land on the wrong day."""
    value = value.strip()
    try:
        if "VALUE=DATE" in params.upper() and "T" not in value:
            return datetime.strptime(value, "%Y%m%d").date()
        if value.endswith("Z"):
            utc = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return utc.astimezone(LOCAL_TZ).date()
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").date()
    except (ValueError, TypeError):
        return None


def _classify(summary: str) -> str:
    s = summary.lower()
    for type_, hints in TYPE_HINTS:
        if any(h in s for h in hints):
            return type_
    return "other"


def fetch_feed(force: bool = False) -> str:
    """Feed text, cached briefly so repeated calls don't hammer onQ."""
    if not ICS_URL:
        return ""
    if not force and CACHE_PATH.exists() and \
            time.time() - CACHE_PATH.stat().st_mtime < CACHE_SECONDS:
        return CACHE_PATH.read_text(encoding="utf-8", errors="replace")
    r = requests.get(ICS_URL, timeout=30)
    r.raise_for_status()
    try:
        CACHE_PATH.write_text(r.text, encoding="utf-8")
    except OSError:
        pass
    return r.text


def parse_events(raw: str) -> list[dict]:
    """Coursework events only, as {course, title, type, date, uid}."""
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", _unfold(raw), re.S):
        props = {}
        for line in block.strip().splitlines():
            if ":" not in line:
                continue
            head, _, value = line.partition(":")
            key, _, params = head.partition(";")
            props[key.strip().upper()] = (value, params)

        summary = _unescape(props.get("SUMMARY", ("", ""))[0])
        location = _unescape(props.get("LOCATION", ("", ""))[0])
        m = COURSE_RE.match(location)
        if not m or not summary:
            continue                      # not coursework (residence/social event)

        dt = props.get("DTSTART")
        due = _parse_dt(dt[0], dt[1]) if dt else None
        if not due:
            continue

        title = re.sub(r"\s*-\s*Due$", "", summary, flags=re.I).strip()
        out.append({
            "course": f"{m.group(1)} {m.group(2)}",
            "title": title,
            "type": _classify(summary),
            "date": due.isoformat(),
            "uid": _unescape(props.get("UID", ("", ""))[0]),
        })
    return out


# --------------------------------------------------------------- persistence
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS seen_deadlines (
        item_key TEXT PRIMARY KEY, course TEXT, title TEXT, type TEXT,
        date TEXT, first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    conn.close()


def refresh(force: bool = False) -> list[dict]:
    """Pull the feed and store anything new. Returns only the newly-seen items,
    which is what gets written to the vault (same behaviour as the old scraper)."""
    if not ICS_URL:
        return []
    init_db()
    events = parse_events(fetch_feed(force=force))

    conn = sqlite3.connect(DB_PATH)
    new_items = []
    for e in events:
        key = e["uid"] or f"{e['course']}:{e['title']}:{e['date']}"
        if conn.execute("SELECT 1 FROM seen_deadlines WHERE item_key = ?", (key,)).fetchone():
            continue
        conn.execute("INSERT OR IGNORE INTO seen_deadlines "
                     "(item_key, course, title, type, date) VALUES (?,?,?,?,?)",
                     (key, e["course"], e["title"], e["type"], e["date"]))
        new_items.append(e)
    conn.commit()
    conn.close()

    # only announce things that haven't already passed
    today = datetime.now(LOCAL_TZ).date()
    fresh = [i for i in new_items if i["date"] >= today.isoformat()]
    if fresh and obsidian_writer.VAULT_PATH:
        lines = [f"- **{i['title']}** ({i['type']}) -- {i['date']} [{i['course']}]" for i in fresh]
        obsidian_writer.append_note("Deadlines", "New deadlines found", "\n".join(lines))
    return new_items


# --------------------------------------------------------------- reads
def get_upcoming_reminders(days_ahead: int = 2) -> list[dict]:
    """Coursework due between today and `days_ahead` out, soonest first."""
    refresh()
    init_db()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT course, title, type, date FROM seen_deadlines "
                        "WHERE type IN ('assignment','exam','quiz')").fetchall()
    conn.close()

    today = datetime.now(LOCAL_TZ).date()
    horizon = today + timedelta(days=days_ahead)
    upcoming = []
    for course, title, type_, date_str in rows:
        try:
            due = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if today <= due <= horizon:
            upcoming.append({"course": course, "title": title, "type": type_,
                             "date": date_str, "days_left": (due - today).days})
    return sorted(upcoming, key=lambda x: (x["days_left"], x["course"]))


def get_reminder_touchpoints(early_days: int = 2) -> list[dict]:
    """Two-touch pattern: an early warning at `early_days` out plus a day-of
    reminder -- not a daily nag for every day in between."""
    return [r for r in get_upcoming_reminders(days_ahead=early_days)
            if r["days_left"] in (0, early_days)]


def check_feed_ok() -> tuple[bool, str]:
    """Health check replacing the old session check. The feed token can be
    revoked by regenerating it in onQ, so a 401/403 is worth alerting on."""
    if not ICS_URL:
        return False, "onQ ICS feed URL not configured (set ONQ_ICS_URL)"
    try:
        r = requests.get(ICS_URL, timeout=20)
    except requests.RequestException as e:
        return False, f"onQ calendar feed unreachable ({type(e).__name__})"
    if r.status_code != 200:
        return False, (f"onQ calendar feed returned {r.status_code} -- the token may have "
                       f"been regenerated; get a fresh Subscribe URL from onQ")
    if "BEGIN:VCALENDAR" not in r.text[:200]:
        return False, "onQ calendar feed did not return a calendar"
    return True, ""
