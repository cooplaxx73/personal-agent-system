"""Local API exposing each worker as an HTTP endpoint on the Windows host,
so n8n (running inside Docker) can trigger them via host.docker.internal
instead of trying to run scripts directly inside its own container."""

import os
import requests
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from dateutil import parser as dateparser
from fastapi import FastAPI, Query

WORKERS_DIR = Path(__file__).parent / "workers"
sys.path.insert(0, str(WORKERS_DIR))

import jobs_worker
import indeed_worker
import job_store
import reminders_worker

app = FastAPI()


def _split_terms(raw):
    """Models send 'a,b', 'a, b' or 'a b c' interchangeably -- accept all."""
    import re as _re
    if not raw:
        return None
    parts = [p for p in _re.split(r"[,\s]+", raw) if p.strip()]
    return parts or None


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


def _blocked_reply(source: str) -> dict:
    """Shape returned when a site's bot-protection refuses this server. Explicit
    so the agent can explain it, rather than reporting 'no jobs found' (a lie)
    or leaking a raw 500."""
    return {"new_matches": [], "blocked": True,
            "note": (f"{source} is blocking this server's IP address, so that search "
                     f"can't run from the cloud. Other job sources are unaffected.")}


@app.get("/run/indeed")
def run_indeed(
    search_query: Optional[str] = None,
    search_locations: Optional[str] = Query(
        None, description="Semicolon-separated, e.g. 'Toronto, ON;Kingston, ON'"),
    role_keywords: Optional[str] = Query(None, description="Comma-separated"),
    require_intern: bool = True,
):
    locs = search_locations.split(";") if search_locations else None
    kw = role_keywords.split(",") if role_keywords else None
    from indeed_worker import BlockedError
    try:
        return {"new_matches": indeed_worker.run(
        search_query=search_query, search_locations=locs,
        role_keywords=kw, require_intern=require_intern)}
    except BlockedError:
        return _blocked_reply("Indeed")


@app.get("/run/queens")
def run_queens():
    if not (WORKERS_DIR / "queens_session.json").exists():
        return {"error": "Queen's session not set up yet -- run queens_login.py first"}
    import queens_worker
    return {"new_matches": queens_worker.run()}


@app.get("/run/onq")
def run_onq():
    if not (WORKERS_DIR / "onq_session.json").exists():
        return {"error": "onQ session not set up yet -- run onq_login.py first"}
    import onq_ics
    return {"new_items": onq_ics.refresh(force=True)}


@app.get("/run/onq_reminders")
def run_onq_reminders(early_days: int = 2):
    if not (WORKERS_DIR / "onq_deadlines.db").exists():
        return {"reminders": []}
    import onq_ics
    return {"reminders": onq_ics.get_reminder_touchpoints(early_days=early_days)}


@app.get("/health/check")
def health_check():
    """Check whether onQ session or any Gmail account's token have expired
    and need re-authentication. Meant to be polled on a schedule and pushed
    as a Telegram alert when something needs attention."""
    issues = []

    import onq_ics
    ok, problem = onq_ics.check_feed_ok()
    if not ok:
        issues.append(problem)

    if (WORKERS_DIR / "queens_session.json").exists():
        import queens_worker
        ok, problem = queens_worker.check_access()
        if not ok:
            issues.append(problem)

    if (WORKERS_DIR / "credentials.json").exists():
        import gmail_worker
        for account in gmail_worker.check_all_accounts_valid():
            issues.append(f"Gmail account '{account}' authentication has expired -- "
                           f"run gmail_login.py {account} to re-consent")

    return {"issues": issues}


VAULT_SECTIONS = ["Deadlines", "Jobs", "Emails"]


@app.get("/vault/search")
def vault_search(q: str = Query("", description="Keyword to look for anywhere in the vault"),
                 folder: str = Query("", description="Optional folder to limit the search to"),
                 limit: int = 20,
                 section: Optional[str] = Query(None, description="Deprecated shortcut")):
    """Keyword-search the WHOLE vault. `section` is kept only so older callers
    don't break -- it just scopes the search to that folder."""
    import vault
    try:
        if section and not q and not folder:
            return {"query": "", "folder": section,
                    "results": vault.list_notes(f"{vault.AGENT_FOLDER}/{section}")["notes"]}
        if section and not folder:
            folder = f"{vault.AGENT_FOLDER}/{section}"
        return {"query": q, "folder": folder or "/", "results": vault.search(q, limit, folder)}
    except (RuntimeError, ValueError) as e:
        return {"error": str(e), "results": []}


@app.get("/vault/read")
def vault_read(path: str = Query(..., description="Vault-relative path, e.g. 'Personal Agent System/_Home.md'")):
    """Read one note in full."""
    import vault
    try:
        return vault.read_note(path)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e), "content": ""}


@app.get("/vault/write")
def vault_write(path: str = Query(..., description="Vault-relative .md path"),
                content: str = Query(..., description="Markdown to save"),
                mode: str = Query("append", description="append | create | overwrite"),
                title: str = Query("", description="Optional heading for the entry")):
    """Create, append to, or overwrite a note. Appending is the default because
    it is the only non-destructive option."""
    import vault
    try:
        return vault.write_note(path, content, mode, title)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e)}


