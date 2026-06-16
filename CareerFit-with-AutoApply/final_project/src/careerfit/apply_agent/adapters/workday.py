"""
Workday Adapter
───────────────
Workday is the hardest ATS: pure React, auto-save, multi-step wizard,
aggressive bot detection, and ARIA-only inputs (no visible HTML ids).

Speed strategy:
  1. Navigate to the job → click "Apply" → handle sign-in flow.
  2. Use data-automation-id attributes (Workday's stable hooks) for field targeting.
  3. JS-inject for text fields; Playwright interaction for radio/checkbox groups.
  4. Step through pages (My Information → My Experience → Application Questions → Review).
  5. Auto-detect "Already Applied" and bail gracefully.

Known data-automation-ids (stable across Workday tenants):
  legalNameSection, addressSection, phoneSection, emailSection
  resumeSection, coverLetterSection
  workHistorySection, educationSection
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from careerfit.apply_agent.adapters.base import BaseAdapter, SELECTOR_TIMEOUT, REACT_SETTLE_MS
from careerfit.apply_agent.core.field_mapper import FieldMapper

logger = logging.getLogger(__name__)


class WorkdayAdapter(BaseAdapter):
    ats_name = "workday"

    async def apply(self, page: Page, mapper: FieldMapper, job: dict) -> dict:
        url = job.get("canonical_url", "")
        company = job.get("company", "")
        title = job.get("title", "")

        # ── 1. Navigate to job page ─────────────────────────────────────────
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        except PlaywrightTimeout:
            return {"status": "failed", "reason": "Page load timeout"}

        # ── 2. Click Apply button ───────────────────────────────────────────
        apply_clicked = await self._click_apply(page)
        if not apply_clicked:
            return {"status": "skipped", "reason": "Apply button not found — may require account login first"}

        # ── 3. Already applied? ─────────────────────────────────────────────
        if await self._already_applied(page):
            return {"status": "skipped", "reason": "Already applied"}

        # ── 4. Walk through wizard steps ─────────────────────────────────
        step = 0
        max_steps = 8

        while step < max_steps:
            step += 1
            step_name = await self._current_step_name(page)
            logger.info(f"[workday] Step {step}: {step_name}")

            if step_name in ("My Information", ""):
                await self._fill_my_information(page, mapper)
            elif step_name == "My Experience":
                await self._fill_my_experience(page, mapper, title, company)
            elif "Question" in step_name or "Application" in step_name:
                await self._fill_generic(page, mapper, title, company)
            elif step_name in ("Review", "Summary"):
                break  # Don't auto-submit; pause for user review

            # Click Next / Save & Continue
            advanced = await self.click_next(
                page,
                ["Save and Continue", "Next", "Continue", "Save & Continue",
                 "Proceed to Next Step", "Next Step"],
            )
            if not advanced:
                # Try data-automation-id next button
                try:
                    btn = page.locator("[data-automation-id='bottom-navigation-next-button']")
                    await btn.wait_for(timeout=4_000)
                    await btn.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=8_000)
                except Exception:
                    break

        # ── 5. Submit (unless dry_run) ─────────────────────────────────────
        dry_run = mapper._cfg.get("dry_run", True)
        if dry_run:
            logger.info(f"[workday] DRY RUN — not submitting {title} @ {company}")
            return {"status": "success", "reason": "dry_run"}

        submitted = await self._submit(page)
        if submitted:
            return {"status": "success", "reason": "submitted"}
        return {"status": "failed", "reason": "Could not find submit button"}

    # ── Step fillers ─────────────────────────────────────────────────────────

    async def _fill_my_information(self, page: Page, mapper: FieldMapper):
        """Fill the 'My Information' step: name, address, phone, email, work auth."""

        await self._wd_fill("legalName--firstName", mapper.resolve("first_name"), page)
        await self._wd_fill("legalName--lastName",  mapper.resolve("last_name"),  page)
        await self._wd_fill("addressSection--addressLine1", mapper.resolve("address"), page)
        await self._wd_fill("addressSection--city",  mapper.resolve("city"),  page)
        await self._wd_fill("addressSection--postalCode", mapper.resolve("zip_code"), page)

        # Phone — type into the phone field (React-controlled)
        phone = mapper.resolve("phone")
        if phone:
            phone_field = page.locator("[data-automation-id='phone-number']").first
            try:
                await phone_field.wait_for(state="visible", timeout=4_000)
                await phone_field.triple_click()
                await phone_field.type(str(phone), delay=15)
            except Exception:
                pass

        # Work authorization — usually a Yes/No radio group
        await self._wd_radio("workAuthorizationRadioGroup", mapper.resolve("work_authorization"), page)
        await self._wd_radio("requireSponsorshipRadio",    mapper.resolve("sponsorship"),        page)

    async def _fill_my_experience(self, page: Page, mapper: FieldMapper,
                                   title: str, company: str):
        """Fill resume upload and cover letter on 'My Experience' step."""
        resume = mapper.resolve("resume", "file")
        if resume:
            file_input = page.locator("[data-automation-id='resume-section-upload-input'], input[type='file']").first
            try:
                await file_input.wait_for(state="attached", timeout=5_000)
                await file_input.set_input_files(str(resume))
                await asyncio.sleep(1.5)  # Workday needs time to process uploads
            except Exception as e:
                logger.debug(f"[workday] Resume upload: {e}")

        cl = mapper.resolve("cover_letter") or mapper.cover_letter_for_job(title, company)
        if cl:
            await self._wd_fill("coverLetter", cl, page, tag="textarea")

        # LinkedIn
        linkedin = mapper.resolve("linkedin")
        if linkedin:
            await self._wd_fill("linkedIn", linkedin, page)

        # Education dropdowns — degree type and field of study
        # fill_select_field queries live options then fuzzy-matches via dropdown_matcher
        # data-automation-id hooks used by Workday's education section:
        #   educationSection--degreeType  → degree level (e.g. Master of Science)
        #   educationSection--fieldOfStudy → major/field (e.g. Data Science)
        # Guard with count() before calling fill_select_field to avoid burning
        # SELECTOR_TIMEOUT (8 s) on jobs whose education section is absent.
        degree_sel = page.locator("[data-automation-id='degreeType']")
        if await degree_sel.count() > 0:
            await self.fill_select_field(
                page,
                "[data-automation-id='degreeType']",
                "degree",
                mapper,
            )
        major_sel = page.locator("[data-automation-id='fieldOfStudy']")
        if await major_sel.count() > 0:
            await self.fill_select_field(
                page,
                "[data-automation-id='fieldOfStudy']",
                "major",
                mapper,
            )

    async def _fill_generic(self, page: Page, mapper: FieldMapper,
                             title: str, company: str):
        """Generic label-driven fill for Workday's custom question steps."""
        await self.fill_fields_by_label(page, mapper, title, company)

    # ── Workday-specific helpers ─────────────────────────────────────────────

    async def _wd_fill(self, automation_id: str, value: Optional[str],
                        page: Page, tag: str = "input") -> bool:
        """Fill a Workday element by its data-automation-id."""
        if not value:
            return False
        sel = f"[data-automation-id='{automation_id}'] {tag}, [{automation_id}]"
        return await self.fast_fill(page, sel, str(value))

    async def _wd_radio(self, automation_id: str, value: Optional[str], page: Page) -> bool:
        """Click a Yes/No radio in a Workday radio group."""
        if not value:
            return False
        v_lower = str(value).lower()
        try:
            group = page.locator(f"[data-automation-id='{automation_id}']").first
            await group.wait_for(state="visible", timeout=4_000)
            radios = group.get_by_role("radio")
            count = await radios.count()
            for i in range(count):
                label_text = (await radios.nth(i).text_content() or "").lower()
                if v_lower in label_text or label_text in v_lower:
                    await radios.nth(i).click()
                    return True
        except Exception:
            pass
        return False

    async def _click_apply(self, page: Page) -> bool:
        """Click the 'Apply' button on the job description page."""
        candidates = [
            "[data-automation-id='apply-button']",
            "button:has-text('Apply')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply Now')",
        ]
        for sel in candidates:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=5_000)
                await btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=12_000)
                return True
            except Exception:
                pass
        return False

    async def _already_applied(self, page: Page) -> bool:
        """Return True if 'You have already applied' message is visible."""
        try:
            el = page.locator("text=/already applied|duplicate application/i").first
            return await el.count() > 0
        except Exception:
            return False

    async def _current_step_name(self, page: Page) -> str:
        """Read the active wizard step title from Workday's progress indicator."""
        try:
            el = page.locator("[data-automation-id='current-step-name'], .css-current-step, [aria-current='step']").first
            if await el.count() > 0:
                return (await el.text_content() or "").strip()
        except Exception:
            pass
        return ""

    async def _submit(self, page: Page) -> bool:
        """Click the final Submit button."""
        candidates = [
            "[data-automation-id='bottom-navigation-next-button']",
            "button:has-text('Submit')",
            "button:has-text('Submit Application')",
        ]
        for sel in candidates:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=5_000)
                await btn.click()
                await asyncio.sleep(2)
                return True
            except Exception:
                pass
        return False
