"""
Ashby Adapter
─────────────
Ashby (app.ashbyhq.com / jobs.ashbyhq.com) uses a React SPA with clean
data-testid hooks. Applications are usually 1-2 pages.

Key hooks:
  [data-testid="ApplicationFormField-first-name"]
  [data-testid="ApplicationFormField-last-name"]
  [data-testid="ApplicationFormField-email"]
  [data-testid="ApplicationFormField-phone"]
  [data-testid="ApplicationFormField-linkedin"]
  [data-testid="ApplicationFormField-github"]
  [data-testid="ApplicationFormField-portfolio"]
  [data-testid="FileInput-resume"]   (file upload)

Note: fill_select_field() (from BaseAdapter) is available for any education-related
dropdown (degree, major, field of study, education level). It fetches live options
and uses the smart dropdown fallback engine (dropdown_matcher.py) to pick the best
match — no manual option text required.
"""
from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from careerfit.apply_agent.adapters.base import BaseAdapter, SELECTOR_TIMEOUT
from careerfit.apply_agent.core.field_mapper import FieldMapper

logger = logging.getLogger(__name__)

# Keywords that indicate an education-related select/dropdown
_EDU_KEYWORDS = ("degree", "major", "field of study", "education level", "highest education")


class AshbyAdapter(BaseAdapter):
    ats_name = "ashby"

    async def apply(self, page: Page, mapper: FieldMapper, job: dict) -> dict:
        url = job.get("canonical_url", "")
        company = job.get("company", "")
        title = job.get("title", "")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeout:
            return {"status": "failed", "reason": "Page load timeout"}

        try:
            await page.wait_for_selector("[data-testid*='ApplicationForm'], form",
                                         timeout=SELECTOR_TIMEOUT)
        except PlaywrightTimeout:
            return {"status": "failed", "reason": "Application form not found"}

        # Standard fields via data-testid
        fields = {
            "[data-testid*='first-name'] input, [data-testid*='firstName'] input":  mapper.resolve("first_name"),
            "[data-testid*='last-name'] input,  [data-testid*='lastName'] input":   mapper.resolve("last_name"),
            "[data-testid*='email'] input":       mapper.resolve("email"),
            "[data-testid*='phone'] input":       mapper.resolve("phone"),
            "[data-testid*='linkedin'] input":    mapper.resolve("linkedin"),
            "[data-testid*='github'] input":      mapper.resolve("github"),
            "[data-testid*='portfolio'] input, [data-testid*='website'] input": mapper.resolve("website"),
        }
        for sel, value in fields.items():
            if value:
                for s in [x.strip() for x in sel.split(",")]:
                    if await self.fast_fill(page, s, str(value)):
                        break

        # Resume
        resume = mapper.resolve("resume", "file")
        if resume:
            await self.upload_file(
                page,
                "[data-testid*='resume'] input[type='file'], input[type='file']",
                str(resume),
            )
            await asyncio.sleep(1.0)

        # Education dropdowns — use fill_select_field for smart fuzzy resolution
        await self._fill_education_dropdowns(page, mapper)

        # Cover letter / generic label fill
        await self.fill_fields_by_label(page, mapper, title, company)

        dry_run = mapper._cfg.get("dry_run", True)
        if dry_run:
            logger.info(f"[ashby] DRY RUN — not submitting {title} @ {company}")
            return {"status": "success", "reason": "dry_run"}

        submitted = await self.click_next(page, ["Submit Application", "Submit", "Apply"])
        if not submitted:
            return {"status": "failed", "reason": "Submit button not found"}
        return {"status": "success", "reason": "submitted"}

    async def _fill_education_dropdowns(self, page: Page, mapper: FieldMapper) -> None:
        """
        Use fill_select_field for any education-related dropdowns on the Ashby form.
        Ashby typically renders these as React comboboxes with ARIA role=option items.
        """
        edu_selectors = [
            ("[data-testid*='degree'] select, [data-testid*='degree']", "degree"),
            ("[data-testid*='major'] select,  [data-testid*='major']",  "major"),
            ("[data-testid*='fieldOfStudy'],   [data-testid*='field-of-study']", "major"),
            ("[data-testid*='educationLevel'], [data-testid*='education-level']", "degree"),
        ]
        for selector, label in edu_selectors:
            for sel in [s.strip() for s in selector.split(",")]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await self.fill_select_field(page, sel, label, mapper)
                        break
                except Exception:
                    pass


