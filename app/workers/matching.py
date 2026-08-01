"""Shared filter: role keywords + optional internship requirement + location.

All criteria are now parameters with sensible defaults, so an agent (or
anyone) can call this with different keywords/locations per request instead
of being stuck with one hardcoded search.

Uses word-boundary matching so substrings like "Internal" or "Internet"
don't false-positive against "intern".
"""

import re

DEFAULT_ROLE_KEYWORDS = ["software", "backend", "developer", "engineer"]
# "online" postings are almost always labeled "Remote" in real listings, not "Online"
DEFAULT_LOCATIONS = ["toronto", "kingston", "remote"]

INTERN_RE = re.compile(r"\b(intern|internship|co-?op)\b", re.IGNORECASE)
SEASON_RE = re.compile(r"\b(summer|fall|autumn|winter|spring)\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b20\d{2}\b")


def matches_timeframe(title: str, timeframe: str | None) -> bool:
    """True unless the title names a season/year that CONFLICTS with the wanted
    timeframe (e.g. drop 'SDE Intern - 2026' when asked for 'summer 2027').
    Most postings say nothing about term in the title -- those always pass,
    so this only removes explicit mismatches, it can't prove a match."""
    if not timeframe:
        return True

    def norm(seasons):
        return {x.lower().replace("autumn", "fall") for x in seasons}

    want_years = set(YEAR_RE.findall(timeframe))
    want_seasons = norm(SEASON_RE.findall(timeframe))
    got_years = set(YEAR_RE.findall(title))
    got_seasons = norm(SEASON_RE.findall(title))
    if want_years and got_years and not (want_years & got_years):
        return False
    if want_seasons and got_seasons and not (want_seasons & got_seasons):
        return False
    return True


def matches_criteria(
    title: str,
    location: str,
    role_keywords: list[str] | None = None,
    require_intern: bool = True,
    locations: list[str] | None = None,
    timeframe: str | None = None,
) -> bool:
    # tolerate empty/whitespace entries from loosely-formatted agent calls
    role_keywords = [k.strip() for k in (role_keywords or []) if k.strip()] or DEFAULT_ROLE_KEYWORDS
    locations = [l.strip() for l in (locations or []) if l.strip()] or DEFAULT_LOCATIONS

    role_pattern = r"\b(" + "|".join(re.escape(k) for k in role_keywords) + r")\b"
    is_role_match = bool(re.search(role_pattern, title, re.IGNORECASE))
    is_intern = bool(INTERN_RE.search(title)) if require_intern else True
    is_location = any(loc.lower() in location.lower() for loc in locations)

    return is_role_match and is_intern and is_location and matches_timeframe(title, timeframe)


BLOCK_SIGNALS = (
    "you have been blocked", "request blocked", "unable to access",
    "verify you are human", "unusual traffic", "captcha",
    "security service to protect", "just a moment",
    # Cloudflare's challenge variant: "Just a moment..." lives in the TITLE and
    # the body only says this, so match the wording explicitly.
    "additional verification required", "ray id",
)


def looks_blocked(page_text: str) -> bool:
    """Is this a bot-block/WAF interstitial rather than real content?

    Datacenter IPs (like the Oracle VM) get Cloudflare-blocked by Indeed and
    Queen's SWEP. Block pages are short and contain none of the usual login
    words, so without this check they read as 'page loaded fine, no results'."""
    low = (page_text or "")[:1500].lower()
    return any(s in low for s in BLOCK_SIGNALS)
