"""
job_board.py — External job-board API integration for CareerFit.

Provides:
  fetch_jsearch()             — JSearch API via RapidAPI (requires key)
  fetch_adzuna()              — Adzuna Jobs API (requires key)
  fetch_greenhouse_public()   — Greenhouse public board API (zero key / zero signup)
  fetch_lever_public()        — Lever public board API (zero key / zero signup)
  fetch_workday_public()      — Workday public job search (zero key / zero signup)
  fetch_all_jobs()            — Aggregate all APIs with deduplication
  detect_ats()                — Infer the ATS platform from an apply URL
  convert_to_normalized_job() — Convert a raw API dict to NormalizedJob
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from careerfit.fetchers import NormalizedJob

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Free public boards — zero key, zero signup
# ---------------------------------------------------------------------------

GREENHOUSE_SLUGS = [
    "stripe", "databricks", "airbnb", "figma", "reddit", "twilio", "coinbase",
    "robinhood", "doordash_campus", "brex", "benchling", "scale", "verkada",
    "samsara", "plaid", "squareup", "dropbox", "gitlab", "hashicorp", "cloudflare",
    "pagerduty", "zendesk", "mongodb", "elastic", "confluent", "datadog",
    "okta", "box", "pandadoc", "lattice", "gusto", "mixpanel", "amplitude",
    "duolingo", "quora", "lyft_campus", "asana", "klaviyo", "hubspot",
    "intercom", "segment", "mimecast", "snyk", "checkr", "greenhouse_io",
    "affirm", "chime", "brainly", "ro", "nerdwallet",
]

LEVER_SLUGS = [
    "lyft", "waymo", "doordash", "coinbase", "figma", "notion", "airtable",
    "vercel", "supabase", "retool", "linear", "mercury", "brex", "ramp",
    "watershed", "scale-ai", "anyscale", "weights-biases", "huggingface",
    "cohere", "anthropic", "mistral", "replicate", "modal", "together-ai",
]

WORKDAY_TENANTS = [
    {"tenant": "nvidia", "board": "NVIDIAExternalCareerSite", "subdomain": "nvidia.wd5"},
    {"tenant": "qualcomm", "board": "External", "subdomain": "qualcomm.wd5"},
    {"tenant": "micron", "board": "External", "subdomain": "micron.wd1"},
    {"tenant": "amd", "board": "AMD", "subdomain": "amd.wd1"},
    {"tenant": "intel", "board": "External", "subdomain": "intel.wd1"},
    {"tenant": "applied", "board": "External", "subdomain": "applied.wd1"},
    {"tenant": "cisco", "board": "CiscoJobs", "subdomain": "cisco.wd5"},
    {"tenant": "paypal", "board": "jobs", "subdomain": "paypal.wd1"},
    {"tenant": "salesforce", "board": "External", "subdomain": "salesforce.wd12"},
    {"tenant": "adobe", "board": "external", "subdomain": "adobe.wd5"},
]

# ---------------------------------------------------------------------------
# JSearch (RapidAPI)
# ---------------------------------------------------------------------------

JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"


def fetch_jsearch(query: str, api_key: str, num_pages: int = 3) -> list[dict]:
    """Fetch jobs from the JSearch API (via RapidAPI).

    Returns a list of normalised job dicts.
    Returns [] immediately if api_key is empty or None.
    """
    if not api_key:
        return []

    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }

    jobs: list[dict] = []
    try:
        with httpx.Client(timeout=15) as client:
            for page in range(1, num_pages + 1):
                params: dict[str, Any] = {
                    "query": query,
                    "page": page,
                    "num_pages": 1,
                    "date_posted": "month",
                    "employment_types": "INTERN,FULLTIME",
                    "country": "us",
                }
                try:
                    response = client.get(JSEARCH_URL, headers=headers, params=params)
                    response.raise_for_status()
                    data = response.json()
                except Exception as exc:
                    logger.warning("fetch_jsearch page %d error: %s", page, exc)
                    break

                raw_jobs = data.get("data") or []
                for item in raw_jobs:
                    jobs.append({
                        "title": item.get("job_title") or "",
                        "company": item.get("employer_name") or "",
                        "location": _jsearch_location(item),
                        "description": item.get("job_description") or "",
                        "apply_url": item.get("job_apply_link") or "",
                        "job_id": item.get("job_id") or "",
                        "source": "jsearch",
                        "employment_type": item.get("job_employment_type") or "",
                        "date_posted": item.get("job_posted_at_datetime_utc") or "",
                    })
    except Exception as exc:
        logger.exception("fetch_jsearch unexpected error: %s", exc)

    return jobs


def _jsearch_location(item: dict) -> str:
    city = item.get("job_city") or ""
    state = item.get("job_state") or ""
    parts = [p for p in [city, state] if p]
    return ", ".join(parts) if parts else item.get("job_country") or ""


# ---------------------------------------------------------------------------
# Adzuna
# ---------------------------------------------------------------------------

ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/us/search/1"


def fetch_adzuna(query: str, app_id: str, app_key: str) -> list[dict]:
    """Fetch jobs from the Adzuna Jobs API.

    Returns a list of normalised job dicts.
    Returns [] immediately if app_id or app_key is empty or None.
    """
    if not app_id or not app_key:
        return []

    params: dict[str, Any] = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": 50,
        "what": query,
        "where": "usa",
        "what_and": 'intern OR "new grad" OR "entry level"',
        "max_days_old": 30,
    }

    jobs: list[dict] = []
    try:
        with httpx.Client(timeout=15) as client:
            try:
                response = client.get(ADZUNA_URL, params=params)
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logger.warning("fetch_adzuna error: %s", exc)
                return jobs

            results = data.get("results") or []
            for item in results:
                company_obj = item.get("company") or {}
                location_obj = item.get("location") or {}
                jobs.append({
                    "title": item.get("title") or "",
                    "company": company_obj.get("display_name") or "",
                    "location": location_obj.get("display_name") or "",
                    "description": item.get("description") or "",
                    "apply_url": item.get("redirect_url") or "",
                    "job_id": str(item.get("id") or ""),
                    "source": "adzuna",
                    "employment_type": item.get("contract_type") or "",
                    "date_posted": item.get("created") or "",
                })
    except Exception as exc:
        logger.exception("fetch_adzuna unexpected error: %s", exc)

    return jobs


# ---------------------------------------------------------------------------
# Greenhouse public board (zero key)
# ---------------------------------------------------------------------------

def _fetch_one_greenhouse(slug: str, keyword: str, client: httpx.Client) -> list[dict]:
    """Fetch jobs from one Greenhouse company board. Returns [] on any error."""
    try:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        resp = client.get(url, timeout=10)
        if resp.status_code != 200:
            return []
        data = resp.json()
        jobs = []
        for item in (data.get("jobs") or []):
            title = item.get("title") or ""
            if keyword and keyword.lower() not in title.lower():
                continue
            location_obj = item.get("location") or {}
            jobs.append({
                "title": title,
                "company": slug.replace("_", " ").title(),
                "location": location_obj.get("name") or "",
                "description": "",
                "apply_url": item.get("absolute_url") or "",
                "job_id": str(item.get("id") or ""),
                "source": "greenhouse_api",
                "employment_type": "",
                "date_posted": item.get("updated_at") or "",
            })
        return jobs
    except Exception as exc:
        logger.debug("greenhouse slug=%s error: %s", slug, exc)
        return []


def fetch_greenhouse_public(keyword: str) -> list[dict]:
    """Fetch jobs from 50+ Greenhouse company boards in parallel. Zero API key needed."""
    all_jobs: list[dict] = []
    with httpx.Client(timeout=12) as client:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                pool.submit(_fetch_one_greenhouse, slug, keyword, client): slug
                for slug in GREENHOUSE_SLUGS
            }
            for future in as_completed(futures):
                try:
                    all_jobs.extend(future.result())
                except Exception as exc:
                    logger.debug("greenhouse future error: %s", exc)
    return all_jobs


# ---------------------------------------------------------------------------
# Lever public board (zero key)
# ---------------------------------------------------------------------------

def _fetch_one_lever(slug: str, keyword: str, client: httpx.Client) -> list[dict]:
    """Fetch jobs from one Lever company board. Returns [] on any error."""
    try:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        resp = client.get(url, timeout=10)
        if resp.status_code != 200:
            return []
        items = resp.json()
        if not isinstance(items, list):
            return []
        jobs = []
        for item in items:
            title = item.get("text") or ""
            if keyword and keyword.lower() not in title.lower():
                continue
            cats = item.get("categories") or {}
            jobs.append({
                "title": title,
                "company": slug.replace("-", " ").title(),
                "location": cats.get("location") or "",
                "description": "",
                "apply_url": item.get("hostedUrl") or "",
                "job_id": item.get("id") or "",
                "source": "lever_api",
                "employment_type": cats.get("commitment") or "",
                "date_posted": str(item.get("createdAt") or ""),
            })
        return jobs
    except Exception as exc:
        logger.debug("lever slug=%s error: %s", slug, exc)
        return []


def fetch_lever_public(keyword: str) -> list[dict]:
    """Fetch jobs from 25+ Lever company boards in parallel. Zero API key needed."""
    all_jobs: list[dict] = []
    with httpx.Client(timeout=12) as client:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                pool.submit(_fetch_one_lever, slug, keyword, client): slug
                for slug in LEVER_SLUGS
            }
            for future in as_completed(futures):
                try:
                    all_jobs.extend(future.result())
                except Exception as exc:
                    logger.debug("lever future error: %s", exc)
    return all_jobs


# ---------------------------------------------------------------------------
# Workday public search (zero key)
# ---------------------------------------------------------------------------

def _fetch_one_workday(entry: dict, keyword: str, client: httpx.Client) -> list[dict]:
    """POST to one Workday tenant's public job search endpoint. Returns [] on any error."""
    tenant = entry["tenant"]
    board = entry["board"]
    subdomain = entry["subdomain"]
    try:
        url = f"https://{tenant}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
        payload = {
            "appliedFacets": {},
            "limit": 20,
            "offset": 0,
            "searchText": keyword or "software engineer",
        }
        headers = {"Content-Type": "application/json"}
        resp = client.post(url, json=payload, headers=headers, timeout=12)
        if resp.status_code != 200:
            return []
        data = resp.json()
        jobs = []
        for item in (data.get("jobPostings") or []):
            title = item.get("title") or ""
            ext_path = item.get("externalPath") or ""
            apply_url = f"https://{subdomain}.myworkdayjobs.com{ext_path}" if ext_path else ""
            jobs.append({
                "title": title,
                "company": tenant.title(),
                "location": item.get("locationsText") or "",
                "description": " ".join(item.get("bulletFields") or []),
                "apply_url": apply_url,
                "job_id": ext_path.split("/")[-1] if ext_path else "",
                "source": "workday_api",
                "employment_type": "",
                "date_posted": item.get("postedOn") or "",
            })
        return jobs
    except Exception as exc:
        logger.debug("workday tenant=%s error: %s", tenant, exc)
        return []