# ────────────────────────────────────────────────────────────────────────────────


class SmartRecruitersAdapter(BaseAdapter):
    """
    SmartRecruiters (jobs.smartrecruiters.com/{slug}/{job_id})
    Uses a multi-step wizard similar to Workday but less complex.
    Key automation hooks: [data-testid] attributes and standard HTML names.

    Note: fill_select_field() (from BaseAdapter) is available for any
    education-related dropdown (degree, major, field of study, education level).
    """
    ats_name = "smartrecruiters"

    async def apply(self, page: Page, mapper: FieldMapper, job: dict) -> dict:
        url = job.get("canonical_url", "")
        company = job.get("company", "")
        title = job.get("title", "")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeout:
            return {"status": "failed", "reason": "Page load timeout"}

        # SmartRecruiters may show an "Apply" CTA before the actual form
        try:
            apply_btn = page.locator("button:has-text('Apply'), a:has-text('Apply Now')").first
            if await apply_btn.count() > 0:
                await apply_btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass

        try:
            await page.wait_for_selector("form, [class*='application']", timeout=SELECTOR_TIMEOUT)
        except PlaywrightTimeout:
            return {"status": "failed", "reason": "Application form not found"}

        # Step 1: Personal Information
        fields = {
            "[name='firstName'], [data-testid='first-name']":   mapper.resolve("first_name"),
            "[name='lastName'],  [data-testid='last-name']":    mapper.resolve("last_name"),
            "[name='email'],     [data-testid='email']":        mapper.resolve("email"),
            "[name='phoneNumber'], [data-testid='phone']":      mapper.resolve("phone"),
            "[name='web.LinkedIn'], [placeholder*='LinkedIn']": mapper.resolve("linkedin"),
        }
        for sel, value in fields.items():
            if value:
                for s in [x.strip() for x in sel.split(",")]:
                    if await self.fast_fill(page, s, str(value)):
                        break

        # Next step
        await self.click_next(page, ["Next", "Continue"])

        # Step 2: Resume
        resume = mapper.resolve("resume", "file")
        if resume:
            await self.upload_file(page, "input[type='file']", str(resume))
            await asyncio.sleep(1.5)
        await self.click_next(page, ["Next", "Continue"])

        # Step 3: Questions (includes education dropdowns)
        # Use fill_select_field for degree/major dropdowns before the generic pass
        await self._fill_education_dropdowns(page, mapper)
        await self.fill_fields_by_label(page, mapper, title, company)

        dry_run = mapper._cfg.get("dry_run", True)
        if dry_run:
            logger.info(f"[smartrecruiters] DRY RUN — not submitting {title} @ {company}")
            return {"status": "success", "reason": "dry_run"}

        submitted = await self.click_next(page, ["Submit", "Submit Application", "Send Application"])
        if not submitted:
            return {"status": "failed", "reason": "Submit button not found"}
        return {"status": "success", "reason": "submitted"}

    async def _fill_education_dropdowns(self, page: Page, mapper: FieldMapper) -> None:
        """
        Use fill_select_field for education-related dropdowns on SmartRecruiters forms.
        """
        edu_selectors = [
            ("select[name*='degree'], select[id*='degree']",         "degree"),
            ("select[name*='major'], select[id*='major']",           "major"),
            ("select[name*='fieldOfStudy'], select[id*='field']",    "major"),
            ("select[name*='educationLevel'], select[id*='education']", "degree"),
        ]
        for selector, label in edu_selectors:
            for sel in [s.strip() for s in selector.split(",")]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await self.fill_select_field(page, sel, label, mapper)
                        break
                except Exception:
                    pass
