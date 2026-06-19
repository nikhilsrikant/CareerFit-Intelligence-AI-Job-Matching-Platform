"""
ApplicationEngine — main orchestrator.
Reads profile from st.secrets (Streamlit Cloud) or a local YAML file.
Uses async Playwright for maximum speed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from careerfit.apply_agent.adapters import get_adapter
from careerfit.apply_agent.core.field_mapper import FieldMapper

logger = logging.getLogger(__name__)

_BLOCK_RESOURCE_TYPES = {"image", "media", "font"}
_BLOCK_URL_PATTERNS = [
    "**/analytics**", "**/hotjar**", "**/segment**", "**/gtm**",
    "**/googletagmanager**", "**/doubleclick**", "**/facebook**",
    "**/intercom**", "**/drift**", "**/mixpanel**",
    "**/*.woff", "**/*.woff2", "**/*.ttf",
]


def load_profile(profile_path: Optional[str] = None) -> dict:
    """
    Load applicant profile from:
      1. SQLite DB profile  (populated via the Profile Setup tab — preferred)
      2. st.secrets  (Streamlit Cloud fallback)
      3. Local YAML file (local dev fallback)
    """
    # Try SQLite DB profile first (populated via the Profile Setup tab)
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _root = _Path(__file__).resolve().parent.parent.parent.parent.parent  # final_project/
        _src = _root / "src"
        if str(_src) not in _sys.path:
            _sys.path.insert(0, str(_src))
        from careerfit.db import load_profile as _db_load_profile
        _db_data = _db_load_profile()
        if _db_data and _db_data.get("email"):
            return _db_data
    except Exception:
        pass

    # Try st.secrets (always available on Streamlit Cloud)
    try:
        import streamlit as st
        if hasattr(st, "secrets") and len(st.secrets) > 0:
            # Convert AttrDict → plain dict
            return dict(st.secrets)
    except Exception:
        pass

    # Fallback: local YAML
    if profile_path:
        import yaml
        p = Path(profile_path)
        if p.exists():
            with p.open() as f:
                return yaml.safe_load(f) or {}

    raise FileNotFoundError(
        "No applicant profile found.\n"
        "• On Streamlit Cloud: add your info in App Settings → Secrets\n"
        "• Locally: create applicant_profile.yaml next to streamlit_app.py"
    )


class ApplicationEngine:

    def __init__(self, profile_path: Optional[str] = None,
                 headless: bool = True,
                 storage_state: Optional[str] = None,
                 qa_answers: "list[dict] | None" = None):
        self._profile_path = profile_path
        self._headless = headless
        self._storage_state = storage_state
        self._qa_answers = qa_answers or []
        self._field_mapper: Optional[FieldMapper] = None
        self._pw = None
        self._browser = None
        self._context = None

    async def start(self, runtime_profile=None):
        from playwright.async_api import async_playwright
        cfg = load_profile(self._profile_path)
        # Extract openai_api_key from profile or db
        openai_api_key = ""
        try:
            api_keys_raw = cfg.get("api_keys", {})
            if isinstance(api_keys_raw, str):
                import json as _json
                api_keys_raw = _json.loads(api_keys_raw)
            openai_api_key = (api_keys_raw or {}).get("openai_api_key", "") or ""
        except Exception:
            pass
        if not openai_api_key:
            try:
                from careerfit import db as _db
                openai_api_key = _db.load_api_keys().get("openai_api_key", "") or ""
            except Exception:
                pass
        self._field_mapper = FieldMapper(
            cfg,
            runtime_profile,
            qa_answers=self._qa_answers,
            openai_api_key=openai_api_key,
            job_description="",
        )

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-gpu", "--disable-extensions",
            ],
        )
        ctx_kwargs: dict = dict(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        if self._storage_state and Path(self._storage_state).exists():
            ctx_kwargs["storage_state"] = self._storage_state

        self._context = await self._browser.new_context(**ctx_kwargs)

        async def _route(route, request):
            if request.resource_type in _BLOCK_RESOURCE_TYPES:
                return await route.abort()
            from fnmatch import fnmatch
            for pat in _BLOCK_URL_PATTERNS:
                if fnmatch(request.url, pat):
                    return await route.abort()
            await route.continue_()

        await self._context.route("**/*", _route)
        logger.info("Engine started (headless=%s)", self._headless)

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()

    async def apply(self, job: dict) -> dict:
        t0 = time.time()
        page = await self._context.new_page()
        try:
            ats = (job.get("source") or job.get("ats") or "generic").lower()
            adapter = get_adapter(ats)
            logger.info("[%s] → %s (ATS: %s)", job.get("company"), job.get("title"), ats)
            # Update job_description context for this specific job
            if self._field_mapper is not None:
                job_desc = job.get("description") or job.get("job_description") or ""
                self._field_mapper._job_description = job_desc
                self._field_mapper._llm_engine_instance = None  # reset so engine rebuilds with new JD
                self._field_mapper._cache.clear()
            result = await adapter.apply(page, self._field_mapper, job)
        except Exception as e:
            logger.exception("Error: %s", e)
            result = {"status": "failed", "reason": str(e)}
        finally:
            await page.close()

        result.update({
            "time_s":  round(time.time() - t0, 2),
            "company": job.get("company", ""),
            "title":   job.get("title", ""),
            "url":     job.get("canonical_url", ""),
            "ats":     job.get("source", "generic"),
        })
        return result

    async def apply_batch(self, jobs: list[dict], max_concurrent: int = 2) -> list[dict]:
        sem = asyncio.Semaphore(max_concurrent)
        async def _go(job):
            async with sem:
                return await self.apply(job)
        return list(await asyncio.gather(*[_go(j) for j in jobs]))

    @classmethod
    def run_sync(cls, jobs: list[dict],
                 profile_path: Optional[str] = None,
                 runtime_profile=None,
                 headless: bool = True,
                 max_concurrent: int = 2,
                 storage_state: Optional[str] = None,
                 qa_answers: "list[dict] | None" = None) -> list[dict]:
        """Blocking wrapper — safe to call from Streamlit."""
        async def _run():
            engine = cls(profile_path, headless=headless, storage_state=storage_state, qa_answers=qa_answers)
            await engine.start(runtime_profile)
            try:
                return await engine.apply_batch(jobs, max_concurrent)
            finally:
                await engine.stop()

        try:
            return asyncio.run(_run())
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, _run()).result()
