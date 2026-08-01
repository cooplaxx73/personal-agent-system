"""Queue of PC-dependent requests deferred while the PC is off. Lives on the VM
so it survives regardless of the PC. Each row is a captured PC API call (path +
query) to replay when the PC is back. DB path via QUEUE_DB env.
"""
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("QUEUE_DB") or (Path(__file__).parent / "queue.db"))


def _init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            query TEXT,
            label TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def add(path: str, query: str = "", label: str | None = None) -> int:
    _init()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("INSERT INTO queue (path, query, label) VALUES (?, ?, ?)",
                       (path, query, label or path))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def all_items() -> list[dict]:
    _init()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM queue ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove(qid: int) -> bool:
    _init()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM queue WHERE id = ?", (qid,))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok
