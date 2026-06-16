"""
Lever Adapter
─────────────
Lever (jobs.lever.co/{slug}/{job_id}) has a straightforward single-page form.
The HTML is clean, static, and predictable — fastest ATS to automate.

Field IDs follow a consistent pattern:
  #name, #email, #phone, #org (current company), #urls[LinkedIn],
  #urls[GitHub], #urls[Portfolio], #cards[cover letter], textarea (cover letter)

Resume: Lever has a dedicated "Upload Resume" button → hidden file input.

Note: fill_select_field() (from BaseAdapter) is available for any education-related
dropdown (degree, major, field of study, education level). It fetches live options
and uses the smart dropdown fallback engine (dropdown_matcher.py) to pick the best
match — no manual option text required.
"""
from __future__ import annotations

import asyncio
import logging
import re

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from careerfit.apply_agent.adapters.base import BaseAdapter, SELECTOR_TIMEOUT
from careerfit.apply_agent.core.field_mapper import FieldMapper

logger = logging.getLogger(__name__)


class LeverAdapter(BaseAdapter):
    ats_name = "lever"

    async def apply(self, page: Page, mapper: FieldMapper, job: dict) -> dict:
        url = job.get("canonical_url", "")
        company = job.get("company", "")
        title = job.get("title", "")

        # ── 1. Navigate ─────────────────────────────────────────────────────
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeout:
            return {"status": "failed", "reason": "Page load timeout"}

        # ── 2. Wait for form ────────────────────────────────────────────────
        try:
            await page.wait_for_selector(".application-form, form.application, #application",
                                         timeout=SELECTOR_TIMEOUT)
        except PlaywrightTimeout:
            return {"status": "failed", "reason": "Application form not found"}

        # ── 3. Standard fields ──────────────────────────────────────────────
        fields = {
            "#name":                         mapper.resolve("full_name"),
            "#email":                        mapper.resolve("email"),
            "#phone":                        mapper.resolve("phone"),
            "#org":                          mapper.resolve("current_company"),
            "input[name='urls[LinkedIn]']":  mapper.resolve("linkedin"),
            "input[name='urls[GitHub]']":    mapper.resolve("github"),
            "input[name='urls[Portfolio]']": mapper.resolve("website"),
        }
        for sel, value in fields.items():
            if value:
                await self.fast_fill(page, sel, str(value))

        # ── 4. Resume upload ────────────────────────────────────────────────
        resume = mapper.resolve("resume", "file")
        if resume:
            # Click the "Upload Resume" button to reveal the file input
            try:
                upload_btn = page.get_by_text("Upload Resume").first
                if await upload_btn.count() > 0:
                    await upload_btn.click()
                    await asyncio.sleep(0.3)
            except Exception:
                pass
            await self.upload_file(
                page,
                "input[type='file'][name='resume'], input[type='file'][class*='resume']",
                str(resume),
            )

        # ── 5. Cover letter ─────────────────────────────────────────────────
        cl = mapper.resolve("cover_letter") or mapper.cover_letter_for_job(title, company)
        if cl:
            # Lever uses a collapsible "Add a cover letter" card
            try:
                toggle = page.get_by_text("Add a cover letter").first
                if await toggle.count() > 0:
                    await toggle.click()
                    await asyncio.sleep(0.25)
            except Exception:
                pass
            await self.fast_fill(page, "textarea[name='comments'], #cover-letter", cl)

        # ── 6. Custom / additional questions ───────────────────────────────
        await self._fill_education_dropdowns(page, mapper)
        await self.fill_fields_by_label(page, mapper, title, company)

        # ── 7. EEO / demographic checkboxes ────────────────────────────────
        await self._fill_eeo(page, mapper)

        # ── 8. Submit ──────────────────────────────────────────────────────
        dry_run = mapper._cfg.get("dry_run", True)
        if dry_run:
            logger.info(f"[lever] DRY RUN — not submitting {title} @ {company}")
            return {"status": "success", "reason": "dry_run"}

        submitted = await self.click_next(
            page, ["Submit Application", "Submit your application", "Submit", "Apply"])
        if not submitted:
            return {"status": "failed", "reason": "Submit button not found"}

        # Confirm page
        try:
            await page.wait_for_selector(
                ".confirmation-page, [class*='thanks'], h1:has-text('Thank')",
                timeout=10_000,
            )
        except PlaywrightTimeout:
            pass  # Some Lever boards redirect without a selector

        return {"status": "success", "reason": "submitted"}

    async def _fill_education_dropdowns(self, page: Page, mapper: FieldMapper) -> None:
        """
        Use fill_select_field for any education-related dropdowns on Lever forms.
        Lever occasionally includes degree/major fields in custom question sections.
        fill_select_field queries live options then fuzzy-matches via dropdown_matcher.
        """
        edu_selectors = [
            ("select[name*='degree'], select[id*='degree']",       "degree"),
            ("select[name*='major'],  select[id*='major']",        "major"),
            ("select[name*='field'],  select[id*='fieldOfStudy']", "major"),
            ("select[name*='education']",                          "degree"),
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

    async def _fill_eeo(self, page: Page, mapper: FieldMapper):
        eeo = {
            "select[name='eeo[gender]']":    mapper.resolve("gender"),
            "select[name='eeo[race]']":      mapper.resolve("ethnicity"),
            "select[name='eeo[veteran]']":   mapper.resolve("veteran_status"),
            "select[name='eeo[disability]']": mapper.resolve("disability"),
        }
        for sel, val in eeo.items():
            if val:
                await self.select_option(page, sel, str(val))
