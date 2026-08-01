"""Fetch important academic/career emails via the official Gmail API,
across multiple Google accounts.

One shared credentials.json (your Google Cloud OAuth client) works for all
accounts -- you just run the one-time login once per account, and each
produces its own token file. Add your account names to ACCOUNTS below.
Never sees or handles your password; each login opens your browser for you
to pick that account and click Allow.
"""

import base64
import os.path
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
# ONLY these accounts may send. Everything else stays strictly read-only.
SEND_ACCOUNTS = ["primary", "tech"]


def _scopes_for(account: str) -> list[str]:
    return [READ_SCOPE, SEND_SCOPE] if account in SEND_ACCOUNTS else [READ_SCOPE]


WORKERS_DIR = Path(__file__).parent
CREDENTIALS_PATH = str(WORKERS_DIR / "credentials.json")

# Rename these to whatever's meaningful to you -- each needs its own
# one-time login (see gmail_login.py) before it'll actually fetch anything.
ACCOUNTS = ["primary", "secondary", "tech", "parents"]

# Virtual "inboxes": mail that physically lands in a real account (via external
# forwarding) but should be treated as its own separate source. Each = the real
# account it lands in + a Gmail query isolating it. Its mail gets tagged with the
# virtual name AND excluded from the real account's own results -- so forwarded
# Queen's mail shows up as [queens], not lumped in with the personal account.
VIRTUAL_ACCOUNTS = {
    "queens": {"source": "secondary", "match": "to:queensu.ca"},
}

DEFAULT_QUERY = "newer_than:2d (deadline OR interview OR application OR assignment OR exam)"


def _token_path(account: str) -> str:
    return str(WORKERS_DIR / f"token_{account}.json")


def get_service(account: str):
    scopes = _scopes_for(account)
    token_path = _token_path(account)
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, scopes)
            creds = flow.run_local_server(port=0)  # pick the account to log into here
        with open(token_path, "w") as token:
            token.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def check_token_valid(account: str) -> bool:
    if not (os.path.exists(_token_path(account)) and os.path.exists(CREDENTIALS_PATH)):
        return False
    try:
        service = get_service(account)
        service.users().getProfile(userId="me").execute()
        return True
    except Exception:
        return False


def _extract_body(payload: dict, limit: int = 2000) -> str:
    """Pull plain-text body out of a Gmail message payload (walks MIME parts,
    prefers text/plain). Truncated to `limit` chars to keep it lightweight."""
    def walk(part):
        data = part.get("body", {}).get("data")
        if part.get("mimeType") == "text/plain" and data:
            return base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
        for sub in part.get("parts", []):
            text = walk(sub)
            if text:
                return text
        return ""

    return " ".join(walk(payload).split())[:limit]


def fetch_recent_important(account: str, query: str = DEFAULT_QUERY,
                           max_results: int = 20, include_body: bool = False) -> list[dict]:
    service = get_service(account)
    results = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    messages = results.get("messages", [])

    items = []
    for msg in messages:
        # 'full' is needed to read the body; 'metadata' is lighter when we don't
        kwargs = {"userId": "me", "id": msg["id"], "format": "full" if include_body else "metadata"}
        if not include_body:
            kwargs["metadataHeaders"] = ["Subject", "From", "Date"]
        full = service.users().messages().get(**kwargs).execute()
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        item = {
            "account": account,
            "subject": headers.get("Subject", ""),
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "snippet": full.get("snippet", ""),
        }
        if include_body:
            item["body"] = _extract_body(full["payload"])
        items.append(item)
    return items


def _exclusions_for(account: str) -> str:
    """If a real account is the landing spot for virtual inbox(es), build the
    query terms that keep that forwarded mail OUT of the account's own results."""
    return " ".join(f"-{cfg['match']}" for cfg in VIRTUAL_ACCOUNTS.values()
                    if cfg["source"] == account)


def fetch_all_accounts(query: str = DEFAULT_QUERY, max_results: int = 20,
                       include_body: bool = False) -> list[dict]:
    """Fetch from every logged-in account (skipping ones without a token), then
    the virtual inboxes. Forwarded mail is tagged with its virtual name and kept
    out of the host account's own results, so sources stay cleanly separated."""
    all_items = []
    for account in ACCOUNTS:
        if not os.path.exists(_token_path(account)):
            continue
        q = f"{query} {_exclusions_for(account)}".strip()
        try:
            all_items.extend(fetch_recent_important(account, q, max_results, include_body))
        except Exception:
            continue  # likely needs re-auth -- the health check catches that separately

    # virtual inboxes: fetch from the host account, isolated by their match query,
    # then re-tag the results with the virtual name
    for vname, cfg in VIRTUAL_ACCOUNTS.items():
        src = cfg["source"]
        if not os.path.exists(_token_path(src)):
            continue
        vq = f"{query} {cfg['match']}".strip()
        try:
            items = fetch_recent_important(src, vq, max_results, include_body)
            for it in items:
                it["account"] = vname
            all_items.extend(items)
        except Exception:
            continue
    return all_items


def check_all_accounts_valid() -> list[str]:
    """Which configured accounts have an expired/invalid token and need
    you to log in again?"""
    invalid = []
    for account in ACCOUNTS:
        if os.path.exists(_token_path(account)) and not check_token_valid(account):
            invalid.append(account)
    return invalid


if __name__ == "__main__":
    import json
    items = fetch_all_accounts()
    with open("gmail_results.txt", "w", encoding="utf-8") as f:
        f.write(json.dumps(items, indent=2))
    print(f"done, {len(items)} emails matched across all logged-in accounts, see gmail_results.txt")
