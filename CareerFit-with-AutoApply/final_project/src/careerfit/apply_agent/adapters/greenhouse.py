"""
Greenhouse Adapter
──────────────────
Greenhouse boards expose a clean JSON API at:
  boards.greenhouse.io/embed/job_app?for={slug}&token={job_id}

Speed strategy:
  1. Extract slug + job_id from the canonical URL.
  2. Fetch the JSON schema to know field names upfront (no DOM scanning needed).
  3. Navigate to the actual application page.
  4. JS-inject all values in one pass.
  5. Upload resume via file input.
  6. Handle multi-step (Confirm → Submit).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import httpx
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from careerfit.apply_agent.adapters.base import BaseAdapter, SELECTOR_TIMEOUT
from careerfit.apply_agent.core.field_mapper import FieldMapper

logger = logging.getLogger(__name__)

# Regex to pull the job_id from a Greenhouse URL
# e.g. boards.greenhouse.io/companyx/jobs/12345678
_JOB_ID_RE = re.compile(r"/jobs?/(\d+)", re.I)
# Slug: boards.greenhouse.io/{slug}/jobs/...
_SLUG_RE = re.compile(r"greenhouse\.io/([^/]+)/", re.I)


class GreenhouseAdapter(BaseAdapter):
    ats_name = "greenhouse"

    async def apply(self, page: Page, mapper: FieldMapper, job: dict) -> dict:
        url = job.get("canonical_url", "")
        company = job.get("company", "")
        title = job.get("title", "")

        # ── 1. Parse slug + job_id ──────────────────────────────────────────
        slug_m = _SLUG_RE.search(url)
        job_id_m = _JOB_ID_RE.search(url)
        if not slug_m or not job_id_m:
            return {"status": "skipped", "reason": "Cannot parse Greenhouse slug/job_id from URL"}

        slug = slug_m.group(1)
        job_id = job_id_m.group(1)

        # ── 2. Fetch JSON schema (fast, no browser) ─────────────────────────
        schema = await _fetch_gh_schema(slug, job_id)

        # ── 3. Navigate to application page ────────────────────────────────
        apply_url = f"https://boards.greenhouse.io/{slug}/jobs/{job_id}"
        try:
            await page.goto(apply_url, wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeout:
            return {"status": "failed", "reason": "Page load timeout"}

        # Wait for the form to appear
        try:
            await page.wait_for_selector("#application_form, form[id*='application']",
                                         timeout=SELECTOR_TIMEOUT)
        except PlaywrightTimeout:
            return {"status": "failed", "reason": "Application form not found"}

        # ── 4. Fill standard fields ─────────────────────────────────────────
        fills = [
            ("#first_name",                mapper.resolve("first_name")),
            ("#last_name",                 mapper.resolve("last_name")),
            ("#email",                     mapper.resolve("email")),
            ("#phone",                     mapper.resolve("phone")),
            ("#job_application_linkedin_profile_url, [name*='linkedin']", mapper.resolve("linkedin")),
            ("#job_application_website, [name*='website']",               mapper.resolve("website")),
            ("#job_application_github,  [name*='github']",                mapper.resolve("github")),
        ]

        for selector, value in fills:
            if value:
                # Try each sub-selector (comma-separated)
                for sel in [s.strip() for s in selector.split(",")]:
                    if await self.fast_fill(page, sel, str(value)):
                        break

        # ── 5. Resume upload ────────────────────────────────────────────────
        resume_path = mapper.resolve("resume", "file")
        if resume_path:
            # Greenhouse uses a hidden <input type="file"> revealed by a styled button
            await self.upload_file(page,
                                   "input[type='file'][name*='resume'], input[id*='resume']",
                                   str(resume_path))

        # ── 6. Custom questions from schema ────────────────────────────────
        if schema:
            await self._fill_custom_questions(page, schema, mapper, title, company)

        # ── 7. Cover letter / free-text ─────────────────────────────────────
        cl_sel = "textarea[name*='cover_letter'], #cover_letter"
        cl_text = mapper.resolve("cover_letter") or mapper.cover_letter_for_job(title, company)
        if cl_text:
            await self.fast_fill(page, cl_sel, cl_text)

        # ── 8. EEO / demographic fields ────────────────────────────────────
        await self._fill_eeo(page, mapper)

        # ── 9. Submit ──────────────────────────────────────────────────────
        dry_run = mapper._cfg.get("dry_run", True)
        if dry_run:
            logger.info(f"[greenhouse] DRY RUN — not submitting {title} @ {company}")
            return {"status": "success", "reason": "dry_run"}

        submitted = await self.click_next(page, ["Submit Application", "Submit", "Apply"])
        if not submitted:
            return {"status": "failed", "reason": "Submit button not found"}

        # Confirm success
        try:
            await page.wait_for_selector(
                ".confirmation, [class*='thank'], [class*='success'], h1:has-text('Thank')",
                timeout=10_000,
            )
            return {"status": "success", "reason": "submitted"}
        except PlaywrightTimeout:
            return {"status": "success", "reason": "submitted (no confirmation detected)"}

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def _fill_custom_questions(self, page: Page, schema: dict,
                                     mapper: FieldMapper, title: str, company: str):
        questions = schema.get("questions", [])
        for q in questions:
            label = q.get("label", "")
            q_id = q.get("id")
            if not label or not q_id:
                continue
            fields = q.get("fields", [])
            for f in fields:
                ftype = f.get("type", "input_text")
                fname = f.get("name", "")
                sel = f"[name='{fname}']" if fname else f"[data-question-id='{q_id}']"
                value = mapper.resolve(label)
                if not value and "cover letter" in label.lower():
                    value = mapper.cover_letter_for_job(title, company)
                if value:
                    if ftype in ("input_text", "textarea"):
                        await self.fast_fill(page, sel, str(value))
                    elif ftype == "select":
                        label_lower = label.lower()
                        # Use smart dropdown resolution for education-related selects
                        if any(kw in label_lower for kw in (
                            "degree", "major", "field of study", "education level",
                        )):
                            await self.fill_select_field(page, sel, label, mapper)
                        else:
                            await self.select_option(page, sel, str(value))

    async def _fill_eeo(self, page: Page, mapper: FieldMapper):
        eeo_map = {
            "#job_application_gender":        mapper.resolve("gender"),
            "#job_application_race":          mapper.resolve("ethnicity"),
            "#job_application_veteran_status": mapper.resolve("veteran_status"),
            "#job_application_disability_status": mapper.resolve("disability"),
        }
        for sel, val in eeo_map.items():
            if val:
                await self.select_option(page, sel, str(val))


async def _fetch_gh_schema(slug: str, job_id: str) -> Optional[dict]:
    """Fetch Greenhouse's job schema JSON for field discovery."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?questions=true"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers={"User-Agent": "CareerFitAgent/1.0"})
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.debug(f"[greenhouse] Schema fetch failed: {e}")
    return None