def fetch_workday_public(keyword: str) -> list[dict]:
    """Search jobs across 10 major Workday tenants in parallel. Zero API key needed."""
    all_jobs: list[dict] = []
    with httpx.Client(timeout=14) as client:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                pool.submit(_fetch_one_workday, entry, keyword, client): entry["tenant"]
                for entry in WORKDAY_TENANTS
            }
            for future in as_completed(futures):
                try:
                    all_jobs.extend(future.result())
                except Exception as exc:
                    logger.debug("workday future error: %s", exc)
    return all_jobs


# ---------------------------------------------------------------------------
# Aggregate + dedup
# ---------------------------------------------------------------------------

def fetch_all_jobs(profile_keywords: list[str], api_keys_dict: dict | None = None) -> list[dict]:
    """Fetch and deduplicate jobs from all configured APIs.

    Always fetches from the free public boards (Greenhouse, Lever, Workday) — no
    API key needed, works out of the box.  Optionally also calls JSearch and Adzuna
    if api_keys_dict contains the respective keys.

    profile_keywords — skills/roles extracted from the user's resume.
    api_keys_dict    — optional dict with keys: jsearch_key, adzuna_app_id, adzuna_app_key.

    Deduplication: first by job_id, then by normalised title+company string.
    """
    api_keys_dict = api_keys_dict or {}
    jsearch_key = api_keys_dict.get("jsearch_key") or ""
    adzuna_app_id = api_keys_dict.get("adzuna_app_id") or ""
    adzuna_app_key = api_keys_dict.get("adzuna_app_key") or ""

    keywords = (profile_keywords or [])[:5]
    if keywords:
        base_query = " ".join(keywords)
        query = f"{base_query} intern OR new grad OR entry level"
        # Simpler keyword for board-level filtering (first skill term)
        board_keyword = keywords[0] if keywords else ""
    else:
        query = "software engineer intern"
        board_keyword = "software engineer"

    all_jobs: list[dict] = []

    # Always fetch from free public boards (zero key needed)
    try:
        all_jobs.extend(fetch_greenhouse_public(board_keyword))
    except Exception as exc:
        logger.warning("fetch_greenhouse_public error: %s", exc)
    try:
        all_jobs.extend(fetch_lever_public(board_keyword))
    except Exception as exc:
        logger.warning("fetch_lever_public error: %s", exc)
    try:
        all_jobs.extend(fetch_workday_public(board_keyword))
    except Exception as exc:
        logger.warning("fetch_workday_public error: %s", exc)

    # Optionally augment with paid API sources if keys are configured
    if jsearch_key:
        all_jobs.extend(fetch_jsearch(query, jsearch_key))
    if adzuna_app_id and adzuna_app_key:
        all_jobs.extend(fetch_adzuna(query, adzuna_app_id, adzuna_app_key))

    # Dedup pass 1: by job_id
    seen_ids: set[str] = set()
    pass1: list[dict] = []
    for job in all_jobs:
        jid = (job.get("job_id") or "").strip()
        if jid and jid in seen_ids:
            continue
        if jid:
            seen_ids.add(jid)
        pass1.append(job)

    # Dedup pass 2: by normalised title+company
    seen_keys: set[str] = set()
    result: list[dict] = []
    for job in pass1:
        norm_key = (
            (job.get("title") or "").lower().strip()
            + "|"
            + (job.get("company") or "").lower().strip()
        )
        if norm_key in seen_keys:
            continue
        seen_keys.add(norm_key)
        result.append(job)

    return result


