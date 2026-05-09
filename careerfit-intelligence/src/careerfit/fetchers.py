from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from careerfit.location import is_us_or_remote_location


@dataclass
class NormalizedJob:
    company: str
    source: str
    external_job_id: str | None
    canonical_url: str
    title: str
    location: str | None = None
    department: str | None = None
    employment_type: str | None = None
    compensation: str | None = None
    description_text: str | None = None
    description_html: str | None = None
    raw_payload: dict | None = None

    @property
    def content_hash(self) -> str:
        body = "|".join([
            self.company or "",
            self.title or "",
            self.location or "",
            self.department or "",
            self.description_text or "",
        ])
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["content_hash"] = self.content_hash
        return payload


class FetchError(RuntimeError):
    pass


ATS_PATTERNS = {
    "workday": ["myworkdayjobs.com"],
    "greenhouse": ["boards.greenhouse.io", "job-boards.greenhouse.io", "greenhouse.io"],
    "ashby": ["jobs.ashbyhq.com", "ashbyhq.com"],
    "lever": ["jobs.lever.co", "lever.co"],
    "smartrecruiters": ["jobs.smartrecruiters.com", "smartrecruiters.com"],
    "amazon": ["amazon.jobs"],
    "microsoft": ["jobs.careers.microsoft.com", "careers.microsoft.com", "apply.careers.microsoft.com"],
}

LOCALE_SEGMENT = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$|^[a-z]{2}_[A-Z]{2}$")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _dict_bool(payload: dict, key: str, default: bool) -> bool:
    if key not in payload:
        return default
    raw = payload.get(key)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def http_timeout() -> float:
    try:
        return float(os.getenv("CAREERFIT_HTTP_TIMEOUT", "18"))
    except ValueError:
        return 18.0


def cache_ttl_seconds() -> int:
    try:
        return int(os.getenv("CAREERFIT_FETCH_CACHE_SECONDS", "21600"))
    except ValueError:
        return 21600


