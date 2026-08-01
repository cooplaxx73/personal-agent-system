"""Always-on CLOUD API (Oracle VM): reminders + digest + health. The PC was
retired 2026-07-23, so jobs/email/onQ now run in the workers API on this same VM
(:8002), reached over localhost. reminders.db location is set by REMINDERS_DB.

The digest is the one daily briefing: reminders (local) plus school work and job
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
import queue_store
import sent_store

app = FastAPI()

PC_BASE = os.environ.get("PC_API", "")
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


def pc_reachable() -> bool:
    # fast TCP port check -- doesn't hit the slow /health/check (which does live
    # session validation and can take ~4s), just confirms the PC API is listening.
    u = urlparse(PC_BASE)
    try:
        with socket.create_connection((u.hostname, u.port or 8001), timeout=4):
            return True
    except OSError:
        return False


# --- PC proxy: forward to the PC when it's on, else queue for later ------------
@app.get("/pc/{path:path}")
def pc_proxy(path: str, request: Request):
    """Every PC-dependent tool calls the PC through here. If the PC is reachable
    we forward and return its result; if it's off we queue the exact call and
    tell the user, so nothing errors when the PC is asleep."""
    query = str(request.url.query)
    label = path.rstrip("/").split("/")[-1]
    if pc_reachable():
        try:
            url = f"{PC_BASE}/{path}" + (f"?{query}" if query else "")
            return requests.get(url, timeout=180).json()
        except Exception as e:  # noqa: BLE001 - PC dropped mid-call -> queue it
            qid = queue_store.add(path, query, label)
            return {"queued": True, "id": qid,
                    "message": f"Your PC hiccuped mid-task, so I queued this (#{qid}); "
                               f"it'll run when the PC's back."}
    qid = queue_store.add(path, query, label)
    return {"queued": True, "id": qid,
            "message": f"Your PC is off right now, so I've queued this (#{qid}). It'll run "
                       f"automatically when your PC is back — say 'show my queue' anytime."}


@app.get("/queue/list")
def queue_list():
    return {"queue": queue_store.all_items()}


@app.get("/queue/summary")
def queue_summary():
    items = queue_store.all_items()
    if not items:
        return {"text": "Your queue is empty, master.", "count": 0}
    lines = [f"#{i['id']} {i['label']}" + (f" ({i['query']})" if i['query'] else "")
             for i in items]
    return {"text": "Queued for when your PC is back:\n" + "\n".join(f"- {l}" for l in lines),
            "count": len(items)}


@app.get("/queue/add")
def queue_add(path: str = Query(...), query: str = ""):
    return {"added": queue_store.add(path, query)}


@app.get("/queue/remove")
def queue_remove(id: int):
    return {"removed": queue_store.remove(id)}


@app.get("/queue/process")
def queue_process():
    """Replay queued PC calls if the PC is back. Called on a schedule."""
    if not pc_reachable():
        return {"processed": 0, "reason": "PC still off"}
    results = []
    for item in queue_store.all_items():
        try:
            url = f"{PC_BASE}/{item['path']}" + (f"?{item['query']}" if item['query'] else "")
            requests.get(url, timeout=180)
            queue_store.remove(item["id"])
            results.append({"id": item["id"], "label": item["label"], "ok": True})
        except Exception as e:  # noqa: BLE001
            results.append({"id": item["id"], "label": item["label"], "ok": False, "error": str(e)})
    return {"processed": sum(1 for x in results if x["ok"]),
            "still_queued": len(queue_store.all_items()), "results": results}


@app.get("/health/check")
def health_check():
    """Session/token health, used by the re-auth alert. The real checks (onQ,
    Gmail) live with the workers on :8002; this was hardcoded to an empty list
    back when they ran on the PC, which silently killed the alert once the PC
    was retired."""
    issues = (_workers_get("/health/check", {}) or {}).get("issues") or []
    text = ""
    if issues:
        text = (_title("Action Needed") + "\n\nHeads up, master:\n"
                + "\n".join(f"- {_esc(i)}" for i in issues))
    return {"issues": issues, "text": text}


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


@app.post("/reminders/act")
async def reminders_act(request: Request):
    """Run the scheduler bot's JSON: {action: add|list|complete|chat, ...}.
    Returns {'text': <finished HTML reply>} for the bot to send as-is. The
    scheduler bot emits JSON instead of using tool-calling (tool schemas were
    ~90% of its token cost), so all the logic lives here."""
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

    action = (j.get("action") or "chat").lower()

    if action == "add":
        res = reminders_add(
            text=j.get("text") or "reminder", kind=j.get("kind") or "once",
            due_date=j.get("due_date"), fire_time=j.get("fire_time"),
            lead_days=int(j.get("lead_days") or 0),
            days_of_week=j.get("days_of_week"))
        if res.get("error"):
            return {"text": _title("Hmm") + nl + nl + _esc(res["error"])}
        a = res.get("added", {})
        if a.get("kind") == "daily":
            when = (f"every day at {_pretty_time(a['fire_time'])}" if a.get("fire_time")
                    else "every day in the morning digest")
        elif a.get("kind") == "weekly":
            days = reminders_worker.describe_days(a.get("days_of_week"))
            when = (f"{days} at {_pretty_time(a['fire_time'])}" if a.get("fire_time")
                    else f"{days}, in the morning digest")
        elif a.get("kind") == "deadline":
            when = f"due {a.get('due_date')}" + (f", nudging {a['lead_days']}d ahead"
                                                 if a.get("lead_days") else "")
        else:
            when = (a.get("due_date") or "today") + (f" at {_pretty_time(a['fire_time'])}"
                                                     if a.get("fire_time") else "")
        return {"text": _title("Reminder Set") + nl + nl + f"- {_esc(a.get('text'))} ({when})"}

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

    # two-touch pattern (day-of + early warning) -- matches onq_worker's design
    onq = (_workers_get("/run/onq_reminders?early_days=2", {}) or {}).get("reminders") or []
    if onq:
        items += len(onq)
        sections.append("<b>School Work Due</b>\n" + "\n".join(
            "- {} - {}: {} ({})".format(
                "TODAY" if r.get("days_left") == 0 else f"{r.get('days_left')}d",
                _esc(r.get("course", "")), _esc(r.get("title", "")), _esc(r.get("type", "")))
            for r in onq))

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
