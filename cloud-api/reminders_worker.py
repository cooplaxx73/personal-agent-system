"""Reminders + to-dos store, and the logic that decides what to notify when.

Four kinds of reminder:
- 'once'     : fire one time at a specific date + time (e.g. "call prof 3pm Fri")
- 'daily'    : recurring every day at a time (e.g. "review new postings 9am")
- 'weekly'   : recurring on set weekdays via `days_of_week` ("mon" / "mon,thu" /
               weekdays / weekends). Same firing path as daily, gated on today
               being in the mask.
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
DIGEST_TIME = "07:30"  # must match the n8n Morning Trigger cron (30 7 * * *);
                       # it decides which items ride the digest vs ping separately
PURGE_GRACE_DAYS = 3   # keep past/completed reminders this long, then hard-delete

# index matches datetime.weekday() -- Monday is 0
DAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_ALIASES = {"weekday": "mon,tue,wed,thu,fri", "weekdays": "mon,tue,wed,thu,fri",
                "weekend": "sat,sun", "weekends": "sat,sun",
                "everyday": ",".join(DAY_CODES), "daily": ",".join(DAY_CODES)}
RECURRING_KINDS = ("daily", "weekly")


def normalize_days(days) -> str:
    """'Monday, Wed' / 'weekdays' / ['mon'] -> 'mon,wed'. Unknown tokens are
    dropped rather than guessed at; an empty result lets the caller default."""
    if not days:
        return ""
    parts = days if isinstance(days, (list, tuple)) else str(days).replace("/", ",").split(",")
    out = []
    for raw in parts:
        tok = str(raw).strip().lower()
        if not tok:
            continue
        if tok in _DAY_ALIASES:
            out += _DAY_ALIASES[tok].split(",")
            continue
        code = tok[:3]
        if code in DAY_CODES and code not in out:
            out.append(code)
    seen, ordered = set(), []
    for c in DAY_CODES:            # keep Mon..Sun order regardless of input order
        if c in out and c not in seen:
            seen.add(c)
            ordered.append(c)
    return ",".join(ordered)


def describe_days(mask: str) -> str:
    """Mask -> the phrase the bot says back ('Mondays', 'weekdays')."""
    codes = [c for c in (mask or "").split(",") if c]
    if not codes:
        return "weekly"
    if len(codes) == 7:
        return "every day"
    if codes == ["mon", "tue", "wed", "thu", "fri"]:
        return "weekdays"
    if codes == ["sat", "sun"]:
        return "weekends"
    names = {"mon": "Mondays", "tue": "Tuesdays", "wed": "Wednesdays", "thu": "Thursdays",
             "fri": "Fridays", "sat": "Saturdays", "sun": "Sundays"}
    full = [names[c] for c in codes]
    return full[0] if len(full) == 1 else ", ".join(full[:-1]) + " and " + full[-1]


def fires_today(row, now: datetime | None = None) -> bool:
    """Does this recurring row apply to today? Only 'weekly' is ever gated."""
    if row["kind"] != "weekly":
        return True
    mask = row["days_of_week"] if "days_of_week" in row.keys() else ""
    codes = [c for c in (mask or "").split(",") if c]
    if not codes:
        return False          # a weekly with no days set must not fire every day
    return DAY_CODES[(now or datetime.now()).weekday()] in codes


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
            days_of_week TEXT,                  -- weekly: 'mon,thu' (see DAY_CODES)
            active INTEGER DEFAULT 1,
            done INTEGER DEFAULT 0,
            last_fired TEXT,                    -- YYYY-MM-DD, blocks same-day re-fire
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # migrate DBs created before weekly reminders existed
    if "days_of_week" not in [r[1] for r in conn.execute("PRAGMA table_info(reminders)")]:
        conn.execute("ALTER TABLE reminders ADD COLUMN days_of_week TEXT")
    conn.commit()
    conn.close()


def add(text: str, kind: str = "once", due_date: str | None = None,
        fire_time: str | None = None, lead_days: int = 0,
        days_of_week: str | None = None) -> dict:
    """Store a reminder. `due_date` YYYY-MM-DD, `fire_time` HH:MM (24h),
    `days_of_week` 'mon,thu' for kind='weekly'."""
    kind = kind if kind in ("once", "daily", "deadline", "weekly") else "once"
    mask = normalize_days(days_of_week)
    if kind == "weekly" and not mask:
        # no parseable day -> anchor to the day they asked, never all seven
        mask = DAY_CODES[datetime.now().weekday()]
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO reminders (text, kind, due_date, fire_time, lead_days, days_of_week) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (text, kind, due_date, fire_time, lead_days or 0, mask))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return {"id": rid, "text": text, "kind": kind, "due_date": due_date,
            "fire_time": fire_time, "lead_days": lead_days or 0, "days_of_week": mask}


def list_active() -> list[dict]:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, text, kind, due_date, fire_time, lead_days, days_of_week, done "
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


def _pretty_time(hhmm: str) -> str:
    """24h -> the 9am/9:30pm style the bot uses everywhere else."""
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except (ValueError, AttributeError):
        return hhmm
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{'' if m == 0 else ':%02d' % m}{suffix}"


def get_today_timed_rows(now: datetime | None = None) -> list[dict]:
    """Reminders that will ping later today -- the digest's preview section.

    These still fire on their own at fire_time; listing them at 7:30am is a
    heads-up, not a replacement, which is why they are kept separate from the
    untimed To Do items. Anything whose time has already passed is skipped so
    "Later Today" stays truthful."""
    now = now or datetime.now()
    today, hhmm = _today(now), now.strftime("%H:%M")
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM reminders WHERE active = 1 AND done = 0 "
        "AND fire_time IS NOT NULL AND fire_time != '' AND fire_time != ?",
        (DIGEST_TIME,)).fetchall()
    conn.close()

    out = []
    for r in rows:
        if r["kind"] == "once":
            if not r["due_date"] or r["due_date"] != today:
                continue
        elif r["kind"] in RECURRING_KINDS:
            if not fires_today(r, now):
                continue                  # weekly, but not one of its days
        else:
            continue                      # deadlines have no time; they sit in To Do
        if r["fire_time"] <= hhmm:
            continue                      # already fired earlier today
        out.append({"id": r["id"], "fire_time": r["fire_time"],
                    "line": f"{_pretty_time(r['fire_time'])} - {r['text']}"})
    return sorted(out, key=lambda x: x["fire_time"])


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
        if not fires_today(r, now):
            continue                      # weekly item, not scheduled for today
        due.append({"id": r["id"], "text": r["text"], "kind": r["kind"]})
        conn.execute("UPDATE reminders SET last_fired = ? WHERE id = ?", (today, r["id"]))
        if r["kind"] == "once":
            conn.execute("UPDATE reminders SET done = 1 WHERE id = ?", (r["id"],))
    conn.commit()
    conn.close()
    return due


def is_done(reminder_id: int) -> bool:
    """Has this reminder been completed? Used when re-rendering an already-sent
    notification so finished items show struck through."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT done FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    conn.close()
    return bool(row and row[0])