def cache_dir() -> Path:
    path = Path(os.getenv("CAREERFIT_CACHE_DIR", "data/cache"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def html_to_text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)


def clean_url(url: str | None) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not urlparse(url).scheme:
        url = "https://" + url
    return url


def extract_compensation(text: str) -> str | None:
    if not text:
        return None
    patterns = [
        r"(?:base salary range|salary range|compensation range)[^\n.]{0,240}",
        r"\$\s?\d{2,3},?\d{3}\s?(?:-|to|–)\s?\$?\s?\d{2,3},?\d{3}",
        r"\$\s?\d{2,3}\s?(?:-|to|–)\s?\$?\s?\d{2,3}\s?/\s?hour",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return match.group(0).strip()
    return None


def _posting_location(posting: dict[str, Any]) -> str | None:
    value = posting.get("locationsText") or posting.get("location")
    if isinstance(value, dict):
        return value.get("name") or value.get("city") or json.dumps(value)
    if value:
        return str(value)
    bullet_fields = posting.get("bulletFields") or []
    if bullet_fields:
        return " | ".join(str(x) for x in bullet_fields if x)
    return None


def _passes_location_filter(location: str | None, ats: dict[str, Any]) -> bool:
    us_only = _dict_bool(ats, "us_only", False)
    if not us_only:
        return True
    include_unknown = _dict_bool(ats, "include_unknown_locations", False)
    include_remote = _dict_bool(ats, "include_remote", True)
    return is_us_or_remote_location(location, include_remote=include_remote, include_unknown=include_unknown)


def _cache_key(company: dict) -> str:
    ats = company.get("ats") or {}
    payload = {
        "name": company.get("name"),
        "url": company.get("careers_url"),
        "ats": ats,
        "version": "studio-v3",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _jobs_to_payload(jobs: list[NormalizedJob]) -> list[dict]:
    return [j.to_dict() for j in jobs]


def _payload_to_jobs(payload: list[dict]) -> list[NormalizedJob]:
    jobs: list[NormalizedJob] = []
    for item in payload:
        item = dict(item)
        item.pop("content_hash", None)
        valid = {field.name for field in NormalizedJob.__dataclass_fields__.values()}
        jobs.append(NormalizedJob(**{k: v for k, v in item.items() if k in valid}))
    return jobs


def clear_fetch_cache() -> None:
    d = cache_dir()
    for path in d.glob("jobs_*.json"):
        try:
            path.unlink()
        except OSError:
            pass


def detect_career_source(company_name: str, careers_url: str, default_search_text: str = "intern") -> dict:
    """Infer a source configuration from a career URL.

    The detector is intentionally conservative. It covers common public job-board URLs and
    returns a generic custom connector when no ATS pattern is visible.
    """
    url = clean_url(careers_url)
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path_parts = [p for p in parsed.path.split("/") if p]
    name = company_name.strip() or (path_parts[0] if path_parts else parsed.netloc.split(".")[0].title())

    base = {
        "name": name,
        "active": True,
        "careers_url": url,
        "monitors": {"new_roles": True},
    }

    if "myworkdayjobs.com" in host:
        site_candidates = [p for p in path_parts if not LOCALE_SEGMENT.match(p)]
        site = site_candidates[-1] if site_candidates else "ExternalCareerSite"
        tenant = host.split(".")[0]
        base["ats"] = {
            "type": "workday",
            "host": parsed.netloc,
            "tenant": tenant,
            "site": site,
            "locale": next((p for p in path_parts if LOCALE_SEGMENT.match(p)), "en-US"),
            "search_text": default_search_text,
            "limit": 20,
            "max_pages": 2,
            "detail_concurrency": 12,
            "fetch_details": False,
            "us_only": True,
            "include_remote": True,
            "include_unknown_locations": False,
        }
        return base

    if "greenhouse.io" in host:
        slug = path_parts[0] if path_parts else parsed.netloc.split(".")[0]
        base["ats"] = {"type": "greenhouse", "slug": slug, "us_only": True, "include_remote": True, "include_unknown_locations": False}
        return base

    if "ashbyhq.com" in host:
        slug = path_parts[0] if path_parts else parsed.netloc.split(".")[0]
        base["ats"] = {"type": "ashby", "slug": slug, "us_only": True, "include_remote": True, "include_unknown_locations": False}
        return base

    if "lever.co" in host:
        slug = path_parts[0] if path_parts else parsed.netloc.split(".")[0]
        base["ats"] = {"type": "lever", "slug": slug, "us_only": True, "include_remote": True, "include_unknown_locations": False}
        return base

    if "smartrecruiters.com" in host:
        slug = path_parts[0] if path_parts else parsed.netloc.split(".")[0]
        base["ats"] = {"type": "smartrecruiters", "slug": slug, "us_only": True, "include_remote": True, "include_unknown_locations": False}
        return base

    if "amazon.jobs" in host:
        base["ats"] = {
            "type": "amazon",
            "search_text": default_search_text,
            "limit": 50,
            "max_pages": 3,
            "us_only": True,
            "include_remote": True,
            "include_unknown_locations": False,
        }
        return base

    if "microsoft.com" in host and ("career" in host or "career" in parsed.path.lower() or "jobs" in host or "apply" in host):
        base["ats"] = {
            "type": "microsoft",
            "search_text": default_search_text,
            "limit": 50,
            "max_pages": 3,
            "us_only": True,
            "include_remote": True,
            "include_unknown_locations": False,
        }
        return base

    base["ats"] = {"type": "auto", "us_only": True, "include_remote": True, "include_unknown_locations": False, "search_text": default_search_text}
    return base


def discover_linked_sources(company: dict, max_links: int = 80) -> list[dict]:
    """Fetch a custom careers page and discover linked ATS boards.

    This lets the user paste a company careers page without knowing whether the
    company uses Workday, Greenhouse, Ashby, Lever, SmartRecruiters, or a custom page.
    """
    url = clean_url(company.get("careers_url"))
    if not url:
        return []
    try:
        response = httpx.get(url, timeout=http_timeout(), follow_redirects=True, headers={"User-Agent": "CareerFitIntelligence/3.0"})
        response.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    links: list[str] = []
    for tag in soup.find_all(["a", "link"], href=True):
        href = urljoin(url, tag.get("href"))
        h = urlparse(href).netloc.lower()
        if any(pattern in h for patterns in ATS_PATTERNS.values() for pattern in patterns):
            links.append(href)
        if len(links) >= max_links:
            break

    configs: list[dict] = []
    seen: set[str] = set()
    default_search = (company.get("ats") or {}).get("search_text", "intern")
    for link in links:
        detected = detect_career_source(company.get("name", "Company"), link, default_search)
        ats = detected.get("ats") or {}
        if ats.get("type") == "auto":
            continue
        key = json.dumps(ats, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        # Inherit source-level filters.
        parent_ats = company.get("ats") or {}
        for filter_key in ["us_only", "include_remote", "include_unknown_locations"]:
            if filter_key in parent_ats:
                ats[filter_key] = parent_ats[filter_key]
        configs.append(detected)
    return configs


def expand_company_sources(company: dict) -> list[dict]:
    ats_type = ((company.get("ats") or {}).get("type") or "auto").lower()
    if ats_type == "auto":
        detected = detect_career_source(company.get("name", "Company"), company.get("careers_url", ""), (company.get("ats") or {}).get("search_text", "intern"))
        # If still auto/custom, discover linked ATS boards on the page.
        source_type = (detected.get("ats") or {}).get("type", "auto")
        if source_type == "auto":
            linked = discover_linked_sources(company)
            if linked:
                return linked
            detected["ats"]["type"] = "custom"
            return [detected]
        return [detected]
    return [company]


def fetch_workday(company: dict) -> list[NormalizedJob]:
    name = company["name"]
    ats = company.get("ats") or {}
    host = (ats.get("host") or urlparse(company.get("careers_url", "")).netloc).replace("https://", "").strip("/")
    tenant = ats.get("tenant")
    site = ats.get("site") or ats.get("slug")
    locale = ats.get("locale", "en-US")
    search_text = ats.get("search_text", "") or ""
    limit = min(int(ats.get("limit", 20)), 20)
    max_pages = int(ats.get("max_pages", 2))
    fetch_details = _dict_bool(ats, "fetch_details", _env_bool("CAREERFIT_FETCH_WORKDAY_DETAILS", False))
    concurrency = max(1, min(int(ats.get("detail_concurrency", os.getenv("CAREERFIT_WORKDAY_DETAIL_CONCURRENCY", "12"))), 24))

    if not host or not tenant or not site:
        raise FetchError(f"Missing Workday host, tenant, or site for {name}")

    base_url = f"https://{host}"
    api_base = f"{base_url}/wday/cxs/{tenant}/{site}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 CareerFitIntelligence/3.0",
        "Origin": base_url,
        "Referer": f"{base_url}/{locale}/{site}",
    }

    jobs: list[NormalizedJob] = []
    with httpx.Client(timeout=http_timeout(), follow_redirects=True, headers=headers) as client:
        offset = 0
        for _ in range(max_pages):
            payload = {"appliedFacets": {}, "facets": [], "limit": limit, "offset": offset, "searchText": search_text}
            response = client.post(f"{api_base}/jobs", json=payload)
            response.raise_for_status()
            data = response.json()
            postings = data.get("jobPostings") or []
            if not postings:
                break

            candidate_postings = [p for p in postings if _passes_location_filter(_posting_location(p), ats)]
            detail_by_key: dict[int, dict[str, Any]] = {}
            if fetch_details and candidate_postings:
                def fetch_one(index_and_posting: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
                    index, posting = index_and_posting
                    external_path = posting.get("externalPath")
                    if not external_path:
                        return index, {}
                    if not external_path.startswith("/"):
                        external_path = "/" + external_path
                    try:
                        detail_response = client.get(f"{api_base}{external_path}")
                        if detail_response.status_code < 400:
                            return index, detail_response.json()
                    except Exception:
                        return index, {}
                    return index, {}

                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    futures = [executor.submit(fetch_one, item) for item in enumerate(candidate_postings)]
                    for future in as_completed(futures):
                        index, detail = future.result()
                        detail_by_key[index] = detail

            for index, posting in enumerate(candidate_postings):
                detail = detail_by_key.get(index, {}) if fetch_details else {}
                job = normalize_workday_job(name, host, site, locale, posting, detail)
                if _passes_location_filter(job.location, ats):
                    jobs.append(job)

            offset += limit
            total = data.get("total") or data.get("totalResults")
            if total is not None and offset >= int(total):
                break
            if len(postings) < limit:
                break
    return jobs


def normalize_workday_job(name: str, host: str, site: str, locale: str, posting: dict, detail: dict) -> NormalizedJob:
    info = detail.get("jobPostingInfo") or detail or {}
    external_path = posting.get("externalPath") or info.get("externalPath") or ""
    if external_path and not external_path.startswith("/"):
        external_path = "/" + external_path
    canonical_url = urljoin(f"https://{host}/{locale}/{site}/", external_path.lstrip("/")) if external_path else f"https://{host}/{locale}/{site}"
    title = info.get("title") or posting.get("title") or "Untitled role"
    description_html = info.get("jobDescription") or info.get("jobDescriptionHtml") or ""
    description_text = html_to_text(description_html)
    location = info.get("location") or info.get("locationsText") or posting.get("locationsText") or _posting_location(posting)
    employment_type = info.get("timeType") or info.get("workerSubType")
    external_job_id = info.get("jobReqId") or posting.get("id")
    return NormalizedJob(
        company=name,
        source="workday",
        external_job_id=str(external_job_id) if external_job_id else None,
        canonical_url=canonical_url,
        title=title,
        location=location,
        employment_type=employment_type,
        compensation=extract_compensation(description_text),
        description_text=description_text or title,
        description_html=description_html,
        raw_payload={"posting": posting, "detail": detail},
    )


def fetch_greenhouse(company: dict) -> list[NormalizedJob]:
    name = company["name"]
    ats = company.get("ats") or {}
    slug = ats.get("slug")
    if not slug:
        raise FetchError(f"Missing Greenhouse slug for {name}")
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    response = httpx.get(url, timeout=http_timeout(), follow_redirects=True)
    response.raise_for_status()
    payload = response.json()
    jobs: list[NormalizedJob] = []
    for job in payload.get("jobs", []):
        html = job.get("content") or ""
        location = (job.get("location") or {}).get("name")
        if not _passes_location_filter(location, ats):
            continue
        text = html_to_text(html)
        jobs.append(NormalizedJob(
            company=name,
            source="greenhouse",
            external_job_id=str(job.get("id")) if job.get("id") else None,
            canonical_url=job.get("absolute_url") or company.get("careers_url"),
            title=job.get("title") or "Untitled role",
            location=location,
            department=(job.get("departments") or [{}])[0].get("name") if job.get("departments") else None,
            description_text=text,
            description_html=html,
            compensation=extract_compensation(text),
            raw_payload=job,
        ))
    return jobs


def fetch_ashby(company: dict) -> list[NormalizedJob]:
    name = company["name"]
    ats = company.get("ats") or {}
    slug = ats.get("slug")
    if not slug:
        raise FetchError(f"Missing Ashby slug for {name}")
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    response = httpx.get(url, timeout=http_timeout(), follow_redirects=True)
    response.raise_for_status()
    payload = response.json()
    jobs: list[NormalizedJob] = []
    for job in payload.get("jobs", []):
        location = job.get("location")
        if not _passes_location_filter(location, ats):
            continue
        html = job.get("descriptionHtml") or ""
        text = job.get("descriptionPlain") or html_to_text(html)
        comp = job.get("compensation")
        jobs.append(NormalizedJob(
            company=name,
            source="ashby",
            external_job_id=job.get("id"),
            canonical_url=job.get("jobUrl") or job.get("applyUrl") or company.get("careers_url"),
            title=job.get("title") or "Untitled role",
            location=location,
            department=job.get("department"),
            employment_type=job.get("employmentType"),
            compensation=json.dumps(comp) if comp else extract_compensation(text),
            description_text=text,
            description_html=html,
            raw_payload=job,
        ))
    return jobs


def fetch_lever(company: dict) -> list[NormalizedJob]:
    name = company["name"]
    ats = company.get("ats") or {}
    slug = ats.get("slug")
    if not slug:
        raise FetchError(f"Missing Lever slug for {name}")
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    response = httpx.get(url, timeout=http_timeout(), follow_redirects=True)
    response.raise_for_status()
    payload = response.json()
    jobs: list[NormalizedJob] = []
    for job in payload:
        categories = job.get("categories") or {}
        location = categories.get("location")
        if not _passes_location_filter(location, ats):
            continue
        lists = job.get("lists") or []
        html = (job.get("description") or "") + "\n" + "\n".join(
            f"<h3>{item.get('text','')}</h3>{item.get('content','')}" for item in lists
        )
        text = html_to_text(html)
        jobs.append(NormalizedJob(
            company=name,
            source="lever",
            external_job_id=job.get("id"),
            canonical_url=job.get("hostedUrl") or job.get("applyUrl") or company.get("careers_url"),
            title=job.get("text") or "Untitled role",
            location=location,
            department=categories.get("team"),
            employment_type=categories.get("commitment"),
            compensation=extract_compensation(text),
            description_text=text,
            description_html=html,
            raw_payload=job,
        ))
    return jobs


def fetch_smartrecruiters(company: dict) -> list[NormalizedJob]:
    """Best-effort SmartRecruiters public postings fetcher.

    The public endpoint shape is widely used by SmartRecruiters career pages. Some
    tenants may require API credentials or custom pages, in which case the caller
    should fall back to fetch_custom.
    """
    name = company["name"]
    ats = company.get("ats") or {}
    slug = ats.get("slug")
    if not slug:
        raise FetchError(f"Missing SmartRecruiters slug for {name}")
    limit = int(ats.get("limit", 100))
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit={limit}"
    response = httpx.get(url, timeout=http_timeout(), follow_redirects=True, headers={"User-Agent": "CareerFitIntelligence/3.0"})
    response.raise_for_status()
    payload = response.json()
    postings = payload.get("content") or payload.get("postings") or []
    jobs: list[NormalizedJob] = []
    for item in postings:
        title = item.get("name") or item.get("title") or "Untitled role"
        location_obj = item.get("location") or {}
        location = location_obj.get("fullLocation") or location_obj.get("city") or location_obj.get("country") or item.get("location")
        if isinstance(location, dict):
            location = json.dumps(location)
        if not _passes_location_filter(str(location) if location else None, ats):
            continue
        desc_html = item.get("jobAd") or item.get("description") or ""
        desc_text = html_to_text(desc_html) or title
        apply_url = item.get("ref") or item.get("url") or item.get("applyUrl") or company.get("careers_url")
        jobs.append(NormalizedJob(
            company=name,
            source="smartrecruiters",
            external_job_id=item.get("id") or item.get("uuid"),
            canonical_url=apply_url,
            title=title,
            location=str(location) if location else None,
            department=item.get("department", {}).get("label") if isinstance(item.get("department"), dict) else item.get("department"),
            employment_type=item.get("typeOfEmployment", {}).get("label") if isinstance(item.get("typeOfEmployment"), dict) else item.get("typeOfEmployment"),
            compensation=extract_compensation(desc_text),
            description_text=desc_text,
            description_html=desc_html,
            raw_payload=item,
        ))
    return jobs



def _iter_candidate_job_items(payload: Any) -> list[dict[str, Any]]:
    """Collect likely job dictionaries from a nested JSON payload.

    Vendor APIs change field names often. This best-effort extractor lets the
    Microsoft connector survive small response-shape changes without silently
    dropping every job.
    """
    out: list[dict[str, Any]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            titleish = obj.get("title") or obj.get("jobTitle") or obj.get("name")
            idish = obj.get("jobId") or obj.get("id") or obj.get("reqId") or obj.get("jobReqId")
            urlish = obj.get("url") or obj.get("jobUrl") or obj.get("applyUrl")
            if titleish and (idish or urlish or obj.get("location") or obj.get("primaryLocation")):
                out.append(obj)
            for key in ("jobs", "results", "value", "content", "items", "data"):
                if key in obj:
                    walk(obj[key])
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    # Preserve order while removing exact duplicate dict object IDs/ids.
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in out:
        key = str(item.get("jobId") or item.get("id") or item.get("url") or item.get("title") or item.get("name"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _stringify_location(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces: list[str] = []
        for item in value:
            s = _stringify_location(item)
            if s:
                pieces.append(s)
        return "; ".join(pieces) if pieces else None
    if isinstance(value, dict):
        for key in ["fullLocation", "displayName", "name", "city", "state", "country", "primaryLocation"]:
            if value.get(key):
                return str(value[key])
        return ", ".join(str(v) for v in value.values() if isinstance(v, (str, int, float))) or None
    return str(value)


def fetch_amazon(company: dict) -> list[NormalizedJob]:
    """Fetch Amazon jobs through the public amazon.jobs search JSON endpoint.

    If the endpoint shape changes, CareerFit falls back to HTML crawling through
    fetch_custom rather than failing the whole scan.
    """
    name = company["name"]
    ats = company.get("ats") or {}
    search_text = ats.get("search_text", "") or ""
    limit = max(10, min(int(ats.get("limit", 50)), 100))
    max_pages = max(1, int(ats.get("max_pages", 3)))
    jobs: list[NormalizedJob] = []
    headers = {"User-Agent": "Mozilla/5.0 CareerFitIntelligence/3.0", "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"}

    with httpx.Client(timeout=http_timeout(), follow_redirects=True, headers=headers) as client:
        for page in range(max_pages):
            offset = page * limit
            params = {
                "base_query": search_text,
                "offset": offset,
                "result_limit": limit,
                "sort": "relevant",
            }
            if _dict_bool(ats, "us_only", False):
                params["loc_query"] = "United States"
            try:
                response = client.get("https://www.amazon.jobs/en/search.json", params=params)
                response.raise_for_status()
                payload = response.json()
            except Exception:
                if not jobs:
                    return fetch_custom(company)
                break

            items = payload.get("jobs") or payload.get("results") or []
            if not items:
                break
            for item in items:
                title = item.get("title") or item.get("job_title") or item.get("name") or "Untitled role"
                loc_parts = [item.get("city"), item.get("state"), item.get("country_code") or item.get("country")]
                location = item.get("normalized_location") or ", ".join(str(x) for x in loc_parts if x)
                if not _passes_location_filter(location, ats):
                    continue
                path = item.get("job_path") or item.get("url_next_step") or item.get("url") or ""
                if path and path.startswith("/"):
                    canonical = "https://www.amazon.jobs" + path
                elif path:
                    canonical = path
                else:
                    canonical = company.get("careers_url") or "https://www.amazon.jobs/en/"
                desc = item.get("description_short") or item.get("description") or title
                jobs.append(NormalizedJob(
                    company=name,
                    source="amazon",
                    external_job_id=str(item.get("id") or item.get("job_id") or item.get("id_icims") or "") or None,
                    canonical_url=canonical,
                    title=title,
                    location=location,
                    department=item.get("business_category") or item.get("team"),
                    employment_type=item.get("job_schedule_type") or item.get("employment_type"),
                    compensation=extract_compensation(str(desc)),
                    description_text=html_to_text(str(desc)) or title,
                    raw_payload=item,
                ))
            if len(items) < limit:
                break
    return dedupe_jobs(jobs)


def fetch_microsoft(company: dict) -> list[NormalizedJob]:
    """Best-effort Microsoft Careers connector.

    Microsoft has used several public search surfaces over time. This connector
    first tries the JSON search service and then falls back to parsing public
    search-page/job links if the JSON shape is unavailable.
    """
    name = company["name"]
    ats = company.get("ats") or {}
    search_text = ats.get("search_text", "") or ""
    limit = max(10, min(int(ats.get("limit", 50)), 100))
    max_pages = max(1, int(ats.get("max_pages", 3)))
    jobs: list[NormalizedJob] = []
    headers = {"User-Agent": "Mozilla/5.0 CareerFitIntelligence/3.0", "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"}

    with httpx.Client(timeout=http_timeout(), follow_redirects=True, headers=headers) as client:
        # JSON service used by the modern Microsoft careers experience. Keep the
        # parser flexible because exact nested fields can change.
        for page in range(1, max_pages + 1):
            params = {
                "l": "en_us",
                "pg": page,
                "pgSz": limit,
                "o": "Relevance",
                "flt": "true",
            }
            if search_text:
                params["q"] = search_text
            if _dict_bool(ats, "us_only", False):
                params["lc"] = "United States"
            try:
                response = client.get("https://gcsservices.careers.microsoft.com/search/api/v1/search", params=params)
                response.raise_for_status()
                payload = response.json()
                items = _iter_candidate_job_items(payload)
            except Exception:
                items = []

            if not items:
                break
            for item in items:
                title = item.get("title") or item.get("jobTitle") or item.get("name") or "Untitled role"
                job_id = item.get("jobId") or item.get("id") or item.get("reqId")
                location = _stringify_location(item.get("primaryLocation") or item.get("locations") or item.get("location"))
                if not _passes_location_filter(location, ats):
                    continue
                url = item.get("url") or item.get("jobUrl") or item.get("applyUrl")
                if not url and job_id:
                    url = f"https://jobs.careers.microsoft.com/global/en/job/{job_id}"
                desc = item.get("description") or item.get("shortDescription") or item.get("summary") or title
                jobs.append(NormalizedJob(
                    company=name,
                    source="microsoft",
                    external_job_id=str(job_id) if job_id else None,
                    canonical_url=url or company.get("careers_url") or "https://jobs.careers.microsoft.com/",
                    title=title,
                    location=location,
                    department=item.get("profession") or item.get("discipline") or item.get("jobFamily"),
                    employment_type=item.get("employmentType") or item.get("roleType"),
                    compensation=extract_compensation(str(desc)),
                    description_text=html_to_text(str(desc)) or title,
                    raw_payload=item,
                ))
            if len(items) < limit:
                break

    if jobs:
        return dedupe_jobs(jobs)

    # HTML fallback for public Microsoft pages. This is less detailed but ensures
    # the source can still surface links when the JSON service is unavailable.
    search_url = "https://jobs.careers.microsoft.com/global/en/search"
    try:
        response = httpx.get(search_url, timeout=http_timeout(), follow_redirects=True, headers=headers, params={"q": search_text} if search_text else None)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        seen: set[str] = set()
        for link in soup.find_all("a", href=True):
            href = urljoin(search_url, link.get("href"))
            text = link.get_text(" ", strip=True)
            if "/job/" not in href.lower() and "job_id=" not in href.lower():
                continue
            if href in seen:
                continue
            seen.add(href)
            if search_text and search_text.lower() not in f"{text} {href}".lower():
                # Keep public page fallback focused to avoid loading navigation links.
                continue
            jobs.append(NormalizedJob(
                company=name,
                source="microsoft-links",
                external_job_id=None,
                canonical_url=href,
                title=text[:160] or "Microsoft career role",
                location="United States" if _dict_bool(ats, "us_only", False) else None,
                description_text=text or href,
                raw_payload={"href": href, "text": text},
            ))
    except Exception:
        pass

    return dedupe_jobs([j for j in jobs if _passes_location_filter(j.location, ats)])

def fetch_custom(company: dict, max_links: int = 80) -> list[NormalizedJob]:
    name = company["name"]
    ats = company.get("ats") or {}
    url = clean_url(company.get("careers_url"))
    if not url:
        raise FetchError(f"Missing careers_url for {name}")
    response = httpx.get(url, timeout=http_timeout(), follow_redirects=True, headers={"User-Agent": "CareerFitIntelligence/3.0"})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    jobs: list[NormalizedJob] = []

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = json.loads(script.string or "{}")
        except Exception:
            continue
        items = raw if isinstance(raw, list) else [raw]
        # Handle @graph containers too.
        expanded: list[Any] = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                expanded.extend(item["@graph"])
            else:
                expanded.append(item)
        for item in expanded:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                title = item.get("title") or "Untitled role"
                desc = html_to_text(item.get("description") or "")
                apply_url = item.get("url") or item.get("sameAs") or url
                loc = str(item.get("jobLocation") or "")
                if not _passes_location_filter(loc, ats):
                    continue
                jobs.append(NormalizedJob(
                    company=name,
                    source="custom-jsonld",
                    external_job_id=item.get("identifier") if isinstance(item.get("identifier"), str) else None,
                    canonical_url=apply_url,
                    title=title,
                    location=loc,
                    employment_type=item.get("employmentType"),
                    compensation=extract_compensation(desc),
                    description_text=desc or title,
                    raw_payload=item,
                ))

    if not jobs:
        seen: set[str] = set()
        link_keywords = [
            "job", "career", "opening", "position", "role", "intern", "graduate", "university",
            "engineer", "scientist", "analyst", "designer", "manager", "specialist", "technician",
        ]
        for link in soup.find_all("a", href=True):
            text = link.get_text(" ", strip=True)
            href = urljoin(url, link["href"])
            lowered = f"{text} {href}".lower()
            if not any(word in lowered for word in link_keywords):
                continue
            if href in seen or len(seen) >= max_links:
                continue
            seen.add(href)
            jobs.append(NormalizedJob(
                company=name,
                source="custom-links",
                external_job_id=None,
                canonical_url=href,
                title=text[:160] or "Career page link",
                location=None,
                description_text=text,
                raw_payload={"href": href, "text": text},
            ))
    return jobs


def _fetch_no_cache(company: dict) -> list[NormalizedJob]:
    all_jobs: list[NormalizedJob] = []
    sources = expand_company_sources(company)
    errors: list[str] = []
    for source in sources:
        ats_type = ((source.get("ats") or {}).get("type") or "custom").lower()
        try:
            if ats_type == "workday":
                jobs = fetch_workday(source)
            elif ats_type == "greenhouse":
                jobs = fetch_greenhouse(source)
            elif ats_type == "ashby":
                jobs = fetch_ashby(source)
            elif ats_type == "lever":
                jobs = fetch_lever(source)
            elif ats_type == "smartrecruiters":
                try:
                    jobs = fetch_smartrecruiters(source)
                except Exception:
                    jobs = fetch_custom(source)
            elif ats_type == "amazon":
                jobs = fetch_amazon(source)
            elif ats_type == "microsoft":
                jobs = fetch_microsoft(source)
            else:
                jobs = fetch_custom(source)
            all_jobs.extend(jobs)
        except Exception as exc:
            errors.append(f"{source.get('name', 'source')}:{ats_type}: {exc}")
    if not all_jobs and errors:
        raise FetchError("; ".join(errors[:4]))
    return dedupe_jobs(all_jobs)


def fetch_jobs_for_company(company: dict, use_cache: bool | None = None) -> list[NormalizedJob]:
    if use_cache is None:
        use_cache = _env_bool("CAREERFIT_USE_FETCH_CACHE", True)
    key = _cache_key(company)
    cache_path = cache_dir() / f"jobs_{key}.json"
    ttl = cache_ttl_seconds()
    if use_cache and cache_path.exists() and time.time() - cache_path.stat().st_mtime < ttl:
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            return _payload_to_jobs(payload.get("jobs", []))
        except Exception:
            pass

    jobs = _fetch_no_cache(company)
    if use_cache:
        cache_path.write_text(json.dumps({"created_at": time.time(), "jobs": _jobs_to_payload(jobs)}, ensure_ascii=False), encoding="utf-8")
    return jobs


def dedupe_jobs(jobs: list[NormalizedJob]) -> list[NormalizedJob]:
    seen: set[str] = set()
    deduped: list[NormalizedJob] = []
    for job in jobs:
        key = (job.canonical_url or "").lower().strip() or f"{job.company}|{job.title}|{job.location}".lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(job)
    return deduped


def load_companies(config_path: str = "config/companies.yaml") -> list[dict]:
    import yaml
    with open(config_path, "r", encoding="utf-8") as f:
        payload = yaml.safe_load(f) or {}
    return [c for c in payload.get("companies", []) if c.get("active", True)]
