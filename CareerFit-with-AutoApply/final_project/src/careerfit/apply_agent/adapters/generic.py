"""
Generic Adapter + Amazon/Microsoft Adapters
────────────────────────────────────────────
The Generic adapter is the universal fallback. It uses the label-driven
fill_fields_by_label() from BaseAdapter — no ATS-specific knowledge needed.
Works surprisingly well on custom career pages, BambooHR, Jobvite, iCIMS.

Amazon and Microsoft have separate classes because they have well-known
structural quirks worth handling explicitly.
"""
from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from careerfit.apply_agent.adapters.base import BaseAdapter, SELECTOR_TIMEOUT
from careerfit.apply_agent.core.field_mapper import FieldMapper

logger = logging.getLogger(__name__)


class GenericAdapter(BaseAdapter):
    ats_name = "generic"

    async def apply(self, page: Page, mapper: FieldMapper, job: dict) -> dict:
        url = job.get("canonical_url", "")
        company = job.get("company", "")
        title = job.get("title", "")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeout:
            return {"status": "failed", "reason": "Page load timeout"}

        # Try to find and click an Apply button if this is a JD page, not a form page
        try:
            apply_btn = page.locator(
                "button:has-text('Apply'), a:has-text('Apply Now'), "
                "button:has-text('Apply Now'), [id*='apply']"
            ).first
            if await apply_btn.count() > 0:
                await apply_btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass

        # Wait for any form
        try:
            await page.wait_for_selector("form, [role='form']", timeout=SELECTOR_TIMEOUT)
        except PlaywrightTimeout:
            return {"status": "skipped", "reason": "No application form detected on page"}

        # Resume first (if file input is present)
        resume = mapper.resolve("resume", "file")
        if resume:
            await self.upload_file(page, "input[type='file']", str(resume))
            await asyncio.sleep(0.5)

        # Label-driven fill for everything else
        filled = await self.fill_fields_by_label(page, mapper, title, company)
        logger.info(f"[generic] Filled {filled} fields for {title} @ {company}")

        dry_run = mapper._cfg.get("dry_run", True)
        if dry_run:
            logger.info(f"[generic] DRY RUN — not submitting {title} @ {company}")
            return {"status": "success", "reason": f"dry_run ({filled} fields filled)"}

        submitted = await self.click_next(
            page, ["Submit", "Submit Application", "Apply", "Send", "Next"])
        if submitted:
            return {"status": "success", "reason": "submitted"}
        return {"status": "success", "reason": f"form filled ({filled} fields); manual submit needed"}


class AmazonAdapter(BaseAdapter):
    """
    Amazon Jobs (amazon.jobs) — uses a custom React ATS.
    Applications redirect to internal Amazon ID/login; the agent handles
    public-facing contact info collection before hitting the login wall.
    """
    ats_name = "amazon"

    async def apply(self, page: Page, mapper: FieldMapper, job: dict) -> dict:
        url = job.get("canonical_url", "")
        company = "Amazon"
        title = job.get("title", "")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeout:
            return {"status": "failed", "reason": "Page load timeout"}

        # Amazon requires login — click Apply, detect auth wall, bail gracefully
        try:
            apply_btn = page.locator("a:has-text('Apply now'), button:has-text('Apply')").first
            await apply_btn.wait_for(state="visible", timeout=5_000)
            await apply_btn.click()
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass

        # Detect auth wall
        is_login = await page.locator("input[type='password'], [id*='login'], [id*='signin']").count() > 0
        if is_login:
            return {"status": "skipped",
                    "reason": "Amazon requires account login — sign in manually then retry"}

        # Attempt generic fill if we got past login
        filled = await self.fill_fields_by_label(page, mapper, title, company)

        dry_run = mapper._cfg.get("dry_run", True)
        if dry_run:
            return {"status": "success", "reason": f"dry_run ({filled} fields)"}

        await self.click_next(page, ["Submit", "Next", "Continue"])
        return {"status": "success", "reason": "submitted"}


class MicrosoftAdapter(BaseAdapter):
    """
    Microsoft Careers (apply.careers.microsoft.com) — uses an internal ATS
    built on Azure Active Directory auth. Same pattern as Amazon: requires
    Microsoft account login before form access.
    """
    ats_name = "microsoft"

    async def apply(self, page: Page, mapper: FieldMapper, job: dict) -> dict:
        url = job.get("canonical_url", "")
        company = "Microsoft"
        title = job.get("title", "")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeout:
            return {"status": "failed", "reason": "Page load timeout"}

        # Microsoft will always redirect to login
        try:
            apply_btn = page.locator(
                "button:has-text('Apply'), a:has-text('Apply for this job')").first
            if await apply_btn.count() > 0:
                await apply_btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass

        is_login = await page.locator(
            "input[type='email'][name='loginfmt'], [id='i0116']").count() > 0
        if is_login:
            return {
                "status": "skipped",
                "reason": "Microsoft requires MSA/AAD login — sign in to microsoft.com first",
            }

        # Post-login: standard form fill
        filled = await self.fill_fields_by_label(page, mapper, title, company)

        dry_run = mapper._cfg.get("dry_run", True)
        if dry_run:
            return {"status": "success", "reason": f"dry_run ({filled} fields)"}

        await self.click_next(page, ["Submit", "Next", "Save and Continue"])
        return {"status": "success", "reason": "submitted"}
