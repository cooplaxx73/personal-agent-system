"""Always-on CLOUD API (Oracle VM): reminders + digest + health. The PC was
retired 2026-07-23, so job search runs in the workers API on this same VM
(:8002), reached over localhost. reminders.db location is set by REMINDERS_DB.

The digest is the one daily briefing: reminders (local) plus job application
deadlines fetched from the workers API. Every notifier message is rendered as
Telegram HTML with a bold dated title, escaped here rather than in n8n.
"""
import json
import os
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from dateutil import parser as dateparser
from fastapi import FastAPI, Query, Request

sys.path.insert(0, str(Path(__file__).parent))
import reminders_worker
import sent_store

app = FastAPI()

WORKERS_BASE = os.environ.get("WORKERS_API", "http://127.0.0.1:8002")


def _esc(s) -> str:
    """Escape for Telegram HTML parse_mode. Everything dynamic gets escaped:
    reminder text and job links routinely contain & and <, and a single
    unescaped one makes Telegram reject the whole message."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _title(label: str) -> str:
    """The bold dated heading every notifier push starts with. Container TZ is
    America/Toronto, so this is local time."""
    return f"<b>{_esc(label)} - {_esc(datetime.now().strftime('%a %d %b'))}</b>"


def _restrike(reminder_id: int) -> int:
    """Strike a just-completed task through in any notifier message the user
    already has. Telegram only lets a bot edit its own messages for 48h, so
    older ones are skipped (sent_store filters them out). Best-effort: failing
    to edit must never make /reminders/complete itself fail."""
    token = os.environ.get("TELEGRAM_NOTIFIER_TOKEN", "")
    if not token:
        return 0
    edited = 0
    for row in sent_store.find_with_reminder(reminder_id):
        text = row["text"]
        for it in json.loads(row["items"]):
            line = it["line"]
            if reminders_worker.is_done(int(it["id"])) and line in text \
                    and f"<s>{line}</s>" not in text:
                text = text.replace(line, f"<s>{line}</s>", 1)
        if text == row["text"]:
            continue
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/editMessageText",
                data={"chat_id": row["chat_id"], "message_id": row["message_id"],
                      "parse_mode": "HTML", "text": text}, timeout=15)
            if r.ok and r.json().get("ok"):
                sent_store.update_text(row["render_id"], text)
                edited += 1
        except Exception:  # noqa: BLE001 - a failed edit must not break completion
            pass
    return edited


@app.get("/notify/record")
def notify_record(render_id: str, chat_id: str, message_id: int):
    """Called by n8n immediately after the notifier sends, so we know which
    Telegram message carries which reminders and can edit it later."""
    sent_store.prune()
    return {"recorded": sent_store.attach(render_id, chat_id, message_id)}


def _workers_get(path: str, default):
    """Best-effort call to the :8002 workers API. The morning briefing must never
    fail just because one worker endpoint is down, so errors degrade to `default`
    and the rest of the digest still sends."""
    try:
        return requests.get(f"{WORKERS_BASE}{path}", timeout=20).json()
    except Exception:  # noqa: BLE001 - a dead worker must not kill the digest
        return default


@app.get("/reminders/add")
def reminders_add(
    text: str = Query(..., description="What to be reminded of"),
    kind: str = Query("once", description="once | daily | weekly | deadline"),
    due_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    fire_time: Optional[str] = Query(None, description="HH:MM 24h"),
    lead_days: int = Query(0),
    days_of_week: Optional[str] = Query(None, description="weekly: 'mon,thu' | weekdays | weekends"),
):
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
                    if when < now.replace(second=0, microsecond=0):
                        return {"error": f"That time ({due_date} {fire_time}) has already "
                                f"passed -- it's {now.strftime('%Y-%m-%d %H:%M')} now."}
                except (ValueError, TypeError):
                    pass
            elif d < now.date():
                label = "due date" if kind == "deadline" else "date"
                return {"error": f"That {label} ({due_date}) is already in the past -- "
                        f"it's {now.strftime('%Y-%m-%d')} today."}
    return {"added": reminders_worker.add(text, kind, due_date, fire_time, lead_days,
                                          days_of_week)}


@app.get("/reminders/list")
def reminders_list():
    return {"reminders": reminders_worker.list_active()}


@app.get("/reminders/complete")
def reminders_complete(id: int):
    """Mark done, then strike it through in any notification already sent."""
    ok = reminders_worker.complete(id)
    return {"completed": ok, "messages_updated": _restrike(id) if ok else 0}


@app.get("/reminders/delete")
def reminders_delete(id: int):
    return {"deleted": reminders_worker.delete(id)}


@app.get("/reminders/purge")
def reminders_purge():
    return {"purged": reminders_worker.purge_expired()}


@app.get("/reminders/due")
def reminders_due():
    """Point reminders firing right now -- polled every minute. `due` stays the
    raw list (the n8n gate counts it); `text` is the ready-to-send message, so
    formatting and escaping live here instead of in an n8n expression."""
    due = reminders_worker.get_due_now()
    text, render_id = "", None
    if due:
        tracked, lines = [], []
        for r in due:
            line = f"- {_esc(r.get('text', r) if isinstance(r, dict) else r)}"
            lines.append(line)
            if isinstance(r, dict) and r.get("id") is not None:
                tracked.append({"id": r["id"], "line": line})
        text = _title("Reminder") + "\n\n" + "\n".join(lines)
        render_id = sent_store.new_render("reminder", text, tracked)
    return {"due": due, "text": text, "render_id": render_id}


def _pretty_time(hhmm) -> str:
    """24h -> 9am / 9:30pm style, matching the rest of the bot."""
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
    except (ValueError, AttributeError):
        return str(hhmm)
    suf = "am" if h < 12 else "pm"
    h = h % 12 or 12
    return f"{h}{'' if m == 0 else ':%02d' % m}{suf}"


def _day_heading(iso: str) -> str:
    """'2026-07-28' -> 'Monday 28 Jul' for a schedule day heading."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%A %d %b")
    except (ValueError, TypeError):
        return str(iso)


