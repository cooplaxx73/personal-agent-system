"""Pending outbound-email drafts -- the safety layer for sending.

A send is always two steps: `create` stores the exact email and returns a short
token; the user reviews the draft; `confirm` (in the API) looks it up by token
and sends it, then deletes it. Nothing sends without that reviewed token, so the
bot can never fire an email off on a single message.
"""
import json
import secrets
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "email_drafts.db"


def _init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            token TEXT PRIMARY KEY,
            account TEXT NOT NULL,
            recipients TEXT NOT NULL,   -- JSON list
            subject TEXT,
            body TEXT,
            attachments TEXT,           -- JSON list of file paths
            mode TEXT NOT NULL,         -- single | bcc | individual
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def create(account, recipients, subject, body, attachments=None, mode="single") -> str:
    _init()
    token = secrets.token_hex(3)  # short, e.g. "9f2a1c"
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO drafts (token, account, recipients, subject, body, attachments, mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token, account, json.dumps(recipients), subject or "", body or "",
         json.dumps(attachments or []), mode))
    conn.commit()
    conn.close()
    return token


def get(token: str):
    _init()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM drafts WHERE token = ?", (token,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["recipients"] = json.loads(d["recipients"])
    d["attachments"] = json.loads(d["attachments"])
    return d


def delete(token: str) -> bool:
    _init()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM drafts WHERE token = ?", (token,))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok
