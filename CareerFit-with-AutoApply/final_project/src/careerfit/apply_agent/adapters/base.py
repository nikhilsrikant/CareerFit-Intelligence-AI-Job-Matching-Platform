"""
BaseAdapter — shared speed-optimised fill utilities.

Every ATS adapter inherits from this. The key insight:
  - JS injection (evaluate) fills fields INSTANTLY vs slow type() simulation.
  - We use type() only for fields that require event listeners (React-driven inputs).
  - Images/analytics are already blocked at context level → pages load faster.
  - Smart waits: waitForSelector + short timeout instead of fixed sleep().
"""
from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, Locator, TimeoutError as PlaywrightTimeout

from careerfit.apply_agent.core.field_mapper import FieldMapper

logger = logging.getLogger(__name__)

# How long to wait for any single selector to appear (ms)
SELECTOR_TIMEOUT = 8_000
# Short pause after a fill to let React/Angular reconcile state
REACT_SETTLE_MS = 80


class BaseAdapter(ABC):
    """
    Abstract base class for all ATS adapters.
    Subclasses implement `apply()` and call shared helpers from this base.
    """

    ats_name: str = "generic"

    @abstractmethod
    async def apply(self, page: Page, mapper: FieldMapper, job: dict) -> dict:
        """
        Execute the full application flow.

        Returns:
            {"status": "success"|"failed"|"skipped", "reason": str}
        """
        ...

    # ── Fast fill primitives ────────────────────────────────────────────────

    async def fast_fill(self, page: Page, selector: str, value: str,
                        required: bool = False) -> bool:
        """
        Fill a text input/textarea using JS injection (instant, not keystroke-by-keystroke).
        Falls back to Playwright .fill() for React-controlled inputs.
        Returns True if filled successfully.
        """
        if not value:
            return not required

        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=SELECTOR_TIMEOUT)

            # Attempt native value setter (works for plain HTML inputs)
            filled = await page.evaluate("""
                ([sel, val]) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set ||
                    Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    )?.set;
                    if (nativeInputValueSetter) {
                        nativeInputValueSetter.call(el, val);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
            """, [selector, value])

            if not filled:
                # React fallback: clear + type
                await locator.triple_click()
                await locator.type(value, delay=20)

            await asyncio.sleep(REACT_SETTLE_MS / 1000)
            return True

        except PlaywrightTimeout:
            if required:
                logger.warning(f"[{self.ats_name}] Required field not found: {selector}")
            return False
        except Exception as e:
            logger.debug(f"[{self.ats_name}] fast_fill error on {selector}: {e}")
            return False

    async def fast_fill_locator(self, locator: Locator, value: str) -> bool:
        """Fill a Locator directly — used when we have the element, not a CSS selector."""
        if not value:
            return False
        try:
            await locator.wait_for(state="visible", timeout=SELECTOR_TIMEOUT)
            await locator.triple_click()
            await locator.fill(value)
            await asyncio.sleep(REACT_SETTLE_MS / 1000)
            return True
        except Exception as e:
            logger.debug(f"[{self.ats_name}] fast_fill_locator: {e}")
            return False

    async def select_option(self, page: Page, selector: str, value: str) -> bool:
        """
        Select an <option> whose text or value fuzzy-matches `value`.
        Handles both native <select> and custom React dropdowns.
        """
        if not value:
            return False
        try:
            el = page.locator(selector).first
            await el.wait_for(state="visible", timeout=SELECTOR_TIMEOUT)
            tag = await el.evaluate("el => el.tagName.toLowerCase()")

            if tag == "select":
                # Native select: try exact, then fuzzy
                options = await el.evaluate("""
                    el => Array.from(el.options).map(o => ({text: o.text, value: o.value}))
                """)
                target = _best_option(value, options)
                if target:
                    await el.select_option(value=target)
                    return True
            else:
                # React/custom dropdown: click to open, then click matching item
                await el.click()
                await asyncio.sleep(0.25)
                option = page.get_by_role("option", name=value)
                if await option.count() > 0:
                    await option.first.click()
                    return True
                # fuzzy: find option whose text contains our value
                opts = page.get_by_role("option")
                count = await opts.count()
                value_l = value.lower()
                for i in range(count):
                    txt = (await opts.nth(i).text_content() or "").lower()
                    if value_l in txt or txt in value_l:
                        await opts.nth(i).click()
                        return True
        except Exception as e:
            logger.debug(f"[{self.ats_name}] select_option: {e}")
        return False

    async def fill_select_field(self, page: Page, selector: str, label: str,
                                mapper: FieldMapper) -> bool:
        """
        Query all option texts from a select element, use mapper.resolve_select()
        to pick the best match via fuzzy + fallback chains, then select it.

        Handles both native <select> elements and React/custom dropdowns that
        expose options via ARIA role="option".

        Returns True if a matching option was successfully selected.
        """
        try:
            el = page.locator(selector).first
            await el.wait_for(state="visible", timeout=SELECTOR_TIMEOUT)
            tag = await el.evaluate("el => el.tagName.toLowerCase()")

            if tag == "select":
                options = await el.evaluate(
                    "el => Array.from(el.options).map(o => o.text.trim()).filter(t => t)"
                )
            else:
                # React / custom dropdown: click to open, then collect option texts
                await el.click()
                await asyncio.sleep(0.3)
                opt_els = page.get_by_role("option")
                count = await opt_els.count()
                options = [
                    (await opt_els.nth(i).text_content() or "")
                    for i in range(count)
                ]
                options = [o.strip() for o in options if o.strip()]
                if not options:
                    await page.keyboard.press("Escape")
                    return False

            best = mapper.resolve_select(label, options)
            if not best:
                if tag != "select":
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.1)
                    await page.keyboard.press("Tab")
                return False

            if tag == "select":
                # Select by option text via JS so change events fire correctly
                await el.evaluate(
                    "(el, text) => {"
                    "  Array.from(el.options).forEach(o => {"
                    "    if (o.text.trim() === text) o.selected = true;"
                    "  });"
                    "  el.dispatchEvent(new Event('change', {bubbles: true}));"
                    "}",
                    best,
                )
            else:
                # Click the matching option in the already-open React dropdown
                opt = page.get_by_role("option", name=best)
                if await opt.count() > 0:
                    await opt.first.click()
                else:
                    # Fallback: find by exact text_content match
                    opts = page.get_by_role("option")
                    cnt = await opts.count()
                    for i in range(cnt):
                        txt = (await opts.nth(i).text_content() or "").strip()
                        if txt == best:
                            await opts.nth(i).click()
                            break

            await asyncio.sleep(REACT_SETTLE_MS / 1000)
            return True

        except Exception as exc:
            logger.debug(f"[{self.ats_name}] fill_select_field({label}): {exc}")
            return False

    async def upload_file(self, page: Page, selector: str, file_path: str) -> bool:
        """Attach a file to a file input element."""
        if not file_path or not Path(file_path).exists():
            logger.warning(f"[{self.ats_name}] Resume not found: {file_path}")
            return False
        try:
            locator = page.locator(selector).first
            await locator.wait_for(state="attached", timeout=SELECTOR_TIMEOUT)
            await locator.set_input_files(file_path)
            await asyncio.sleep(0.4)
            return True
        except Exception as e:
            logger.debug(f"[{self.ats_name}] upload_file: {e}")
            return False

    async def click_next(self, page: Page, labels: list[str]) -> bool:
        """Click a button matching any of the given label strings (case-insensitive)."""
        for label in labels:
            try:
                btn = page.get_by_role("button", name=label)
                if await btn.count() > 0:
                    await btn.first.scroll_into_view_if_needed()
                    await btn.first.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    return True
                # Fallback: any element whose text matches
                el = page.locator(f"text=/{label}/i").first
                if await el.count() > 0:
                    await el.click()
                    return True
            except Exception:
                pass
        return False

    async def fill_fields_by_label(self, page: Page, mapper: FieldMapper,
                                   job_title: str = "", company: str = "") -> int:
        """
        Generic label-driven fill: find all visible input/textarea/select elements,
        read their associated label text, resolve via mapper, and fill.
        Returns count of fields successfully filled.
        """
        filled = 0
        # Collect all form fields with their labels
        fields_info = await page.evaluate("""
            () => {
                const results = [];
                const inputs = document.querySelectorAll(
                    'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio]):not([type=file]), textarea, select'
                );
                inputs.forEach((el, idx) => {
                    // Find label
                    let label = '';
                    const id = el.id;
                    if (id) {
                        const labelEl = document.querySelector(`label[for="${id}"]`);
                        if (labelEl) label = labelEl.innerText.trim();
                    }
                    if (!label) {
                        // Walk up for wrapping label
                        let parent = el.parentElement;
                        for (let i = 0; i < 5; i++) {
                            if (!parent) break;
                            if (parent.tagName === 'LABEL') { label = parent.innerText.replace(el.value,'').trim(); break; }
                            const siblings = parent.querySelectorAll('label');
                            if (siblings.length) { label = siblings[0].innerText.trim(); break; }
                            parent = parent.parentElement;
                        }
                    }
                    if (!label) label = el.getAttribute('placeholder') || el.getAttribute('aria-label') || el.getAttribute('name') || '';
                    results.push({
                        tag: el.tagName.toLowerCase(),
                        type: el.getAttribute('type') || '',
                        label: label,
                        placeholder: el.getAttribute('placeholder') || '',
                        name: el.getAttribute('name') || '',
                        id: id,
                        idx: idx
                    });
                });
                return results;
            }
        """)

        for fi in fields_info:
            label_text = fi["label"] or fi["placeholder"] or fi["name"]
            if not label_text:
                continue

            field_type = "file" if fi["type"] == "file" else fi["tag"]
            value = mapper.resolve(label_text, field_type)

            # Special case: cover letter auto-generate if empty
            if not value and "cover letter" in label_text.lower():
                value = mapper.cover_letter_for_job(job_title, company)

            if not value:
                continue

            # Build a selector to target this specific element
            if fi["id"]:
                sel = f"#{fi['id']}"
            elif fi["name"]:
                sel = f"[name='{fi['name']}']"
            else:
                continue  # can't reliably target without id or name

            if fi["tag"] == "select":
                ok = await self.select_option(page, sel, str(value))
            elif fi["type"] == "file":
                ok = await self.upload_file(page, sel, str(value))
            else:
                ok = await self.fast_fill(page, sel, str(value))

            if ok:
                filled += 1

        return filled

    async def wait_for_any(self, page: Page, selectors: list[str],
                           timeout: int = 10_000) -> Optional[str]:
        """Wait until any of the given selectors appears; return which one."""
        tasks = []
        for sel in selectors:
            tasks.append(page.wait_for_selector(sel, timeout=timeout))
        done, pending = await asyncio.wait(
            [asyncio.create_task(t) for t in tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            p.cancel()
        for d in done:
            try:
                result = await d
                if result:
                    return await result.evaluate("el => el.getAttribute('data-sel') || el.tagName")
            except Exception:
                pass
        return None


# ── Utility ────────────────────────────────────────────────────────────────

def _best_option(value: str, options: list[dict]) -> Optional[str]:
    """Find the option value whose text best matches our desired value string."""
    from difflib import SequenceMatcher
    v_lower = value.lower()
    # Exact text match
    for o in options:
        if o["text"].lower().strip() == v_lower:
            return o["value"]
    # Startswith / contains
    for o in options:
        t = o["text"].lower()
        if t.startswith(v_lower) or v_lower in t:
            return o["value"]
    # Fuzzy
    best_val, best_score = None, 0.0
    for o in options:
        score = SequenceMatcher(None, v_lower, o["text"].lower()).ratio()
        if score > best_score:
            best_score = score
            best_val = o["value"]
    return best_val if best_score > 0.60 else None