def _describe_when(a: dict) -> str:
    """Human phrasing for when a freshly added reminder will surface."""
    if a.get("kind") == "daily":
        return (f"every day at {_pretty_time(a['fire_time'])}" if a.get("fire_time")
                else "every day in the morning digest")
    if a.get("kind") == "weekly":
        days = reminders_worker.describe_days(a.get("days_of_week"))
        return (f"{days} at {_pretty_time(a['fire_time'])}" if a.get("fire_time")
                else f"{days}, in the morning digest")
    if a.get("kind") == "deadline":
        return f"due {a.get('due_date')}" + (f", nudging {a['lead_days']}d ahead"
                                             if a.get("lead_days") else "")
    return (a.get("due_date") or "today") + (f" at {_pretty_time(a['fire_time'])}"
                                             if a.get("fire_time") else "")


def _add_one(item) -> tuple[bool, str]:
    """Add a single reminder. Returns (ok, one rendered line, already escaped)."""
    if not isinstance(item, dict):
        return False, "couldn't read one of those items"
    res = reminders_add(
        text=item.get("text") or "reminder", kind=item.get("kind") or "once",
        due_date=item.get("due_date"), fire_time=item.get("fire_time"),
        lead_days=int(item.get("lead_days") or 0),
        days_of_week=item.get("days_of_week"))
    if res.get("error"):
        return False, f"{_esc(item.get('text') or 'that one')} -- {_esc(res['error'])}"
    a = res.get("added", {})
    return True, f"{_esc(a.get('text'))} ({_describe_when(a)})"


