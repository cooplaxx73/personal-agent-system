"""Summarize recent important emails on the LOCAL Ollama model (free), so the
cloud bot only has to relay a short digest -- keeps OpenRouter cost minimal.

The expensive work (reading long email bodies) happens on-device; OpenRouter
never sees more than the finished few-line summary.
"""

import gmail_worker
from local_llm import generate

MAX_EMAILS = 15     # cap the batch so one local pass stays quick
BODY_CHARS = 1200   # trim each body -- signatures/threads are noise, not signal

SUMMARY_PROMPT = """You are a concise assistant summarizing a student's recent \
important emails (school, job/career, personal admin). For each email that \
actually matters, give ONE short bullet in this format:
- Sender (name only) - subject - the key point, plus any action needed and its deadline.
Ignore marketing, promotions, sales, and newsletters entirely - do not list them. \
If nothing important needs attention, just say "Nothing important in the recent \
window." Keep it tight. Output only the bullet list, no preamble.

EMAILS:
{emails}
"""


def summarize_recent(query: str | None = None, max_emails: int = MAX_EMAILS) -> dict:
    """Fetch recent important mail across all logged-in accounts and return a
    short AI summary. Returns {'summary': str, 'count': int} (or an 'error')."""
    q = query or gmail_worker.DEFAULT_QUERY
    items = gmail_worker.fetch_all_accounts(query=q, include_body=True)
    if not items:
        return {"summary": "No important emails in the recent window.", "count": 0}

    items = items[:max_emails]
    blocks = []
    for it in items:
        body = it.get("body") or it.get("snippet") or ""
        blocks.append(
            f"[{it['account']}] From: {it['from']}\nSubject: {it['subject']}\n"
            f"{body[:BODY_CHARS]}\n---")

    try:
        # summarizing = high-context, low-reasoning -> 'fast' tier (qwen2.5:14b)
        summary = generate(SUMMARY_PROMPT.format(emails="\n".join(blocks)), tier="fast")
    except Exception as e:  # noqa: BLE001
        return {"error": f"Local AI (Ollama) unavailable: {e}", "summary": "", "count": len(items)}
    return {"summary": summary, "count": len(items)}


if __name__ == "__main__":
    out = summarize_recent()
    print(f"summarized {out.get('count')} email(s):\n")
    print(out.get("summary") or out.get("error"))