@app.get("/vault/list")
def vault_list(folder: str = Query("", description="Folder to list; blank = whole vault"),
               limit: int = 60):
    """List notes, most recently edited first."""
    import vault
    try:
        return vault.list_notes(folder, limit)
    except (RuntimeError, ValueError) as e:
        return {"error": str(e), "notes": []}


PC_QUEENS = os.environ.get("PC_QUEENS_URL", "")


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


@app.get("/reminders/add")
def reminders_add(
    text: str = Query(..., description="What to be reminded of"),
    kind: str = Query("once", description="once | daily | deadline"),
    due_date: Optional[str] = Query(None, description="YYYY-MM-DD (once, deadline)"),
    fire_time: Optional[str] = Query(None, description="HH:MM 24h (once, daily)"),
    lead_days: int = Query(0, description="deadline: days before due to start showing in the morning digest"),
):
    """Save a reminder/to-do. For a deadline, set lead_days to how much runway
    the task needs (a big assignment gets a larger window than a quick errand).
    Rejects one-time reminders/deadlines whose moment has already passed so the
    bot can tell the user instead of saving something that will never fire."""
    now = datetime.now()
    if kind in ("once", "deadline") and due_date:
        try:
            d = dateparser.parse(due_date).date()
        except (ValueError, TypeError, OverflowError):
            d = None
        if d:
            if kind == "once" and fire_time:
                try:
                    hh, mm = (int(x) for x in fire_time.split(":")[:2])
                    when = datetime(d.year, d.month, d.day, hh, mm)
                    # compare at minute granularity so "9:38" is still valid at 9:38:40
                    if when < now.replace(second=0, microsecond=0):
                        return {"error": f"That time ({due_date} {fire_time}) has already "
                                f"passed -- it's {now.strftime('%Y-%m-%d %H:%M')} now. Pick a "
                                f"future time, or I can set it for tomorrow."}
                except (ValueError, TypeError):
                    pass
            elif d < now.date():
                label = "due date" if kind == "deadline" else "date"
                return {"error": f"That {label} ({due_date}) is already in the past -- "
                        f"it's {now.strftime('%Y-%m-%d')} today."}
    return {"added": reminders_worker.add(text, kind, due_date, fire_time, lead_days)}


@app.get("/reminders/list")
def reminders_list():
    return {"reminders": reminders_worker.list_active()}


@app.get("/reminders/complete")
def reminders_complete(id: int):
    return {"completed": reminders_worker.complete(id)}


@app.get("/reminders/delete")
def reminders_delete(id: int):
    return {"deleted": reminders_worker.delete(id)}


@app.get("/reminders/purge")
def reminders_purge():
    """Hard-delete reminders that are 3+ days past: completed ones, and
    once/deadline items whose date has passed. Never removes 'daily'. Runs
    automatically once a day via the morning digest; also callable directly."""
    return {"purged": reminders_worker.purge_expired()}


@app.get("/reminders/due")
def reminders_due():
    """Point reminders firing right now -- polled every minute by the notifier
    flow. Returns [] most of the time; sends nothing when empty."""
    return {"due": reminders_worker.get_due_now()}


@app.get("/reminders/digest")
def reminders_digest():
    """The consolidated morning message: my reminders + job-application
    deadlines + onQ deadlines, rolled into ONE text so the notifier bot sends
    a single push. Returns {'text': '', 'count': 0} when there's nothing."""
    sections = []
    count = 0

    # daily housekeeping: clear out reminders that are well past (3-day grace)
    reminders_worker.purge_expired()

    mine = reminders_worker.get_digest_items()
    if mine:
        count += len(mine)
        sections.append("Reminders & to-dos:\n" + "\n".join(f"- {x}" for x in mine))

    if (WORKERS_DIR / "jobs.db").exists():
        jobs = job_store.get_upcoming_job_deadlines(days_ahead=3)
        if jobs:
            count += len(jobs)
            lines = [f"- {'TODAY' if j['days_left'] == 0 else str(j['days_left']) + 'd'} "
                     f"- {j['company']}: {j['title']} -- {j['link']}" for j in jobs]
            sections.append("Job application deadlines:\n" + "\n".join(lines))

    if (WORKERS_DIR / "onq_session.json").exists():
        import onq_worker
        onq = onq_worker.get_reminder_touchpoints(early_days=2)
        if onq:
            count += len(onq)
            lines = [f"- {'TODAY' if r['days_left'] == 0 else str(r['days_left']) + 'd'} "
                     f"- {r['course']}: {r['title']} ({r['type']})" for r in onq]
            sections.append("onQ deadlines:\n" + "\n".join(lines))

    text = ("Good morning, master. Here's your day:\n\n" + "\n\n".join(sections)) if count else ""
    return {"text": text, "count": count}


@app.get("/run/gmail")
def run_gmail():
    if not (WORKERS_DIR / "credentials.json").exists():
        return {"error": "Gmail credentials.json not set up yet"}
    import gmail_worker
    return {"emails": gmail_worker.fetch_all_accounts()}


