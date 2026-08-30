"""Workers API: exposes each worker as an HTTP endpoint on the VM, so n8n
(running inside Docker) can trigger them over localhost instead of trying to
run scripts directly inside its own container.

Job search only. Reminders and the daily digest live in the cloud API on :8001."""

import os
import requests
import sys
from pathlib import Path
from typing import Optional

from dateutil import parser as dateparser
from fastapi import FastAPI, Query

WORKERS_DIR = Path(__file__).parent / "workers"
sys.path.insert(0, str(WORKERS_DIR))

import jobs_worker
import job_store

app = FastAPI()


def _split_terms(raw):
    """Models send 'a,b', 'a, b' or 'a b c' interchangeably -- accept all."""
    import re as _re
    if not raw:
        return None
    parts = [p for p in _re.split(r"[,\s]+", raw) if p.strip()]
    return parts or None


@app.get("/health")
def health():
    """Liveness only -- the watchdog needs an endpoint that proves this service
    is answering, not just that the process exists. Deliberately touches no
    database and no network so a slow scrape can't make the service look down."""
    return {"ok": True}


@app.get("/run/jobs")
def run_jobs(
    role_keywords: Optional[str] = Query(
        None, description="Comma-separated keywords, e.g. 'backend,software,engineer'"),
    require_intern: bool = True,
    locations: Optional[str] = Query(
        None, description="Comma-separated location substrings, e.g. 'toronto,kingston'"),
    timeframe: Optional[str] = Query(
        None, description="Term the job is for, e.g. 'summer 2027'; drops postings naming a different season/year"),
):
    kw = _split_terms(role_keywords)
    locs = _split_terms(locations)
    new = jobs_worker.run(role_keywords=kw, require_intern=require_intern,
                          locations=locs, timeframe=timeframe)
    # everything on file fitting the same criteria -- a scan finding nothing NEW
    # is normal and must not read as "there are no jobs"
    allm = job_store.matching_jobs(role_keywords=kw, require_intern=require_intern,
                                   locations=locs, timeframe=timeframe)
    return {"new_matches": new, "new_count": len(new),
            "total_matching": len(allm), "matches": allm[:15],
            # echoed so the bot can state what it actually searched -- the model
            # cannot be relied on to ask first, so the user must at least SEE the
            # criteria and be able to correct them
            "criteria_used": {
                "roles": kw or ["software", "backend", "developer", "engineer"],
                "locations": locs or ["toronto", "kingston", "remote"],
                "interns_only": bool(require_intern),
                "timeframe": timeframe or "any"}}



@app.get("/run/queens")
def run_queens():
    if not (WORKERS_DIR / "queens_session.json").exists():
        return {"error": "Queen's session not set up yet -- run queens_login.py first"}
    import queens_worker
    return {"new_matches": queens_worker.run()}


@app.get("/jobs/queens")
def jobs_queens():
    """Search Queen's SWEP via the PC helper (the VM itself is Cloudflare-blocked)."""
    import socket
    import job_store
    from urllib.parse import urlparse
    u = urlparse(PC_QUEENS)
    try:
        with socket.create_connection((u.hostname, u.port or 8010), timeout=4):
            pass
    except OSError:
        return {"status": "pc_off",
                "message": "Your PC is off or unreachable, master -- turn it on, "
                           "then ask me to search Queen's again."}

    try:
        res = requests.get(f"{PC_QUEENS}/queens/search", timeout=180).json()
    except requests.RequestException:
        return {"status": "error", "message": "The Queen's search on your PC timed out, master."}

    if res.get("needs_login"):
        try:
            requests.get(f"{PC_QUEENS}/queens/login", timeout=15)
        except requests.RequestException:
            pass
        return {"status": "login_needed",
                "message": "I've opened the Queen's sign-in on your PC, master. Finish "
                           "signing in with your NetID + Duo, then ask me to search Queen's again."}
    if res.get("error"):
        return {"status": "error",
                "message": f"The Queen's search hit a snag on your PC ({res['error']}), master."}

    jobs = res.get("jobs", [])
    job_store.init_db()
    added = 0
    for j in jobs:
        key = "queens:" + (j.get("link") or j.get("title", ""))[:120]
        if job_store.is_new(key):
            dl = None
            if j.get("deadline"):
                try:
                    dl = dateparser.parse(j["deadline"]).strftime("%Y-%m-%d")
                except (ValueError, TypeError, OverflowError):
                    dl = None
            job_store.mark_seen(key, "queens", j.get("title", ""),
                                j.get("company", "Queen's SWEP"), j.get("location", ""),
                                j.get("link", ""), dl, None)
            added += 1
    return {"status": "ok", "found": len(jobs), "added": added,
            "message": f"Found {len(jobs)} Queen's SWEP postings ({added} new), master -- "
                       f"ask me to list jobs to see them."}


@app.get("/jobs/query")
def jobs_query(
    keywords: Optional[str] = Query(None, description="Comma-separated, OR-matched against titles"),
    location: Optional[str] = Query(None, description="Substring, e.g. 'toronto'"),
    company: Optional[str] = None,
    posted_within_days: Optional[int] = None,
    timeframe: Optional[str] = Query(None, description="e.g. 'summer 2027'; drops postings naming a different season/year"),
    sort: str = Query("posted", description="'posted' (newest first) or 'deadline' (soonest first)"),
    limit: int = 15,
    offset: str = Query("0", description="skip this many for pagination (page 2 = offset 15)"),
):
    """Query previously-found job postings (already stored, no live scrape),
    filtered and sorted so the bot can summarize what's worth applying to."""
    kw = _split_terms(keywords)
    # pull the whole matching set (sorted), then page it so we can report totals
    everything = job_store.query_jobs(
        keywords=kw, location=_split_terms(location), company=company,
        posted_within_days=posted_within_days, timeframe=timeframe, sort=sort, limit=10000)
    total = len(everything)
    try:
        off = max(0, int(str(offset).strip() or 0))
    except ValueError:
        off = 0  # models sometimes send offset= or garbage; treat as page 1
    page = everything[off:off + limit]
    end = off + len(page)
    return {"jobs": page, "total": total, "offset": off, "shown": len(page),
            "has_more": end < total,
            "next_offset": end if end < total else None,
            "range": (f"{off + 1}-{end} of {total}" if total else "0 of 0")}


@app.get("/jobs/dismiss")
def jobs_dismiss(
    contains: str = Query(..., description="Hide stored listings containing this text"),
    field: str = Query("any", description="Where to match: title, company, location, or any"),
):
    """Hide matching listings from all future queries/summaries. They stay in
    the DB so dedup still recognizes them; reversible via /jobs/unhide."""
    n = job_store.dismiss_jobs(contains, field)
    return {"hidden": n}


@app.get("/jobs/unhide")
def jobs_unhide(contains: Optional[str] = Query(None, description="Omit to unhide everything")):
    return {"unhidden": job_store.unhide_jobs(contains)}


@app.get("/run/job_deadline_reminders")
def run_job_deadline_reminders(days_ahead: int = 3):
    """Job postings with an application deadline landing soon -- most
    postings don't set one, this only fires for the ones that do."""
    if not (WORKERS_DIR / "jobs.db").exists():
        return {"reminders": []}
    return {"reminders": job_store.get_upcoming_job_deadlines(days_ahead=days_ahead)}