# ---------------------------------------------------------------------------
# ATS detection
# ---------------------------------------------------------------------------

def detect_ats(url: str) -> str:
    """Infer the ATS platform name from an apply URL.

    Returns one of: 'workday', 'greenhouse', 'ashby', 'lever',
    'smartrecruiters', or 'generic'.
    """
    u = (url or "").lower()
    if "myworkdayjobs.com" in u or "wd1.myworkdayjobs" in u:
        return "workday"
    if "greenhouse.io" in u:
        return "greenhouse"
    if "ashbyhq.com" in u:
        return "ashby"
    if "lever.co" in u:
        return "lever"
    if "smartrecruiters.com" in u:
        return "smartrecruiters"
    return "generic"


# ---------------------------------------------------------------------------
# Conversion helper
# ---------------------------------------------------------------------------

def convert_to_normalized_job(raw: dict) -> NormalizedJob:
    """Convert a raw API job dict (from fetch_jsearch / fetch_adzuna) to NormalizedJob."""
    return NormalizedJob(
        company=raw.get("company") or "",
        source=raw.get("source") or "api",
        external_job_id=raw.get("job_id") or raw.get("apply_url") or "",
        canonical_url=raw.get("apply_url") or "",
        title=raw.get("title") or "",
        location=raw.get("location") or None,
        employment_type=raw.get("employment_type") or None,
        description_text=raw.get("description") or None,
        raw_payload=raw,
    )
