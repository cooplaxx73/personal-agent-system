"""Categorize fetched emails using the local LLM -- read-only, never
modifies the actual Gmail inbox. Feeds subject/sender/snippet (already
small) rather than full email bodies, so this stays cheap even if we
later route it through a cloud model."""

import json

from local_llm import generate, strip_code_fence

PROMPT_TEMPLATE = """Categorize this email into exactly one category: \
"academic" (course/onQ/deadline related), "career" (job/internship/\
recruiting related), "personal", or "other". Also note if it seems to \
require an action or has a deadline mentioned.

Return ONLY a JSON object with keys "category" and "action_needed" \
(a short string describing the action/deadline, or null if none). No \
explanation, only the JSON object.

Subject: {subject}
From: {sender}
Snippet: {snippet}
"""


def categorize_email(email: dict) -> dict:
    # light classification on small input -> 'fast' tier (qwen2.5:14b)
    prompt = PROMPT_TEMPLATE.format(
        subject=email.get("subject", ""),
        sender=email.get("from", ""),
        snippet=email.get("snippet", ""),
    )
    text = strip_code_fence(generate(prompt, tier="fast", timeout=60))
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = {"category": "other", "action_needed": None}

    return {**email, **result}


def categorize_emails(emails: list[dict]) -> list[dict]:
    return [categorize_email(e) for e in emails]


if __name__ == "__main__":
    sample_emails = [
        {
            "subject": "Assignment 3 grade posted",
            "from": "CISC 121 <noreply@onq.queensu.ca>",
            "date": "Mon, 20 Jul 2026 09:00:00 -0400",
            "snippet": "Your grade for Assignment 3 has been posted. Please review by Friday.",
        },
        {
            "subject": "Your application to Stripe",
            "from": "Stripe Recruiting <recruiting@stripe.com>",
            "date": "Mon, 20 Jul 2026 10:00:00 -0400",
            "snippet": "Thank you for applying. We'd like to schedule a phone screen next week.",
        },
        {
            "subject": "Mom",
            "from": "Mom <mom@example.com>",
            "date": "Mon, 20 Jul 2026 11:00:00 -0400",
            "snippet": "Don't forget dinner on Sunday!",
        },
    ]
    results = categorize_emails(sample_emails)
    with open("gmail_organizer_test.txt", "w", encoding="utf-8") as f:
        f.write(json.dumps(results, indent=2))
    print("done, see gmail_organizer_test.txt")
