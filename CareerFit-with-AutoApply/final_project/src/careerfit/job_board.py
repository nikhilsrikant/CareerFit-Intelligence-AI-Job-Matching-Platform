"""
job_board.py — External job-board API integration for CareerFit.

Provides:
  fetch_jsearch()          — JSearch API via RapidAPI
  fetch_adzuna()           — Adzuna Jobs API
  fetch_all_jobs()         — Aggregate both APIs with deduplication
  detect_ats()             — Infer the ATS platform from an apply URL
  convert_to_normalized_job() — Convert a raw API dict to NormalizedJob
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from careerfit.fetchers import NormalizedJob

logger = logging.getLogger(__name__)

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
# Aggregate + dedup
# ---------------------------------------------------------------------------

def fetch_all_jobs(profile_keywords: list[str], api_keys_dict: dict) -> list[dict]:
    """Fetch and deduplicate jobs from all configured APIs.

    profile_keywords — skills/roles extracted from the user's resume.
    api_keys_dict    — dict with keys: jsearch_key, adzuna_app_id, adzuna_app_key.

    Builds a search query from the first 5 keywords, appends an intern/new-grad
    suffix, then calls every configured API.
    Deduplication: first by job_id, then by normalised title+company string.
    """
    jsearch_key = (api_keys_dict or {}).get("jsearch_key") or ""
    adzuna_app_id = (api_keys_dict or {}).get("adzuna_app_id") or ""
    adzuna_app_key = (api_keys_dict or {}).get("adzuna_app_key") or ""

    keywords = (profile_keywords or [])[:5]
    if keywords:
        base_query = " ".join(keywords)
        query = f"{base_query} intern OR new grad OR entry level"
    else:
        query = "software engineer intern"

    all_jobs: list[dict] = []
    all_jobs.extend(fetch_jsearch(query, jsearch_key))
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