@app.get("/gmail/summarize")
def gmail_summarize(days: int = 1, keywords_only: bool = False):
    """AI-summarize recent important email across all accounts. `days` = how far
    back (1 = today/last 24h, 7 = the week). By default this covers ALL real mail
    minus promotions/social/spam/trash; the summarizer ignores anything that's
    just marketing. Set keywords_only=true to narrow to deadline/interview/exam-
    type mail. Runs on the free local/gateway model, so it barely uses credit."""
    if not (WORKERS_DIR / "credentials.json").exists():
        return {"error": "Gmail not set up yet", "summary": ""}
    import email_summarizer
    q = f"newer_than:{days}d -category:promotions -category:social -in:spam -in:trash"
    if keywords_only:
        q += " (deadline OR interview OR application OR assignment OR exam OR offer OR meeting)"
    return email_summarizer.summarize_recent(query=q)


@app.get("/organize/gmail")
def organize_gmail():
    """Fetch recent emails across all logged-in accounts, categorize them
    (read-only), and write a note per category into the Obsidian vault --
    requires OBSIDIAN_VAULT_PATH."""
    if not (WORKERS_DIR / "credentials.json").exists():
        return {"error": "Gmail credentials.json not set up yet"}
    import gmail_worker
    import gmail_organizer
    import obsidian_writer

    if not obsidian_writer.VAULT_PATH:
        return {"error": "OBSIDIAN_VAULT_PATH not set yet -- provide your vault's folder path"}

    emails = gmail_worker.fetch_all_accounts()
    categorized = gmail_organizer.categorize_emails(emails)

    by_category: dict[str, list[dict]] = {}
    for e in categorized:
        by_category.setdefault(e["category"], []).append(e)

    written_paths = []
    for category, items in by_category.items():
        lines = []
        for e in items:
            action = f" -- **Action:** {e['action_needed']}" if e.get("action_needed") else ""
            lines.append(f"- **[{e.get('account', '?')}] {e['subject']}** (from {e['from']}){action}")
        content = "\n".join(lines)
        path = obsidian_writer.append_note("Emails", category.capitalize(), content)
        written_paths.append(path)

    return {"categorized": categorized, "notes_written": written_paths}


@app.get("/email/accounts")
def email_accounts():
    """Which accounts are allowed to send email."""
    import gmail_worker
    return {"send_enabled": gmail_worker.SEND_ACCOUNTS}


@app.get("/email/draft")
def email_draft(
    account: str = Query(..., description="Send-enabled account to send FROM (primary or tech)"),
    to: str = Query(..., description="Recipient(s), comma-separated"),
    subject: str = "",
    body: str = "",
    attachments: Optional[str] = Query(None, description="Comma-separated file paths to attach"),
    mode: str = Query("single", description="single | bcc | individual"),
):
    """STEP 1 of sending: stage an email for review. Returns a token + a preview
    to SHOW THE USER. Nothing is sent yet -- only call /email/confirm with this
    token AFTER the user has reviewed the draft and explicitly said yes."""
    import gmail_worker
    import email_drafts
    if account not in gmail_worker.SEND_ACCOUNTS:
        return {"error": f"'{account}' can't send. Send-enabled: {gmail_worker.SEND_ACCOUNTS}"}
    recips = [x.strip() for x in to.split(",") if x.strip()]
    if not recips:
        return {"error": "no valid recipients"}
    atts = [x.strip() for x in attachments.split(",") if x.strip()] if attachments else []
    if mode not in ("single", "bcc", "individual"):
        mode = "single"
    token = email_drafts.create(account, recips, subject, body, atts, mode)
    return {"token": token, "preview": {
        "from_account": account, "to": recips, "subject": subject,
        "body": body, "attachments": atts, "mode": mode}}


@app.get("/email/confirm")
def email_confirm(token: str = Query(..., description="Token from /email/draft, after user confirmed")):
    """STEP 2: actually send the drafted email. Only call after the user has
    reviewed the draft from /email/draft and explicitly confirmed."""
    import gmail_sender
    import email_drafts
    d = email_drafts.get(token)
    if not d:
        return {"error": "no such draft (already sent, cancelled, or expired)"}
    if d["mode"] in ("bcc", "individual"):
        res = gmail_sender.send_bulk(d["account"], d["recipients"], d["subject"],
                                     d["body"], mode=d["mode"], attachments=d["attachments"])
    else:
        res = gmail_sender.send(d["account"], d["recipients"], d["subject"],
                                d["body"], attachments=d["attachments"])
    if res.get("sent"):
        email_drafts.delete(token)
    return res


@app.get("/email/cancel")
def email_cancel(token: str = Query(..., description="Discard a drafted email without sending")):
    import email_drafts
    return {"cancelled": email_drafts.delete(token)}


@app.get("/run/all")
def run_all():
    return {
        "jobs": run_jobs(),
        "indeed": run_indeed(),
        "queens": run_queens(),
        "onq": run_onq(),
        "gmail": run_gmail(),
    }
