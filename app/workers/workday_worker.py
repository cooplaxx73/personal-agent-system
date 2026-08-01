"""Fetch postings from companies running on Workday's public CxS API.

Workday powers many big banks / insurers / enterprises. Its careers page is
backed by a JSON POST endpoint, so this is a clean API integration (one
generic function), not fragile HTML scraping.

Endpoints below were discovered by probing; add more as (tenant, dc, site).
Workday search is fuzzy, so we pass a searchText to cut volume server-side
then apply our own strict filter (matches_criteria) client-side.
"""

import requests

# name -> (tenant, datacenter, site)
WORKDAY_COMPANIES = {
    "sunlife": ("sunlife", "wd3", "Experienced"),      # insurance
    "td": ("td", "wd3", "TD_Bank_Careers"),            # bank
    "bmo": ("bmo", "wd3", "External"),                 # bank
    "manulife": ("manulife", "wd3", "MFCJH_Jobs"),     # insurance
    "salesforce": ("salesforce", "wd12", "External_Career_Site"),
}

DEFAULT_SEARCH_TEXT = "intern"

# Workday rejects requests without a browser-like User-Agent (returns 400)
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Content-Type": "application/json",
    "Accept": "application/json",
}


# Workday rejects any limit above 20 with HTTP 400 -- asking for 50 made every
# tenant return zero postings silently for months. Do not raise this.
MAX_PAGE = 20
DEFAULT_PAGES = 5


def fetch_workday(name: str, search_text: str = DEFAULT_SEARCH_TEXT,
                  limit: int = MAX_PAGE, pages: int = DEFAULT_PAGES):
    """Returns (job_key, title, location, link, deadline, posted) tuples, matching
    the shape the other fetchers use so jobs_worker can treat them uniformly.

    Pages through the result set because one page is only 20 postings and the
    fuzzy searchText returns hundreds; relevant hits cluster early but not all
    land on page one."""
    if name not in WORKDAY_COMPANIES:
        return []
    tenant, dc, site = WORKDAY_COMPANIES[name]
    host = f"https://{tenant}.{dc}.myworkdayjobs.com"
    url = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    page_size = min(limit, MAX_PAGE)
    out = []
    try:
        # Workday 400s on cold programmatic hits; priming the session with a
        # GET to the careers page first establishes the cookies it expects.
        session = requests.Session()
        session.headers.update(HEADERS)
        session.get(f"{host}/en-US/{site}", timeout=10)
        for page in range(max(1, pages)):
            body = {"appliedFacets": {}, "limit": page_size,
                    "offset": page * page_size, "searchText": search_text}
            r = session.post(url, json=body, timeout=10)
            if r.status_code != 200:
                # a mid-pagination failure still returns what we already have
                print(f"[workday] {name} page {page} HTTP {r.status_code}", flush=True)
                break
            postings = r.json().get("jobPostings", [])
            for p in postings:
                ext = p.get("externalPath", "")
                link = f"{host}/en-US/{site}{ext}" if ext else host
                out.append((
                    f"workday:{name}:{p.get('bulletFields', [''])[0] or ext}",
                    p.get("title", ""),
                    p.get("locationsText", ""),
                    link,
                    None,  # Workday CxS doesn't expose an application deadline here
                    p.get("postedOn", ""),  # relative, e.g. "Posted 3 Days Ago"
                ))
            if len(postings) < page_size:
                break  # last page
        return out
    except Exception as e:
        print(f"[workday] {name} failed: {type(e).__name__}: {e}", flush=True)
        return out


if __name__ == "__main__":
    from matching import matches_criteria
    for name in WORKDAY_COMPANIES:
        rows = fetch_workday(name)
        matches = [(t, l) for k, t, l, link, d, p in rows
                   if matches_criteria(t, l, None, True, ["toronto", "kingston", "remote"])]
        print(f"{name}: {len(rows)} fetched, {len(matches)} intern matches in target locations")
        for t, l in matches[:3]:
            print("   ", t.strip(), "--", l)
