"""Ask the local model to extract structured deadlines/announcements from messy
raw page text, entirely on-device, at zero API cost. Model choice + Ollama
handling live in local_llm (this is a 'fast'-tier, high-context task).

`ensure_ollama_running` is re-exported here so older imports keep working."""

import json

from local_llm import ensure_ollama_running, generate, strip_code_fence  # noqa: F401

PROMPT_TEMPLATE = """You extract assignment deadlines, exam dates, and course \
announcements from messy webpage text.

Return ONLY a JSON array of objects with keys: "title", "type" \
(assignment/exam/announcement), "date" (ISO format if possible, else as \
written), "course". Omit items with no date. Return [] if nothing relevant \
is found. No explanation, only the JSON array.

RAW TEXT:
{raw_text}
"""


def extract_deadlines(raw_text: str) -> list[dict]:
    # high-context extraction, low reasoning -> 'fast' tier (qwen2.5:14b)
    text = strip_code_fence(generate(PROMPT_TEMPLATE.format(raw_text=raw_text), tier="fast"))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


if __name__ == "__main__":
    sample = """
    Welcome to CISC 121! Navigation Home Announcements Grades Content
    <div class="widget">Upcoming: Assignment 3 - Recursion Basics due Friday,
    July 25, 2026 at 11:59pm</div>
    <footer>Copyright Queen's University</footer>
    <div>Midterm Exam scheduled for August 5, 2026, 2:00 PM in Room 201</div>
    <script>trackPageView();</script>
    Announcement: Office hours moved to Wednesdays this week only.
    """
    results = extract_deadlines(sample)
    with open("llm_clean_test.txt", "w", encoding="utf-8") as f:
        f.write(json.dumps(results, indent=2))
    print("done, see llm_clean_test.txt")
