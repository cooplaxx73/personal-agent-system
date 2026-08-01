"""Send email via the Gmail API from send-enabled accounts, with attachments
and optional bulk sending.

Sending is gated to gmail_worker.SEND_ACCOUNTS (primary, tech). Every send here
is only ever reached AFTER an explicit user confirmation -- see the draft/confirm
flow in email_drafts + the /email/* API endpoints. Nothing in this module should
be called directly from the bot without that confirmation step in front of it.
"""
import base64
import mimetypes
import os
from email.message import EmailMessage

import gmail_worker

MAX_BULK = 100  # hard cap on recipients per bulk send, to protect the account


def _as_list(x) -> list[str]:
    if not x:
        return []
    if isinstance(x, str):
        return [p.strip() for p in x.split(",") if p.strip()]
    return [str(p).strip() for p in x if str(p).strip()]


def _account_address(service) -> str:
    return service.users().getProfile(userId="me").execute().get("emailAddress", "")


def _build_raw(sender, to, subject, body, cc=None, bcc=None, attachments=None) -> dict:
    msg = EmailMessage()
    if sender:
        msg["From"] = sender
    msg["To"] = ", ".join(_as_list(to))
    if cc:
        msg["Cc"] = ", ".join(_as_list(cc))
    if bcc:
        msg["Bcc"] = ", ".join(_as_list(bcc))
    msg["Subject"] = subject or ""
    msg.set_content(body or "")
    for path in _as_list(attachments):
        if not os.path.exists(path):
            continue
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(path))
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}


def _guard(account: str):
    if account not in gmail_worker.SEND_ACCOUNTS:
        return {"error": f"account '{account}' is not send-enabled "
                         f"(only {gmail_worker.SEND_ACCOUNTS} can send)"}
    return None


def send(account, to, subject, body, cc=None, bcc=None, attachments=None) -> dict:
    """Send a single email. Returns {'sent': True, 'id', 'from', 'to'} or {'error'}."""
    bad = _guard(account)
    if bad:
        return bad
    try:
        service = gmail_worker.get_service(account)
        sender = _account_address(service)
        raw = _build_raw(sender, to, subject, body, cc, bcc, attachments)
        sent = service.users().messages().send(userId="me", body=raw).execute()
        return {"sent": True, "id": sent.get("id"), "from": sender, "to": _as_list(to)}
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "insufficient" in msg.lower() or "scope" in msg.lower() or "403" in msg:
            msg += " -- re-run gmail_login.py for this account to grant send permission"
        return {"error": f"send failed: {msg}"}


def send_bulk(account, recipients, subject, body, mode="individual", attachments=None) -> dict:
    """mode='bcc' -> one email, everyone BCC'd (they don't see each other);
    mode='individual' -> a separate copy to each recipient (looks personal)."""
    bad = _guard(account)
    if bad:
        return bad
    recips = _as_list(recipients)
    if not recips:
        return {"error": "no valid recipients"}
    if len(recips) > MAX_BULK:
        return {"error": f"too many recipients ({len(recips)}); cap is {MAX_BULK}"}

    if mode == "bcc":
        try:
            service = gmail_worker.get_service(account)
            sender = _account_address(service)
            # To = self, everyone else BCC'd so addresses stay private
            raw = _build_raw(sender, sender, subject, body, bcc=recips, attachments=attachments)
            sent = service.users().messages().send(userId="me", body=raw).execute()
            return {"sent": True, "mode": "bcc", "count": len(recips), "id": sent.get("id")}
        except Exception as e:  # noqa: BLE001
            return {"error": f"send failed: {e}"}

    # individual
    results = []
    for r in recips:
        res = send(account, r, subject, body, attachments=attachments)
        results.append({"to": r, "ok": res.get("sent", False), "error": res.get("error")})
    ok = sum(1 for x in results if x["ok"])
    return {"sent": ok > 0, "mode": "individual", "sent_count": ok,
            "failed": len(results) - ok, "results": results}
