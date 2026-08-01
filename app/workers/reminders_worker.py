"""Reminders + to-dos store, and the logic that decides what to notify when.

Three kinds of reminder:
- 'once'     : fire one time at a specific date + time (e.g. "call prof 3pm Fri")
- 'daily'    : recurring every day at a time (e.g. "review new postings 9am")
- 'deadline' : a due date with NO point ping -- it surfaces in the morning
               digest starting `lead_days` before the due date. The main bot's
               LLM sets lead_days adaptively (big task -> larger window).

Delivery has two channels, both sent by the notifier bot:
1. Point pings  -- 'once'/'daily' items fire at their exact fire_time.
2. Morning digest (DIGEST_TIME) -- ONE consolidated message rolling up daily
   to-dos, deadlines inside their window, and any once-item due today with no
   specific time. Items whose fire_time IS the digest time ride the digest
   instead of firing separately, so you never get a ping + a digest line.
"""

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from dateutil import parser as dateparser

# DB path is overridable via env so this can run in a container on the VM;
# defaults to sitting next to this file (unchanged behaviour on the PC).
DB_PATH = Path(os.environ.get("REMINDERS_DB") or (Path(__file__).parent / "reminders.db"))
DIGEST_TIME = "08:00"  # when the consolidated morning digest goes out
PURGE_GRACE_DAYS = 3   # keep past/completed reminders this long, then hard-delete


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            kind TEXT NOT NULL,                 -- once | daily | deadline
            due_date TEXT,                      -- YYYY-MM-DD (once, deadline)
            fire_time TEXT,                     -- HH:MM (once, daily)
            lead_days INTEGER DEFAULT 0,        -- deadline: days before to start showing
            active INTEGER DEFAULT 1,
            done INTEGER DEFAULT 0,
            last_fired TEXT,                    -- YYYY-MM-DD, blocks same-day re-fire
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def add(text: str, kind: str = "once", due_date: str | None = None,
        fire_time: str | None = None, lead_days: int = 0) -> dict:
    """Store a reminder. `due_date` YYYY-MM-DD, `fire_time` HH:MM (24h)."""
    kind = kind if kind in ("once", "daily", "deadline") else "once"
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO reminders (text, kind, due_date, fire_time, lead_days) "
        "VALUES (?, ?, ?, ?, ?)",
        (text, kind, due_date, fire_time, lead_days or 0))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return {"id": rid, "text": text, "kind": kind, "due_date": due_date,
            "fire_time": fire_time, "lead_days": lead_days or 0}


def list_active() -> list[dict]:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, text, kind, due_date, fire_time, lead_days, done "
        "FROM reminders WHERE active = 1 AND done = 0 "
        "ORDER BY due_date IS NULL, due_date, fire_time").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def complete(reminder_id: int) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    # record the completion date too, so purge can time the grace window from it
    cur = conn.execute("UPDATE reminders SET done = 1, last_fired = ? WHERE id = ?",
                       (datetime.now().strftime("%Y-%m-%d"), reminder_id))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def delete(reminder_id: int) -> bool:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def _today(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def get_due_now(now: datetime | None = None) -> list[dict]:
    """Point reminders ('once'/'daily') firing this minute. Marks them fired so
    the every-minute poll won't double-send; 'once' items are also completed."""
    now = now or datetime.now()
    hhmm, today = now.strftime("%H:%M"), _today(now)
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM reminders WHERE active = 1 AND done = 0 "
        "AND fire_time IS NOT NULL AND fire_time != '' AND fire_time != ? "
        "AND fire_time = ? AND (last_fired IS NULL OR last_fired != ?)",
        (DIGEST_TIME, hhmm, today)).fetchall()

    due = []
    for r in rows:
        # 'once' only fires on its due date (or if no date, treat as today)
        if r["kind"] == "once" and r["due_date"] and r["due_date"] != today:
            continue
        due.append({"id": r["id"], "text": r["text"], "kind": r["kind"]})
        conn.execute("UPDATE reminders SET last_fired = ? WHERE id = ?", (today, r["id"]))
        if r["kind"] == "once":
            conn.execute("UPDATE reminders SET done = 1 WHERE id = ?", (r["id"],))
    conn.commit()
    conn.close()
    return due


def get_digest_items(now: datetime | None = None) -> list[str]:
    """My reminders that belong in the morning digest: daily to-dos, deadlines
    inside their lead window, and once-items due today with no specific time."""
    now = now or datetime.now()
    today = now.date()
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM reminders WHERE active = 1 AND done = 0").fetchall()
    conn.close()

    def rides_digest(ft):  # no explicit time, or time == digest time
        return not ft or ft == DIGEST_TIME

    lines = []
    for r in rows:
        kind, ft = r["kind"], r["fire_time"]
        if kind == "daily" and rides_digest(ft):
            lines.append(f"[daily] {r['text']}")
        elif kind == "once" and rides_digest(ft) and r["due_date"]:
            try:
                if dateparser.parse(r["due_date"]).date() == today:
                    lines.append(f"[today] {r['text']}")
            except (ValueError, TypeError, OverflowError):
                pass
        elif kind == "deadline" and r["due_date"]:
            try:
                due = dateparser.parse(r["due_date"]).date()
            except (ValueError, TypeError, OverflowError):
                continue
            days_left = (due - today).days
            if 0 <= days_left <= (r["lead_days"] or 0):
                when = "TODAY" if days_left == 0 else f"{days_left}d left"
                lines.append(f"[{when}] {r['text']} (due {r['due_date']})")
    return lines


def purge_expired(now: datetime | None = None) -> int:
    """Hard-delete reminders that are well past, after a PURGE_GRACE_DAYS buffer:
    - completed items (timed from their completion date)
    - 'once'/'deadline' whose date has passed (timed from the due date)
    'daily' reminders are never purged -- they're meant to recur. Returns how
    many rows were deleted. Runs once a day (via the morning digest) and can be
    called directly through /reminders/purge.
    """
    now = now or datetime.now()
    cutoff = now.date() - timedelta(days=PURGE_GRACE_DAYS)
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, kind, due_date, done, last_fired FROM reminders").fetchall()

    def as_date(s):
        if not s:
            return None
        try:
            return dateparser.parse(s).date()
        except (ValueError, TypeError, OverflowError):
            return None

    to_delete = []
    for r in rows:
        if r["kind"] == "daily":
            continue  # recurring -- never expires
        # anchor: completion date for done items, else the due date
        anchor = as_date(r["last_fired"]) or as_date(r["due_date"]) if r["done"] \
            else as_date(r["due_date"])
        if anchor and anchor <= cutoff:
            to_delete.append(r["id"])

    for rid in to_delete:
        conn.execute("DELETE FROM reminders WHERE id = ?", (rid,))
    conn.commit()
    conn.close()
    return len(to_delete)


if __name__ == "__main__":
    init_db()
    print("reminders.db ready")
    print("active:", list_active())
