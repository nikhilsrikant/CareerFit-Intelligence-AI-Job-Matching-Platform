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
from difflib import SequenceMatcher

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from careerfit.apply_agent.adapters.base import BaseAdapter, SELECTOR_TIMEOUT
from careerfit.apply_agent.core.field_mapper import FieldMapper

logger = logging.getLogger(__name__)


def _radio_matches(value_lower: str, opt_label: str, opt_value: str) -> bool:
    """Return True when *value_lower* is a confident match for an option.

    Uses exact match first, then a SequenceMatcher ratio >= 0.85 threshold to
    avoid substring false-positives (e.g. "No" matching "No, I have no disability"
    AND "No, I do not require sponsorship").
    """
    if opt_label == value_lower or opt_value.lower() == value_lower:
        return True
    ratio = SequenceMatcher(None, value_lower, opt_label).ratio()
    return ratio >= 0.85


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

        # Label-driven fill for everything else (first step/page)
        filled = await self.fill_fields_by_label(page, mapper, title, company)
        # Radio button groups on the first step
        filled += await self._fill_radio_groups(page, mapper, title, company)
        logger.info(f"[generic] Filled {filled} fields for {title} @ {company}")

        # Multi-step loop (up to 5 additional steps)
        for _step in range(5):
            _found_next = await self.click_next(
                page, ["Next", "Continue", "Next Step", "Proceed", "Next Page"]
            )
            if not _found_next:
                break
            await asyncio.sleep(1.0)
            _more = await self.fill_fields_by_label(page, mapper, title, company)
            _more += await self._fill_radio_groups(page, mapper, title, company)
            filled += _more

        dry_run = mapper._cfg.get("dry_run", True)
        if dry_run:
            logger.info(f"[generic] DRY RUN — not submitting {title} @ {company}")
            return {"status": "success", "reason": f"dry_run ({filled} fields filled)"}

        submitted = await self.click_next(
            page, ["Submit", "Submit Application", "Apply", "Send", "Next"])
        if submitted:
            return {"status": "success", "reason": "submitted"}
        return {"status": "success", "reason": f"form filled ({filled} fields); manual submit needed"}

    async def _fill_radio_groups(self, page: Page, mapper: FieldMapper,
                                  job_title: str = "", company: str = "") -> int:
        """Find all radio button groups and select the best option using profile or LLM."""
        filled = 0
        try:
            radio_groups = await page.evaluate("""
                () => {
                    const groups = {};
                    document.querySelectorAll('input[type="radio"]').forEach(el => {
                        const name = el.getAttribute('name');
                        if (!name) return;
                        if (!groups[name]) {
                            // Walk up to find a group label (legend, fieldset label, etc.)
                            let label = '';
                            let parent = el.parentElement;
                            for (let i = 0; i < 8; i++) {
                                if (!parent) break;
                                const legends = parent.querySelectorAll('legend');
                                if (legends.length) { label = legends[0].innerText.trim(); break; }
                                const labels = parent.querySelectorAll('label');
                                if (labels.length && labels[0] !== el.parentElement) {
                                    label = labels[0].innerText.trim(); break;
                                }
                                parent = parent.parentElement;
                            }
                            const options = Array.from(
                                document.querySelectorAll('input[type="radio"][name="' + name + '"]')
                            ).map(r => {
                                let optLabel = '';
                                const id = r.id;
                                if (id) {
                                    const lEl = document.querySelector('label[for="' + id + '"]');
                                    if (lEl) optLabel = lEl.innerText.trim();
                                }
                                if (!optLabel) optLabel = r.value;
                                return { value: r.value, label: optLabel };
                            });
                            groups[name] = { groupLabel: label, options: options };
                        }
                    });
                    return Object.entries(groups).map(([name, data]) => (
                        { name: name, groupLabel: data.groupLabel, options: data.options }
                    ));
                }
            """)

            for group in (radio_groups or []):
                group_label = group.get("groupLabel", "") or group.get("name", "")
                options = group.get("options", [])
                option_labels = [o.get("label", o.get("value", "")) for o in options]

                if not group_label or not options:
                    continue

                # Try profile/Q&A first, then LLM
                value = mapper.resolve(group_label, "radio")
                if not value:
                    value = mapper.resolve_with_llm(group_label, option_labels)

                if not value:
                    continue

                value_lower = str(value).lower().strip()
                for opt in options:
                    opt_label = (opt.get("label") or opt.get("value") or "").lower().strip()
                    opt_value = opt.get("value", "")
                    if _radio_matches(value_lower, opt_label, opt_value):
                        try:
                            radio_sel = (
                                f"input[type='radio'][name='{group['name']}']"
                                f"[value='{opt_value}']"
                            )
                            radio_el = page.locator(radio_sel).first
                            if await radio_el.count() > 0:
                                await radio_el.click()
                                filled += 1
                                break
                        except Exception:
                            pass
        except Exception:
            pass
        return filled


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