@app.post("/reminders/act")
async def reminders_act(request: Request):
    """Run the scheduler bot's JSON: {action: add|list|complete|remove|chat, ...},
    or a JSON LIST of add-objects when one message names several reminders.
    Returns {'text': <finished HTML reply>} for the bot to send as-is. The
    scheduler bot emits JSON instead of using tool-calling (tool schemas were
    ~90% of its token cost), so all the logic lives here.

    Lists are add-only: batching complete/remove would need matching semantics
    (what if two of three match nothing?) and isn't wanted yet."""
    nl = chr(10)
    raw = (await request.body()).decode("utf-8", "replace").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw[:4].lower() == "json":
            raw = raw[4:]
    raw = raw.strip()
    try:
        j = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {"text": _title("Scheduler") + nl + nl
                + "Sorry master, I didn't catch that -- try 'remind me to email my prof at 5pm tomorrow'."}

    # Several reminders in one message arrive as a JSON array. Partial success is
    # deliberate: dropping two good reminders because the third was ambiguous
    # would be worse than saving those two and naming the one that failed.
    if isinstance(j, list):
        added, failed = [], []
        for item in j:
            ok, line = _add_one(item)
            (added if ok else failed).append(line)
        if not added and not failed:
            return {"text": _title("Scheduler") + nl + nl + "Nothing to add, master."}
        parts = []
        if added:
            parts.append(_title("Reminders Set" if len(added) > 1 else "Reminder Set")
                         + nl + nl + nl.join(f"- {line}" for line in added))
        if failed:
            parts.append("<b>Couldn't set</b>" + nl
                         + nl.join(f"- {line}" for line in failed))
        return {"text": (nl + nl).join(parts)}

    action = (j.get("action") or "chat").lower()

    if action == "add":
        ok, line = _add_one(j)
        if not ok:
            return {"text": _title("Hmm") + nl + nl + line}
        return {"text": _title("Reminder Set") + nl + nl + f"- {line}"}

    if action == "list":
        # Title is the timeframe the user asked for (bold, no date suffix), e.g.
        # "Upcoming Week", "Today". Dated items are grouped under a bold day
        # heading; recurring 'daily' items sit under "Every day". No ids shown.
        title = _esc(j.get("title") or "Your Reminders")
        fromd, tod = j.get("from_date"), j.get("to_date")
        rows = reminders_worker.list_active()

        def _in_window(d):
            if fromd and d < fromd:
                return False
            if tod and d > tod:
                return False
            return True

        daily = [r for r in rows if r.get("kind") == "daily"]
        weekly = [r for r in rows if r.get("kind") == "weekly"]
        dated = [r for r in rows if r.get("due_date") and _in_window(r["due_date"])]

        sections = []
        if daily:
            items = [f"- {_esc(r['text'])}"
                     + (f" at {_pretty_time(r['fire_time'])}" if r.get("fire_time") else "")
                     for r in daily]
            sections.append("<b>Every day</b>" + nl + nl.join(items))
        if weekly:
            items = [f"- {_esc(r['text'])} ({reminders_worker.describe_days(r.get('days_of_week'))}"
                     + (f" at {_pretty_time(r['fire_time'])}" if r.get("fire_time") else "")
                     + ")" for r in weekly]
            sections.append("<b>Every week</b>" + nl + nl.join(items))

        by_day = {}
        for r in sorted(dated, key=lambda x: (x["due_date"], x.get("fire_time") or "")):
            by_day.setdefault(r["due_date"], []).append(r)
        for day in sorted(by_day):
            items = []
            for r in by_day[day]:
                if r.get("kind") == "once" and r.get("fire_time"):
                    extra = f" at {_pretty_time(r['fire_time'])}"
                elif r.get("kind") == "deadline":
                    extra = " (deadline)"
                else:
                    extra = ""
                items.append(f"- {_esc(r['text'])}{extra}")
            sections.append(f"<b>{_day_heading(day)}</b>" + nl + nl.join(items))

        if not sections:
            return {"text": "<b>" + title + "</b>" + nl + nl + "Nothing scheduled, master."}
        return {"text": "<b>" + title + "</b>" + nl + nl + (nl + nl).join(sections)}

    if action == "complete":
        rid = j.get("id")
        rows = reminders_worker.list_active()
        done_text = None
        if not rid and j.get("text"):
            needle = str(j["text"]).lower()
            for r in rows:
                if needle in r["text"].lower():
                    rid, done_text = r["id"], r["text"]
                    break
        if rid and done_text is None:
            done_text = next((r["text"] for r in rows if r["id"] == int(rid)), None)
        ok = reminders_worker.complete(int(rid or 0))
        if not ok:
            return {"text": _title("Done") + nl + nl
                    + "I couldn't find that one, master -- try listing them first."}
        # strike it through in any digest / reminder notification already sent
        edited = _restrike(int(rid)) if rid else 0
        shown = f"<s>{_esc(done_text)}</s>" if done_text else "that"
        tail = (f" (crossed off in {edited} sent message{'s' if edited != 1 else ''})"
                if edited else "")
        return {"text": _title("Done") + nl + nl + f"Marked {shown} done, master.{tail}"}

    if action in ("remove", "delete", "cancel"):
        rid = j.get("id")
        rows = reminders_worker.list_active()
        if not rid and j.get("text"):
            needle = str(j["text"]).lower()
            for r in rows:
                if needle in r["text"].lower():
                    rid = r["id"]
                    break
        removed = next((r["text"] for r in rows if r["id"] == int(rid or 0)), None)
        ok = reminders_worker.delete(int(rid or 0))
        if ok and removed:
            msg = "Removed " + repr(removed) + " from your schedule, master."
        elif ok:
            msg = "Removed it, master."
        else:
            msg = "I couldn't find that one to remove, master -- try listing them first."
        return {"text": _title("Removed") + nl + nl + _esc(msg)}

    return {"text": _title("Scheduler") + nl + nl
            + _esc(j.get("message") or "I only handle reminders and to-dos, master.")}


