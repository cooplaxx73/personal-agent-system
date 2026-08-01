"""Normalize the many 'posted date' formats different job sources use into
a simple YYYY-MM-DD string (or '' if unknown)."""

from datetime import datetime, timezone

from dateutil import parser as dateparser


def normalize_posted(value) -> str:
    if value is None or value == "":
        return ""
    # epoch milliseconds (Lever createdAt, Microsoft postedTs)
    if isinstance(value, (int, float)):
        try:
            ts = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return ""
    # numeric string
    s = str(value).strip()
    if s.isdigit():
        return normalize_posted(int(s))
    # ISO / human date string
    try:
        return dateparser.parse(s).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OverflowError):
        # relative strings like "Posted 3 Days Ago" -- keep as-is, it's still useful
        return s
