"""Fetch job postings from known Greenhouse/Lever company boards,
filter for backend/software-engineering internships in Toronto/Kingston,
and report only postings not already seen in a previous run."""

import time

import requests
from matching import matches_criteria
from job_store import init_db, is_new, mark_seen
from workday_worker import WORKDAY_COMPANIES, fetch_workday
from direct_boards import DIRECT_SOURCES
from dates import normalize_posted
import obsidian_writer

GREENHOUSE_COMPANIES = [
    "anthropic", "spacex", "linkedin", "lyft", "airbnb", "stripe", "block",
    "instacart", "godaddy", "coinbase", "gemini", "ripple", "consensys",
    "robinhood", "kalshi", "okx", "paradigm",
]
LEVER_COMPANIES = ["palantir", "linkedin", "anchorage"]
ASHBY_COMPANIES = [
    "circle", "alchemy", "uniswap", "opensea", "paradigm", "phantom",
    "magiceden", "kraken", "openai", "ramp", "ethglobal",
]


def fetch_greenhouse(slug):
    try:
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=8)
        if r.status_code != 200:
            return []
        return [
            (
                f"greenhouse:{j['id']}",
                j["title"],
                j.get("location", {}).get("name", ""),
                j.get("absolute_url", ""),
                j.get("application_deadline"),  # often null -- most postings don't set one
                normalize_posted(j.get("first_published")),
            )
            for j in r.json().get("jobs", [])
        ]
    except Exception:
        return []


def fetch_lever(slug):
    try:
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=8)
        data = r.json()
        if not isinstance(data, list):
            return []
        return [
            (
                f"lever:{j.get('id')}",
                j.get("text", ""),
                j.get("categories", {}).get("location", ""),
                j.get("hostedUrl", ""),
                None,  # Lever postings don't expose a deadline field
                normalize_posted(j.get("createdAt")),
            )
            for j in data
        ]
    except Exception:
        return []


def fetch_ashby(slug):
    try:
        r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=8)
        if r.status_code != 200:
            return []
        return [
            (
                f"ashby:{j.get('id')}",
                j.get("title", ""),
                j.get("location", ""),
                j.get("jobUrl", ""),
                None,  # Ashby postings don't expose a deadline field
                normalize_posted(j.get("publishedAt") or j.get("updatedAt")),
            )
            for j in r.json().get("jobs", [])
            if j.get("isListed", True)
        ]
    except Exception:
        return []


def run(
    role_keywords: list[str] | None = None,
    require_intern: bool = True,
    locations: list[str] | None = None,
    timeframe: str | None = None,
):
    init_db()
    new_matches = []

    for slug in GREENHOUSE_COMPANIES:
        for job_key, title, location, link, deadline, posted in fetch_greenhouse(slug):
            if matches_criteria(title, location, role_keywords, require_intern, locations, timeframe) and is_new(job_key):
                mark_seen(job_key, "greenhouse", title, slug, location, link, deadline, posted)
                new_matches.append({"source": slug, "title": title, "location": location,
                                     "link": link, "deadline": deadline, "posted": posted})

    for slug in LEVER_COMPANIES:
        for job_key, title, location, link, deadline, posted in fetch_lever(slug):
            if matches_criteria(title, location, role_keywords, require_intern, locations, timeframe) and is_new(job_key):
                mark_seen(job_key, "lever", title, slug, location, link, deadline, posted)
                new_matches.append({"source": slug, "title": title, "location": location,
                                     "link": link, "deadline": deadline, "posted": posted})

    for slug in ASHBY_COMPANIES:
        for job_key, title, location, link, deadline, posted in fetch_ashby(slug):
            if matches_criteria(title, location, role_keywords, require_intern, locations, timeframe) and is_new(job_key):
                mark_seen(job_key, "ashby", title, slug, location, link, deadline, posted)
                new_matches.append({"source": slug, "title": title, "location": location,
                                     "link": link, "deadline": deadline, "posted": posted})

    # Workday (big banks/insurers) is best-effort -- their anti-bot systems
    # block bursts, so we pace requests and tolerate failures gracefully.
    for i, name in enumerate(WORKDAY_COMPANIES):
        if i:
            time.sleep(5)
        for job_key, title, location, link, deadline, posted in fetch_workday(name):
            if matches_criteria(title, location, role_keywords, require_intern, locations, timeframe) and is_new(job_key):
                mark_seen(job_key, "workday", title, name, location, link, deadline, posted)
                new_matches.append({"source": name, "title": title, "location": location,
                                     "link": link, "deadline": deadline, "posted": posted})

    # Big companies running their own careers API (Amazon, Oracle, Microsoft, RBC)
    for name, fetch in DIRECT_SOURCES.items():
        for job_key, title, location, link, deadline, posted in fetch():
            if matches_criteria(title, location, role_keywords, require_intern, locations, timeframe) and is_new(job_key):
                mark_seen(job_key, name, title, name, location, link, deadline, posted)
                new_matches.append({"source": name, "title": title, "location": location,
                                     "link": link, "deadline": deadline, "posted": posted})

    if new_matches and obsidian_writer.VAULT_PATH:
        lines = [f"- **{m['title']}** ({m['source']}) -- {m['location']}"
                 + (f" -- posted {m['posted']}" if m.get('posted') else "")
                 + f" -- {m['link']}"
                 for m in new_matches]
        obsidian_writer.append_note("Jobs", "New postings found (Greenhouse/Lever)", "\n".join(lines))

    return new_matches


if __name__ == "__main__":
    matches = run()
    print(f"{len(matches)} new matching postings")
    for m in matches:
        print(m)
