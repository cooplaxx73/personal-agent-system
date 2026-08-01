"""Fetch postings from big companies that run their own careers API (not a
shared ATS like Greenhouse/Lever/Ashby/Workday).

Each fetcher returns rows in the same 6-field shape every other fetcher uses
    (job_key, title, location, link, deadline, posted)
so jobs_worker can treat them uniformly. Posted dates are normalized to
YYYY-MM-DD via dates.normalize_posted; job_key formats are kept stable so
existing dedup history in jobs.db stays valid.

APIs covered (all verified JSON endpoints, no HTML scraping):
- Amazon   (amazon.jobs search.json)
- Oracle   (Oracle Recruiting Cloud CE)
- Microsoft(apply.careers.microsoft.com pcsx search)
- RBC      (Phenom "widgets" refineSearch API)
"""

import requests

from dates import normalize_posted

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json",
}


def fetch_amazon(search_text="software intern", location="Toronto"):
    url = (f"https://www.amazon.jobs/en/search.json?base_query={requests.utils.quote(search_text)}"
           f"&loc_query={requests.utils.quote(location)}&result_limit=20")
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return out
        for j in r.json().get("jobs", []):
            title = j.get("title", "")
            loc = j.get("normalized_location") or j.get("location", "")
            link = "https://www.amazon.jobs" + j.get("job_path", "")
            out.append((
                f"amazon:{j.get('id', title)}",
                title, loc, link,
                None,  # amazon.jobs search.json has no application-deadline field
                normalize_posted(j.get("posted_date")),
            ))
    except Exception:
        pass
    return out


def fetch_oracle(search_text="intern"):
    url = ("https://eeho.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/"
           "recruitingCEJobRequisitions?onlyData=true"
           "&expand=requisitionList.secondaryLocations"  # required, else requisitionList comes back empty
           f"&finder=findReqs;siteNumber=CX_45001,keyword=%22{requests.utils.quote(search_text)}%22,limit=20,offset=0")
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return out
        items = r.json().get("items", [])
        reqs = items[0].get("requisitionList", []) if items else []
        for j in reqs:
            title = j.get("Title", "")
            location = j.get("PrimaryLocation", "")
            req_id = j.get("Id", title)
            link = f"https://careers.oracle.com/jobs/#en/sites/jobsearch/job/{req_id}"
            out.append((
                f"oracle:{req_id}",
                title, location, link,
                j.get("PostingEndDate"),  # Oracle exposes a real close date (often null)
                normalize_posted(j.get("PostedDate")),
            ))
    except Exception:
        pass
    return out


def fetch_microsoft(search_text="software intern"):
    url = ("https://apply.careers.microsoft.com/api/pcsx/search"
           f"?domain=microsoft.com&query={requests.utils.quote(search_text)}"
           "&location=&start=0&num=20")
    out = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return out
        for j in r.json().get("data", {}).get("positions", []):
            title = j.get("name", "")
            location = "; ".join(j.get("locations") or [])
            job_id = j.get("id", title)
            link = j.get("positionUrl") or f"https://jobs.careers.microsoft.com/global/en/job/{j.get('atsJobId', '')}"
            if link.startswith("/"):
                link = "https://jobs.careers.microsoft.com" + link
            out.append((
                f"microsoft:{job_id}",
                title, location, link,
                None,  # no deadline field exposed
                normalize_posted(j.get("postedTs")),  # epoch seconds
            ))
    except Exception:
        pass
    return out


def fetch_rbc(search_text="intern"):
    url = "https://jobs.rbc.com/widgets"
    body = {
        "lang": "en_ca", "deviceType": "desktop", "country": "ca",
        "pageName": "search-results", "ddoKey": "refineSearch",
        "sortBy": "", "subsearch": "", "from": 0, "jobs": True,
        "counts": True, "all_fields": ["category", "country", "state", "city"],
        "size": 20, "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
        "pageId": "page10", "siteType": "external", "keywords": search_text, "global": True,
    }
    out = []
    try:
        r = requests.post(url, json=body, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return out
        jobs = r.json().get("refineSearch", {}).get("data", {}).get("jobs", [])
        for j in jobs:
            title = j.get("title", "")
            location = j.get("location") or f"{j.get('cityState', '')} {j.get('country', '')}".strip()
            job_id = j.get("jobId", title)
            link = j.get("applyUrl") or f"https://jobs.rbc.com/ca/en/search-results?keywords={title}"
            out.append((
                f"rbc:{job_id}",
                title, location, link,
                None,  # no deadline field exposed
                normalize_posted(j.get("postedDate")),
            ))
    except Exception:
        pass
    return out


# name -> fetcher, used by jobs_worker to iterate uniformly
DIRECT_SOURCES = {
    "amazon": fetch_amazon,
    "oracle": fetch_oracle,
    "microsoft": fetch_microsoft,
    "rbc": fetch_rbc,
}


if __name__ == "__main__":
    for name, fn in DIRECT_SOURCES.items():
        rows = fn()
        print(f"{name}: {len(rows)} rows"
              + (f"  e.g. {rows[0][1]!r} posted={rows[0][5]!r}" if rows else ""))
