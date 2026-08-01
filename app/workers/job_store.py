"""Shared SQLite store for tracking which job postings we've already seen."""

import sqlite3
from datetime import datetime
from pathlib import Path

from dateutil import parser as dateparser

import matching

DB_PATH = Path(__file__).parent / "jobs.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_key TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            company TEXT,
            location TEXT,
            link TEXT,
            deadline TEXT,
            posted TEXT,
            first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # migrate older DBs that predate these columns
    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")]
    if "deadline" not in existing_cols:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN deadline TEXT")
    if "posted" not in existing_cols:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN posted TEXT")
    if "hidden" not in existing_cols:
        # user-dismissed listings: excluded from queries/summaries but kept
        # in the table so dedup still recognizes them (never hard-delete)
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN hidden INTEGER DEFAULT 0")
    conn.commit()
    conn.close()


def is_new(job_key: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM seen_jobs WHERE job_key = ?", (job_key,)).fetchone()
    conn.close()
    return row is None


def mark_seen(job_key: str, source: str, title: str, company: str, location: str, link: str,
              deadline: str | None = None, posted: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs "
        "(job_key, source, title, company, location, link, deadline, posted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_key, source, title, company, location, link, deadline, posted),
    )
    conn.commit()
    conn.close()


def matching_jobs(
    role_keywords: list[str] | None = None,
    require_intern: bool = True,
    locations: list[str] | None = None,
    timeframe: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Every stored (non-hidden) posting that fits the same criteria a live scan
    uses. Deliberately runs matches_criteria rather than SQL LIKE so 'what the
    scan looks for' and 'what we report as matching' can never drift apart."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT title, company, location, link, deadline, posted, first_seen_at "
        "FROM seen_jobs WHERE hidden = 0").fetchall()
    conn.close()
    out = []
    for title, comp, loc, link, deadline, posted, first_seen in rows:
        if not matching.matches_criteria(title, loc, role_keywords, require_intern,
                                         locations, timeframe):
            continue
        out.append({"title": title, "company": comp, "location": loc, "link": link,
                    "deadline": deadline, "posted": posted, "first_seen": first_seen})
    out.sort(key=lambda j: j["posted"] or j["first_seen"], reverse=True)
    return out[:limit]


def query_jobs(
    keywords: list[str] | None = None,
    location: str | list[str] | None = None,
    company: str | None = None,
    posted_within_days: int | None = None,
    timeframe: str | None = None,
    sort: str = "posted",  # "posted" (newest first) or "deadline" (soonest first)
    limit: int = 50,
) -> list[dict]:
    """Query previously-found (non-hidden) postings so the bot can summarize
    'jobs I should apply to' without re-scraping. Keywords OR-match against
    the title; location/company are substring matches."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    where, params = ["hidden = 0"], []
    if keywords:
        ors = " OR ".join("LOWER(title) LIKE ?" for _ in keywords)
        where.append(f"({ors})")
        params += [f"%{k.strip().lower()}%" for k in keywords]
    if location:
        locs = [location] if isinstance(location, str) else location
        locs = [l.strip().lower() for l in locs if l.strip()]
        if locs:
            where.append("(" + " OR ".join("LOWER(location) LIKE ?" for _ in locs) + ")")
            params += [f"%{l}%" for l in locs]
    if company:
        where.append("(LOWER(company) LIKE ? OR LOWER(source) LIKE ?)")
        params += [f"%{company.strip().lower()}%"] * 2
    rows = conn.execute(
        "SELECT title, company, location, link, deadline, posted, first_seen_at "
        f"FROM seen_jobs WHERE {' AND '.join(where)}", params).fetchall()
    conn.close()

    now = datetime.now()
    out = []
    for title, comp, loc, link, deadline, posted, first_seen in rows:
        posted_date = None
        try:
            posted_date = dateparser.parse(posted).date() if posted else None
        except (ValueError, TypeError, OverflowError):
            pass  # relative strings like "Posted 3 Days Ago" aren't sortable
        if posted_within_days is not None:
            # postings with no posted date (common on co-op boards) fall back to
            # when WE first saw them, instead of silently vanishing from results
            recency = posted_date
            if recency is None:
                try:
                    recency = dateparser.parse(first_seen).date()
                except (ValueError, TypeError, OverflowError):
                    recency = None
            if not recency or (now.date() - recency).days > posted_within_days:
                continue
        if not matching.matches_timeframe(title, timeframe):
            continue
        out.append({"title": title, "company": comp, "location": loc, "link": link,
                    "deadline": deadline, "posted": posted, "first_seen": first_seen,
                    "_posted_date": posted_date})

    if sort == "deadline":
        # soonest deadline first; postings without one go last
        def deadline_key(j):
            try:
                return (0, dateparser.parse(j["deadline"]).date()) if j["deadline"] else (1, now.date())
            except (ValueError, TypeError, OverflowError):
                return (1, now.date())
        out.sort(key=deadline_key)
    else:
        # newest posted first; unknown-posted go last (fall back to first_seen)
        out.sort(key=lambda j: j["_posted_date"] or dateparser.parse(j["first_seen"]).date(),
                 reverse=True)
    for j in out:
        j.pop("_posted_date", None)
    return out[:limit]


def dismiss_jobs(contains: str, field: str = "any") -> int:
    """Hide listings whose title/company/location contains the given text
    (one-time cleanup of existing rows -- future finds still appear).
    Returns how many were newly hidden."""
    init_db()
    like = f"%{contains.strip().lower()}%"
    fields = {"title": "LOWER(title) LIKE ?",
              "company": "(LOWER(company) LIKE ? OR LOWER(source) LIKE ?)",
              "location": "LOWER(location) LIKE ?"}
    if field == "any":
        clause = "(" + " OR ".join(fields.values()) + ")"
        params = [like] * 4  # company clause takes two
    else:
        clause = fields.get(field, fields["title"])
        params = [like] * clause.count("?")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(f"UPDATE seen_jobs SET hidden = 1 WHERE hidden = 0 AND {clause}", params)
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def unhide_jobs(contains: str | None = None) -> int:
    """Reverse dismiss_jobs -- with no argument, unhides everything."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    if contains:
        like = f"%{contains.strip().lower()}%"
        cur = conn.execute(
            "UPDATE seen_jobs SET hidden = 0 WHERE hidden = 1 AND "
            "(LOWER(title) LIKE ? OR LOWER(company) LIKE ? OR LOWER(source) LIKE ? OR LOWER(location) LIKE ?)",
            [like] * 4)
    else:
        cur = conn.execute("UPDATE seen_jobs SET hidden = 0 WHERE hidden = 1")
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def get_upcoming_job_deadlines(days_ahead: int = 3) -> list[dict]:
    """Job postings (from any source) with an application deadline landing
    within the next `days_ahead` days -- most postings won't have one set
    at all, this only fires for the ones that do."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT company, title, link, deadline FROM seen_jobs "
        "WHERE deadline IS NOT NULL AND deadline != '' AND hidden = 0"
    ).fetchall()
    conn.close()

    now = datetime.now()
    upcoming = []
    for company, title, link, deadline_str in rows:
        try:
            due = dateparser.parse(deadline_str)
        except (ValueError, TypeError, OverflowError):
            continue
        days_left = (due.date() - now.date()).days
        if 0 <= days_left <= days_ahead:
            upcoming.append({"company": company, "title": title, "link": link,
                              "deadline": due.isoformat(), "days_left": days_left})

    return sorted(upcoming, key=lambda x: x["days_left"])