@app.get("/reminders/digest")


def reminders_digest():
    """The 7:30am briefing -- one push covering the day ahead: to-dos, school work
    due, and job deadlines. ALWAYS returns sendable text so it works as a
    dependable daily heartbeat; `count` is floored at 1 because the n8n
    "Anything to send?" gate (count > 0) is what releases the message and we now
    always want it released. `items` keeps the truthful count."""
    reminders_worker.purge_expired()
    sections = []
    items = 0

    tracked = []
    todo = reminders_worker.get_digest_rows()
    if todo:
        items += len(todo)
        lines = []
        for r in todo:
            line = f"- {_esc(r['line'])}"
            lines.append(line)
            tracked.append({"id": r["id"], "line": line})
        sections.append("<b>To Do</b>\n" + "\n".join(lines))

    later = reminders_worker.get_today_timed_rows()
    if later:
        items += len(later)
        lines = []
        for r in later:
            line = f"- {_esc(r['line'])}"
            lines.append(line)
            tracked.append({"id": r["id"], "line": line})
        sections.append("<b>Later Today</b>\n" + "\n".join(lines))

    jobs = (_workers_get("/run/job_deadline_reminders?days_ahead=3", {}) or {}).get("reminders") or []
    if jobs:
        items += len(jobs)
        sections.append("<b>Job Deadlines</b>\n" + "\n".join(
            "- {} - {}: {}".format(
                "TODAY" if j.get("days_left") == 0 else f"{j.get('days_left')}d",
                _esc(j.get("company", "")), _esc(j.get("title", "")))
            for j in jobs))

    if sections:
        body = "Good morning, master. Here's your day:\n\n" + "\n\n".join(sections)
    else:
        body = ("Good morning, master. Nothing on the books today, "
                "unless I don't know about it.")
    text = _title("Morning Digest") + "\n\n" + body
    return {"text": text, "count": max(items, 1), "items": items,
            "render_id": sent_store.new_render("digest", text, tracked)}
