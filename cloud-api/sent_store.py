"""Registry of notifier messages we've sent, so a task completed later can be
struck through in the message the user already has on their phone.

Telegram only lets a bot edit its own messages for 48 hours, so anything older
than that is dead weight and gets pruned. We store the message text verbatim
rather than regenerating it: regenerating risks drift (a job deadline could have
appeared since), and a plain string replace on the original is deterministic.
"""
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = os.environ.get("SENT_DB", "/data/sent.db")
EDIT_WINDOW_HOURS = 48


def _conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS sent (
            render_id  TEXT PRIMARY KEY,
            kind       TEXT,
            chat_id    TEXT,
            message_id INTEGER,
            text       TEXT,
            items      TEXT,
            created_at TEXT
        )""")


def new_render(kind: str, text: str, items: list[dict]) -> str:
    """Called when we build a message. `items` is [{"id": reminder_id,
    "line": "- exact line as it appears"}] -- only reminder-backed lines, since
    school work and job deadlines aren't completable."""
    init_db()
    rid = uuid.uuid4().hex[:16]
    with _conn() as c:
        c.execute("INSERT INTO sent (render_id, kind, chat_id, message_id, text, items, created_at)"
                  " VALUES (?,?,?,?,?,?,?)",
                  (rid, kind, None, None, text, json.dumps(items),
                   datetime.now().isoformat(timespec="seconds")))
    return rid


def attach(render_id: str, chat_id, message_id) -> bool:
    """Link the render to the Telegram message n8n actually sent."""
    init_db()
    with _conn() as c:
        cur = c.execute("UPDATE sent SET chat_id = ?, message_id = ? WHERE render_id = ?",
                        (str(chat_id), int(message_id), render_id))
        return cur.rowcount > 0


def find_with_reminder(reminder_id: int) -> list[dict]:
    """Sent messages still inside Telegram's 48h edit window that contain this
    reminder. Rows never linked to a message_id are skipped -- nothing to edit."""
    init_db()
    cutoff = (datetime.now() - timedelta(hours=EDIT_WINDOW_HOURS)).isoformat(timespec="seconds")
    out = []
    with _conn() as c:
        for r in c.execute("SELECT * FROM sent WHERE message_id IS NOT NULL AND created_at >= ?",
                           (cutoff,)):
            try:
                if any(int(i["id"]) == int(reminder_id) for i in json.loads(r["items"])):
                    out.append(dict(r))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    return out


def update_text(render_id: str, text: str):
    init_db()
    with _conn() as c:
        c.execute("UPDATE sent SET text = ? WHERE render_id = ?", (text, render_id))


def prune():
    """Drop rows past the edit window -- they can never be edited again."""
    init_db()
    cutoff = (datetime.now() - timedelta(hours=EDIT_WINDOW_HOURS)).isoformat(timespec="seconds")
    with _conn() as c:
        c.execute("DELETE FROM sent WHERE created_at < ?", (cutoff,))