def get_digest_items(now: datetime | None = None) -> list[str]:
    """Digest lines only. Prefer get_digest_rows() when you need the reminder id
    (e.g. to strike an item through later)."""
    return [r["line"] for r in get_digest_rows(now)]


def get_digest_rows(now: datetime | None = None) -> list[dict]:
    """My reminders that belong in the morning digest: daily to-dos, deadlines
    inside their lead window, and once-items due today with no specific time.
    Returns {"id", "line"} so a sent message can be traced back to a reminder."""
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
            lines.append({"id": r["id"], "line": f"[daily] {r['text']}"})
        elif kind == "weekly" and rides_digest(ft) and fires_today(r, now):
            lines.append({"id": r["id"], "line": f"[weekly] {r['text']}"})
        elif kind == "once" and rides_digest(ft) and r["due_date"]:
            try:
                if dateparser.parse(r["due_date"]).date() == today:
                    lines.append({"id": r["id"], "line": f"[today] {r['text']}"})
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
                lines.append({"id": r["id"],
                              "line": f"[{when}] {r['text']} (due {r['due_date']})"})
    return lines


def purge_expired(now: datetime | None = None) -> int:
    """Hard-delete reminders that are well past, after a PURGE_GRACE_DAYS buffer:
    - completed items (timed from their completion date)
    - 'once'/'deadline' whose date has passed (timed from the due date)
    'daily'/'weekly' reminders are never purged -- they recur. Returns how
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
        if r["kind"] in RECURRING_KINDS:
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
