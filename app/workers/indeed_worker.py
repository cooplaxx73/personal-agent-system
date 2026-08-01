"""Search Indeed (Canada) for backend/software-engineering internships in
Toronto/Kingston, reporting only postings not already seen in a previous run."""

from playwright.sync_api import sync_playwright
from matching import matches_criteria, looks_blocked


class BlockedError(RuntimeError):
    """Raised when a bot-block page is served instead of results."""
from job_store import init_db, is_new, mark_seen
from dates import normalize_posted
import obsidian_writer

SEARCH_QUERY = "software engineer intern"
LOCATIONS = ["Toronto, ON", "Kingston, ON", "Remote"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def search_indeed(query: str, location: str) -> list[dict]:
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        page = browser.new_page(user_agent=USER_AGENT, locale="en-CA")

        q = query.replace(" ", "+")
        loc = location.replace(" ", "+").replace(",", "%2C")
        url = f"https://ca.indeed.com/jobs?q={q}&l={loc}"

        page.goto(url, timeout=20000)
        page.wait_for_timeout(3000)

        # include the title -- the challenge page says "Just a moment..." there
        # while its body text on its own can look like an ordinary Indeed header
        if looks_blocked(page.title() + " " + page.inner_text("body")):
            # Surfacing this matters: a block returns zero cards, which is
            # indistinguishable from "no jobs matched" unless we say so.
            browser.close()
            raise BlockedError("Indeed is blocking this server (Cloudflare)")

        cards = page.query_selector_all("div.job_seen_beacon")
        for card in cards:
            title_el = card.query_selector("h3.jobTitle span[title]")
            company_el = card.query_selector('span[data-testid="company-name"]')
            location_el = card.query_selector('div[data-testid="text-location"]')
            link_el = card.query_selector("h3.jobTitle a")
            job_key = link_el.get_attribute("data-jk") if link_el else None
            # Indeed shows a relative date, e.g. "Posted 3 days ago" / "Just posted".
            # It has moved around selectors over time, so try the known ones.
            date_el = (card.query_selector('span[data-testid="myJobsStateDate"]')
                       or card.query_selector("span.date")
                       or card.query_selector('[class*="date"]'))

            results.append({
                "job_key": f"indeed:{job_key}" if job_key else None,
                "title": title_el.inner_text() if title_el else "",
                "company": company_el.inner_text() if company_el else "",
                "location": location_el.inner_text() if location_el else "",
                "link": f"https://ca.indeed.com/viewjob?jk={job_key}" if job_key else None,
                "posted": normalize_posted(date_el.inner_text().replace("Posted", "").strip()
                                           if date_el else ""),
            })

        browser.close()
    return results


def run(
    search_query: str | None = None,
    search_locations: list[str] | None = None,
    role_keywords: list[str] | None = None,
    require_intern: bool = True,
):
    search_query = search_query or SEARCH_QUERY
    search_locations = search_locations or LOCATIONS
    # derive the substring used for matching from each search location,
    # e.g. "Toronto, ON" -> "toronto"
    match_locations = [loc.split(",")[0].strip().lower() for loc in search_locations]

    init_db()
    new_matches = []

    for loc in search_locations:
        for j in search_indeed(search_query, loc):
            if not j["job_key"]:
                continue
            if matches_criteria(j["title"], j["location"], role_keywords, require_intern, match_locations) \
                    and is_new(j["job_key"]):
                mark_seen(j["job_key"], "indeed", j["title"], j["company"], j["location"],
                          j["link"], None, j.get("posted"))
                new_matches.append(j)

    if new_matches and obsidian_writer.VAULT_PATH:
        lines = [f"- **{m['title']}** at {m['company']} -- {m['location']}"
                 + (f" -- posted {m['posted']}" if m.get('posted') else "")
                 + f" -- {m['link']}"
                 for m in new_matches]
        obsidian_writer.append_note("Jobs", "New postings found (Indeed)", "\n".join(lines))

    return new_matches


if __name__ == "__main__":
    matches = run()
    with open("results.txt", "w", encoding="utf-8") as f:
        f.write(f"{len(matches)} new matching postings\n")
        for m in matches:
            f.write(f"{m}\n")
    print("done, see results.txt")
