from __future__ import annotations

# ── Auto-install Playwright Chromium on Streamlit Cloud ──────────────────────
import subprocess, sys as _sys
subprocess.run(
    [_sys.executable, "-m", "playwright", "install", "chromium"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)
# ─────────────────────────────────────────────────────────────────────────────

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
# Ensure both ROOT (for streamlit_apply_patch) and SRC (for careerfit.*) are on sys.path
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pandas as pd
import streamlit as st

try:
    from careerfit import db as _careerfit_db
except Exception:
    _careerfit_db = None

from careerfit.fetchers import (
    NormalizedJob,
    clear_fetch_cache,
    detect_career_source,
    expand_company_sources,
    fetch_jobs_for_company,
    load_companies,
)
from careerfit.matching import JobMatch, extract_intent_terms, rank_jobs
from careerfit.source_intelligence import RuntimeProfile, build_runtime_profile
try:
    from careerfit.filters import apply_filters as _apply_filters
except ImportError:
    _apply_filters = None

try:
    from careerfit.job_board import (
        fetch_greenhouse_public as _fetch_greenhouse_public,
        fetch_lever_public as _fetch_lever_public,
        fetch_workday_public as _fetch_workday_public,
        convert_to_normalized_job as _convert_to_normalized_job,
    )
    _JOB_BOARD_AVAILABLE = True
except Exception:
    _fetch_greenhouse_public = None
    _fetch_lever_public = None
    _fetch_workday_public = None
    _convert_to_normalized_job = None
    _JOB_BOARD_AVAILABLE = False
# ── streamlit_apply_patch inlined — avoids Streamlit Cloud import path issues ──

DEFAULT_STORAGE_STATE = str(ROOT / "data" / "browser_session.json")

_STATUS_COLOR = {
    "success": "#10B981", "failed": "#F43F5E",
    "skipped": "#F59E0B", "dry_run": "#7C3AED",
}


def _chip(source: str) -> str:
    colours = {
        "greenhouse":      ("#22C55E", "#F0FDF4"),
        "workday":         ("#3B82F6", "#EFF6FF"),
        "lever":           ("#A855F7", "#FAF5FF"),
        "ashby":           ("#F59E0B", "#FFFBEB"),
        "smartrecruiters": ("#EF4444", "#FEF2F2"),
        "amazon":          ("#F97316", "#FFF7ED"),
        "microsoft":       ("#0EA5E9", "#F0F9FF"),
        "greenhouse_api":  ("#22C55E", "#F0FDF4"),
        "lever_api":       ("#A855F7", "#FAF5FF"),
        "workday_api":     ("#3B82F6", "#EFF6FF"),
    }
    fg, bg = colours.get((source or "").lower(), ("#64748B", "#F1F5F9"))
    label = source.replace("_api", "").title() if source else "?"
    return (f"<span style='background:{bg};color:{fg};padding:2px 8px;"
            f"border-radius:999px;font-size:11px;font-weight:700'>{label}</span>")


def build_apply_cfg(db_profile: dict, platform: str) -> dict:
    cfg = dict(db_profile)
    platforms_data = cfg.pop("platforms", None) or {}
    if isinstance(platforms_data, str):
        try:
            platforms_data = json.loads(platforms_data)
        except Exception:
            platforms_data = {}
    plat_key = (platform or "").lower()
    plat_creds = platforms_data.get(plat_key, {})
    if plat_creds.get("email"):
        cfg["email"] = plat_creds["email"]
    if plat_creds.get("password"):
        cfg["password"] = plat_creds["password"]
    return cfg


def run_continuous_apply(
    threshold: float = 0.90,
    delay_secs: int = 45,
    max_apps: int = 50,
    dry_run: bool = True,
    headless: bool = True,
    qa_answers: "list[dict] | None" = None,
) -> None:
    if threshold < 0.90:
        threshold = 0.90
    try:
        from careerfit import db as _db
        from careerfit.apply_agent.engine import ApplicationEngine
    except ImportError as _e:
        st.error(f"Required module not available: {_e}")
        st.session_state.continuous_apply_running = False
        return

    db_profile = _db.load_profile()
    if not db_profile or not db_profile.get("email"):
        st.error("Profile not configured. Please complete the Profile Setup tab first.")
        st.session_state.continuous_apply_running = False
        return

    matches = list(st.session_state.get("matches", []))
    if not matches:
        st.warning("No job matches available. Run Job Matching first to get results.")
        st.session_state.continuous_apply_running = False
        return

    already_applied = _db.get_applied_job_ids()
    apply_list = []
    for m in matches:
        if m.score < threshold:
            continue
        if m.decision in ("f1_filtered", "role_filtered"):
            continue
        job_id = str(m.raw_job.get("external_job_id") or "")
        if _db.is_already_applied(job_id, m.canonical_url):
            continue
        apply_list.append(m)

    apply_list = apply_list[:max_apps]

    if not apply_list:
        st.info(f"No new jobs to apply to (threshold: {int(threshold*100)}%, filters active, duplicates excluded).")
        st.session_state.continuous_apply_running = False
        return

    label = "Dry-running" if dry_run else "Applying to"
    progress_container = st.empty()
    all_results = []

    st.warning("Auto-apply is running. The Stop button takes effect between jobs.")

    for i, match in enumerate(apply_list):
        if st.session_state.get("continuous_apply_stop", False):
            progress_container.info(f"Stopped by user after {i} applications.")
            break

        progress_container.markdown(
            f"<div class='cf-alert cf-alert-info'>⚡ {label} <b>{match.company}</b> — {match.title} "
            f"({i+1}/{len(apply_list)}) | Score: {int(match.score*100)}%</div>",
            unsafe_allow_html=True,
        )

        job_dict = {
            "canonical_url": match.canonical_url,
            "title":         match.title,
            "company":       match.company,
            "source":        match.source,
            "location":      match.location or "",
        }

        apply_cfg = build_apply_cfg(db_profile, match.source)
        apply_cfg["dry_run"] = dry_run

        try:
            import careerfit.apply_agent.engine as _eng
            _orig_lp = _eng.load_profile
            _eng.load_profile = lambda *a, **kw: apply_cfg
            job_results = ApplicationEngine.run_sync(
                jobs=[job_dict],
                runtime_profile=st.session_state.get("profile"),
                headless=headless,
                max_concurrent=1,
                storage_state=DEFAULT_STORAGE_STATE,
                qa_answers=qa_answers,
            )
        except Exception as exc:
            job_results = [{"status": "failed", "reason": str(exc),
                            "company": match.company, "title": match.title,
                            "ats": match.source, "time_s": 0}]
        finally:
            try:
                _eng.load_profile = _orig_lp
            except Exception:
                pass

        if job_results:
            result = job_results[0]
            result["company"] = match.company
            result["title"]   = match.title
            result["ats"]     = match.source

            if result.get("status") == "success" and not dry_run:
                job_id = str(match.raw_job.get("external_job_id") or "")
                _db.mark_applied(job_id, match.canonical_url, match.company, match.title, match.source)
                st.session_state.applied_job_ids = _db.get_applied_job_ids()

            all_results.append(result)
            st.session_state.continuous_apply_results = all_results
            st.session_state.apply_results = all_results

        if i < len(apply_list) - 1 and not st.session_state.get("continuous_apply_stop", False):
            time.sleep(delay_secs)

    st.session_state.continuous_apply_running = False
    st.session_state.continuous_apply_stop = False
    ok   = sum(1 for r in all_results if r.get("status") == "success")
    bad  = sum(1 for r in all_results if r.get("status") == "failed")
    skip = sum(1 for r in all_results if r.get("status") == "skipped")
    note = " (dry run — not submitted)" if dry_run else ""
    progress_container.success(f"Auto-apply complete{note}: {ok} ✅  {skip} ⏭  {bad} ❌  out of {len(all_results)} attempted")
    st.rerun()


def init_apply_session_state():
    defaults = {
        "apply_queue":              [],
        "apply_results":            [],
        "apply_running":            False,
        "apply_dry_run":            True,
        "apply_headless":           True,
        "apply_max_concurrent":     2,
        "continuous_apply_running": False,
        "continuous_apply_stop":    False,
        "continuous_apply_results": [],
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def render_apply_button(match) -> None:
    job_dict = {
        "canonical_url": match.canonical_url,
        "title":         match.title,
        "company":       match.company,
        "source":        match.source,
        "location":      match.location or "",
    }
    already = any(j["canonical_url"] == job_dict["canonical_url"]
                  for j in st.session_state.apply_queue)
    if already:
        st.markdown(
            "<span style='font-size:12px;color:#10B981;font-weight:700'>✓ In apply queue</span>",
            unsafe_allow_html=True,
        )
    else:
        key = f"qbtn_{abs(hash(match.canonical_url))}"
        if st.button("⚡ Queue to Apply", key=key, help="Add to the auto-apply queue"):
            st.session_state.apply_queue.append(job_dict)
            st.rerun()


def render_apply_queue_panel() -> None:
    st.markdown("---")
    st.markdown("<div class='cf-side-label'>⚡ Manual Apply Queue</div>", unsafe_allow_html=True)

    queue = st.session_state.apply_queue
    n = len(queue)

    if n == 0:
        st.markdown(
            "<div class='cf-side-note'>No jobs queued yet.<br>"
            "Click <b>⚡ Queue to Apply</b> on any job card.</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(f"<div class='cf-side-note'><b>{n}</b> job(s) in queue.</div>", unsafe_allow_html=True)
        for i, job in enumerate(list(queue)):
            c1, c2 = st.columns([5, 1])
            with c1:
                st.markdown(
                    f"<span style='font-size:12px'>{_chip(job.get('source',''))} <b>{job['company']}</b>"
                    f" — {job['title'][:28]}</span>",
                    unsafe_allow_html=True,
                )
            with c2:
                if st.button("✕", key=f"rm_{i}_{abs(hash(job['canonical_url']))}"):
                    st.session_state.apply_queue.pop(i)
                    st.rerun()
        if st.button("Clear queue", use_container_width=True, key="clear_queue_btn"):
            st.session_state.apply_queue = []
            st.rerun()

    st.markdown("<div class='cf-side-label'>Agent Settings</div>", unsafe_allow_html=True)

    dry_run = st.toggle("Dry run (fill, don't submit)", value=st.session_state.apply_dry_run,
                        key="dry_run_toggle",
                        help="Fills every field but never clicks Submit. Turn OFF when ready.")
    st.session_state.apply_dry_run = dry_run

    headless = st.toggle("Headless browser", value=st.session_state.apply_headless,
                          key="headless_toggle",
                          help="OFF = watch the browser fill forms in real time.")
    st.session_state.apply_headless = headless

    conc = st.slider("Concurrent tabs", 1, 3,
                     value=st.session_state.apply_max_concurrent,
                     key="concurrency_slider")
    st.session_state.apply_max_concurrent = conc

    can_run = n > 0 and not st.session_state.apply_running
    if st.button("🚀 Run Auto-Apply", type="primary", use_container_width=True,
                 disabled=not can_run, key="run_apply_btn"):
        st.session_state.apply_running = True
        st.rerun()


def _render_results(results: list[dict]) -> None:
    st.markdown("---")
    st.markdown("### ⚡ Auto-Apply Results")
    for r in results:
        status = r.get("status", "unknown")
        color  = _STATUS_COLOR.get(status, "#94A3B8")
        reason = r.get("reason", "")
        if reason == "dry_run":
            reason = "dry run (not submitted)"
        t = r.get("time_s", "?")
        st.markdown(
            f"""<div style='border:1px solid #E2E8F0;border-radius:14px;padding:14px 18px;
                margin-bottom:10px;background:#fff;display:flex;justify-content:space-between;
                align-items:center;gap:12px;flex-wrap:wrap'>
              <div>
                <span style='font-weight:700;font-size:14px'>{r.get('company','')}</span>
                <span style='color:#64748B;font-size:13px'> — {r.get('title','')}</span>
                <span style='margin-left:8px'>{_chip(r.get('ats',''))}</span>
              </div>
              <div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap'>
                <span style='background:{color}22;color:{color};padding:4px 10px;
                             border-radius:999px;font-size:12px;font-weight:700'>
                  {status.upper()}
                </span>
                <span style='color:#94A3B8;font-size:12px'>{reason}</span>
                <span style='color:#94A3B8;font-size:11px'>{t}s</span>
              </div>
            </div>""",
            unsafe_allow_html=True,
        )
    if st.button("Clear results", key="clear_results_btn"):
        st.session_state.apply_results = []
        st.rerun()


def run_apply_queue() -> None:
    if st.session_state.apply_results:
        _render_results(st.session_state.apply_results)

    if not st.session_state.apply_running:
        return

    jobs = list(st.session_state.apply_queue)
    if not jobs:
        st.session_state.apply_running = False
        return

    dry = st.session_state.apply_dry_run
    label = "🔍 Dry-running" if dry else "🚀 Applying to"

    with st.spinner(f"{label} {len(jobs)} job(s)…"):
        try:
            from careerfit.apply_agent.engine import ApplicationEngine, load_profile
            cfg = load_profile()
            cfg["dry_run"] = dry
            import careerfit.apply_agent.engine as _eng
            _orig = _eng.load_profile
            _eng.load_profile = lambda *a, **kw: cfg
            results = ApplicationEngine.run_sync(
                jobs=jobs,
                runtime_profile=st.session_state.get("profile"),
                headless=st.session_state.apply_headless,
                max_concurrent=st.session_state.apply_max_concurrent,
                storage_state=DEFAULT_STORAGE_STATE,
                qa_answers=st.session_state.get("qa_answers", []),
            )
            _eng.load_profile = _orig
        except ImportError as e:
            st.error(f"Playwright not available: {e}")
            results = []
        except FileNotFoundError as e:
            st.error(str(e))
            results = []
        except Exception as e:
            st.error(f"Agent error: {e}")
            results = []

    st.session_state.apply_results = results
    st.session_state.apply_running = False

    if results:
        ok   = sum(1 for r in results if r.get("status") == "success")
        bad  = sum(1 for r in results if r.get("status") == "failed")
        skip = sum(1 for r in results if r.get("status") == "skipped")
        note = " (dry run — not submitted)" if dry else ""
        st.success(f"Done{note}: {ok} ✅  {skip} ⏭  {bad} ❌")
    st.rerun()

# ── end of inlined streamlit_apply_patch ─────────────────────────────────────

APP_TITLE = "CareerFit Studio"
DEFAULT_THRESHOLD = float(os.getenv("CAREERFIT_DEFAULT_THRESHOLD", "0.90"))
RUNTIME_COMPANIES_PATH = ROOT / "data" / "runtime_companies.json"

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)
init_apply_session_state()

CSS = r"""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,300..800&display=swap');

/* ════════════════════════════════════════════════════════════
   FORCE LIGHT THEME — beats Streamlit's OS-dark-mode detection
   ════════════════════════════════════════════════════════════ */
:root { color-scheme: light !important; }

:root {
  --blue:    #2563EB;
  --blue-lt: #3B82F6;
  --blue-dk: #1D4ED8;
  --cyan:    #06B6D4;
  --mint:    #10B981;
  --purple:  #7C3AED;
  --rose:    #F43F5E;
  --amber:   #F59E0B;

  --ink:     #0C1322;
  --ink-2:   #1E293B;
  --muted:   #64748B;
  --muted-2: #94A3B8;
  --line:    #E2E8F0;
  --line-2:  #F1F5F9;
  --panel:   #FFFFFF;
  --bg:      #F4F6FB;

  --radius-sm: 10px;
  --radius:    16px;
  --radius-lg: 22px;
  --radius-xl: 28px;
  --shadow-sm: 0 1px 4px rgba(15,23,42,.06), 0 4px 12px rgba(15,23,42,.04);
  --shadow:    0 4px 16px rgba(15,23,42,.08), 0 12px 32px rgba(15,23,42,.05);
  --shadow-lg: 0 8px 32px rgba(15,23,42,.10), 0 24px 56px rgba(15,23,42,.07);
  --shadow-blue: 0 8px 24px rgba(37,99,235,.22);
  --transition: 0.18s cubic-bezier(.4,0,.2,1);
  --glass-bg: rgba(255,255,255,0.72);
  --glass-border: rgba(255,255,255,0.25);
  --glass-shadow: 0 8px 32px rgba(15,23,42,0.10);
  --blur: blur(16px);
  --ambient-1: rgba(37,99,235,0.09);
  --ambient-2: rgba(6,182,212,0.07);
  --ambient-3: rgba(16,185,129,0.06);
}

html, body { background: #F4F6FB !important; color: #0C1322 !important; }
html, body, [class*="css"], .stApp {
  font-family: 'DM Sans', ui-sans-serif, system-ui, sans-serif;
  font-feature-settings: 'kern' 1, 'liga' 1;
}

.stApp,
[data-testid="stAppViewContainer"],
[data-testid="stMain"],
.main { background-color: #F4F6FB !important; color: #0C1322 !important; }

.stApp {
  background-image:
    radial-gradient(ellipse 80% 50% at 0% 0%, rgba(37,99,235,.09) 0%, transparent 55%),
    radial-gradient(ellipse 60% 40% at 100% 100%, rgba(6,182,212,.07) 0%, transparent 55%),
    radial-gradient(ellipse 50% 60% at 50% 0%, rgba(16,185,129,.05) 0%, transparent 60%),
    radial-gradient(ellipse 40% 30% at 80% 30%, rgba(124,58,237,.04) 0%, transparent 50%) !important;
}

section.main > div { padding-top: 0.75rem; }
#MainMenu, footer { visibility: hidden; }
.block-container { padding-left: 2rem; padding-right: 2rem; max-width: 1600px; }
.main p, .main span, .main label, .main h1, .main h2, .main h3, .main h4, .main h5, .main h6, .main li, .main div { color: inherit; }

/* ════════════════════════════════════════════════════════════
   SIDEBAR (always dark — by design)
   ════════════════════════════════════════════════════════════ */
[data-testid="stSidebar"] {
  background: linear-gradient(160deg, #08101E 0%, #0D1829 55%, #0F1F35 100%) !important;
  border-right: 1px solid rgba(255,255,255,.06);
  box-shadow: 4px 0 24px rgba(0,0,0,.18);
}
[data-testid="stSidebar"] * { color: #CBD5E1; }
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color: #94A3B8; font-size: 13px; }
[data-testid="stSidebar"] .stRadio label,
[data-testid="stSidebar"] .stCheckbox label { color: #CBD5E1 !important; font-size: 13.5px; }
[data-testid="stSidebar"] label { color: #94A3B8 !important; font-size: 11px !important; letter-spacing: .06em; font-weight: 600 !important; text-transform: uppercase; }

[data-testid="stSidebar"] [data-testid="stNumberInput"] input,
[data-testid="stSidebar"] [data-testid="stTextInput"] input,
[data-testid="stSidebar"] [data-testid="stTextArea"] textarea,
[data-testid="stSidebar"] [data-baseweb="input"] input,
[data-testid="stSidebar"] [data-baseweb="textarea"] textarea {
  background: rgba(255,255,255,.07) !important;
  color: #F1F5F9 !important;
  -webkit-text-fill-color: #F1F5F9 !important;
  caret-color: #60A5FA !important;
  border: 1px solid rgba(148,163,184,.2) !important;
  border-radius: var(--radius-sm) !important;
  font-family: 'DM Sans', sans-serif !important;
}
[data-testid="stSidebar"] input::placeholder,
[data-testid="stSidebar"] textarea::placeholder { color: #475569 !important; -webkit-text-fill-color: #475569 !important; opacity:1 !important; }
[data-testid="stSidebar"] [data-baseweb="select"] > div { background: rgba(255,255,255,.07) !important; color: #F1F5F9 !important; border: 1px solid rgba(148,163,184,.2) !important; border-radius: var(--radius-sm) !important; }
[data-testid="stSidebar"] [data-baseweb="select"] span,
[data-testid="stSidebar"] [data-baseweb="select"] div { color: #F1F5F9 !important; }
[data-testid="stSidebar"] [data-baseweb="select"] svg { color: #94A3B8 !important; fill: #94A3B8 !important; }
[data-testid="stSidebar"] [data-testid="stNumberInput"] button { background: rgba(255,255,255,.08) !important; color: #CBD5E1 !important; border-radius: 7px !important; }
[data-testid="stSidebar"] [data-testid="stSlider"] [role="slider"] { background: #3B82F6; }

/* ════════════════════════════════════════════════════════════
   MAIN-AREA INPUTS & DROPDOWNS — explicit light, beats BaseWeb
   ════════════════════════════════════════════════════════════ */
.main input, .main textarea,
[data-testid="stMain"] input, [data-testid="stMain"] textarea,
[data-testid="stAppViewContainer"] [data-testid="stMain"] [data-baseweb="input"] input,
[data-testid="stAppViewContainer"] [data-testid="stMain"] [data-baseweb="textarea"] textarea {
  background: #FFFFFF !important;
  color: #0C1322 !important;
  -webkit-text-fill-color: #0C1322 !important;
  caret-color: #2563EB !important;
  border: 1.5px solid #E2E8F0 !important;
  border-radius: 10px !important;
  font-family: 'DM Sans', sans-serif !important;
  font-size: 14px !important;
}
.main input:focus, .main textarea:focus { border-color: #2563EB !important; box-shadow: 0 0 0 3px rgba(37,99,235,.12) !important; outline: none !important; }
.main input::placeholder, .main textarea::placeholder { color:#94A3B8 !important; -webkit-text-fill-color:#94A3B8 !important; opacity:1 !important; }

/* SELECT / DROPDOWN — main area only — explicit light */
[data-testid="stMain"] [data-baseweb="select"] > div,
.main [data-baseweb="select"] > div {
  background: #FFFFFF !important;
  color: #0C1322 !important;
  border: 1.5px solid #E2E8F0 !important;
  border-radius: 10px !important;
}
[data-testid="stMain"] [data-baseweb="select"] span,
[data-testid="stMain"] [data-baseweb="select"] div,
.main [data-baseweb="select"] span,
.main [data-baseweb="select"] div { color: #0C1322 !important; -webkit-text-fill-color: #0C1322 !important; }
[data-testid="stMain"] [data-baseweb="select"] svg,
.main [data-baseweb="select"] svg { color: #64748B !important; fill: #64748B !important; }

/* DROPDOWN POPOVER (the menu that opens) */
[data-baseweb="popover"] [role="listbox"],
[data-baseweb="menu"] {
  background: #FFFFFF !important;
  border: 1px solid #E2E8F0 !important;
  border-radius: 10px !important;
  box-shadow: 0 8px 32px rgba(15,23,42,.10) !important;
}
[data-baseweb="popover"] [role="option"],
[data-baseweb="menu"] [role="option"] {
  background: #FFFFFF !important;
  color: #0C1322 !important;
  -webkit-text-fill-color: #0C1322 !important;
}
[data-baseweb="popover"] [role="option"]:hover,
[data-baseweb="menu"] [role="option"]:hover,
[data-baseweb="popover"] [role="option"][aria-selected="true"],
[data-baseweb="menu"] [role="option"][aria-selected="true"] {
  background: #EEF4FF !important;
  color: #1D4ED8 !important;
  -webkit-text-fill-color: #1D4ED8 !important;
}

/* NUMBER INPUT in main area */
.main [data-testid="stNumberInput"] input,
[data-testid="stMain"] [data-testid="stNumberInput"] input { background:#FFFFFF !important; color:#0C1322 !important; }
.main [data-testid="stNumberInput"] button,
[data-testid="stMain"] [data-testid="stNumberInput"] button { background:#F1F5F9 !important; color:#475569 !important; border:1px solid #E2E8F0 !important; }

/* CHECKBOX / RADIO labels in main area */
.main .stCheckbox label, .main .stRadio label { color: #0C1322 !important; }

/* ════════════════════════════════════════════════════════════
   FILE UPLOADER (light)
   ════════════════════════════════════════════════════════════ */
[data-testid="stFileUploader"] section,
[data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"],
[data-testid="stFileUploaderDropzone"],
[data-testid="stFileUploader"] > section > div {
  background: #FAFBFF !important;
  border: 1.5px dashed #C7D7F5 !important;
  border-radius: 16px !important;
}
[data-testid="stFileUploader"] section:hover { border-color: #2563EB !important; background: #EEF4FF !important; }
[data-testid="stFileUploader"] span,
[data-testid="stFileUploader"] p,
[data-testid="stFileUploader"] small { color: #64748B !important; -webkit-text-fill-color: #64748B !important; }
[data-testid="stFileUploader"] button {
  background: #FFFFFF !important; color: #0C1322 !important;
  -webkit-text-fill-color: #0C1322 !important;
  border: 1.5px solid #E2E8F0 !important;
  border-radius: 10px !important; font-weight: 600 !important;
}
[data-testid="stFileUploader"] [class*="uploadedFile"],
[data-testid="stFileUploader"] > div > div { background: #FAFBFF !important; color: #0C1322 !important; }

/* HELP ICON */
[data-testid="stTooltipIcon"] svg,
[data-testid="stTooltipHoverTarget"] svg { color: #94A3B8 !important; fill: #94A3B8 !important; }

/* ════════════════════════════════════════════════════════════
   BUTTONS — primary blue gradient, secondary white-with-border
   ════════════════════════════════════════════════════════════ */
.stButton > button, [data-testid="stFormSubmitButton"] > button {
  background: linear-gradient(135deg, #2563EB 0%, #3B82F6 100%) !important;
  color: #FFFFFF !important;
  -webkit-text-fill-color: #FFFFFF !important;
  border: none !important;
  border-radius: 10px !important;
  font-weight: 700 !important;
  font-size: 14px !important;
  font-family: 'DM Sans', sans-serif !important;
  box-shadow: 0 8px 24px rgba(37,99,235,.22) !important;
  padding: 0.55em 1.2em !important;
  transition: opacity .18s, transform .18s !important;
}
.stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover { opacity: 0.92 !important; transform: translateY(-1px) !important; }
.stButton > button[kind="secondary"] {
  background: #FFFFFF !important;
  color: #1E293B !important;
  -webkit-text-fill-color: #1E293B !important;
  border: 1.5px solid #E2E8F0 !important;
  box-shadow: 0 1px 4px rgba(15,23,42,.06) !important;
}
.stButton > button[kind="secondary"]:hover {
  border-color: #2563EB !important;
  color: #2563EB !important;
  -webkit-text-fill-color: #2563EB !important;
}

/* ════════════════════════════════════════════════════════════
   BRAND
   ════════════════════════════════════════════════════════════ */
.cf-brand { display:flex; gap:12px; align-items:center; padding:12px 0 20px; border-bottom:1px solid rgba(255,255,255,.07); margin-bottom:20px; }
.cf-logo { width:42px; height:42px; border-radius:13px; display:flex; align-items:center; justify-content:center; font-weight:800; font-size:15px; color:#fff; background: linear-gradient(135deg, #2563EB 0%, #06B6D4 100%); box-shadow: 0 6px 20px rgba(37,99,235,.40); }
.cf-brand-title { font-size:14.5px; font-weight:700; line-height:1.1; color:#F1F5F9; letter-spacing:-.01em; }
.cf-brand-sub { font-size:11px; color:#64748B; margin-top:2px; }
.cf-side-label { margin-top:20px; margin-bottom:6px; font-size:10px; letter-spacing:.12em; color:#475569; font-weight:700; text-transform:uppercase; }
.cf-side-note { padding:11px 13px; border-radius:12px; background: rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.07); color:#94A3B8; font-size:12px; line-height:1.6; }

/* ════════════════════════════════════════════════════════════
   HERO
   ════════════════════════════════════════════════════════════ */
.cf-hero { position:relative; overflow:hidden; padding:36px 38px; border-radius:28px; background: linear-gradient(135deg, #0A1628 0%, #162040 40%, #0C3254 70%, #0E3D5C 100%); color:#fff; margin-bottom:24px; box-shadow: 0 8px 32px rgba(15,23,42,.10), 0 24px 56px rgba(15,23,42,.07); }
.cf-hero:before { content:""; position:absolute; top:-60%; right:-10%; width:500px; height:500px; border-radius:999px; background: radial-gradient(circle, rgba(59,130,246,.30) 0%, transparent 65%); pointer-events:none; }
.cf-hero:after { content:""; position:absolute; bottom:-80px; right:60px; width:260px; height:260px; border-radius:999px; background: radial-gradient(circle, rgba(16,185,129,.22) 0%, transparent 65%); pointer-events:none; }
.cf-hero-content { position:relative; z-index:1; }
.cf-eyebrow { display:inline-flex; align-items:center; gap:7px; border:1px solid rgba(255,255,255,.15); background:rgba(255,255,255,.08); padding:6px 12px; border-radius:999px; font-size:11.5px; font-weight:700; color:#BFDBFE; margin-bottom:14px; letter-spacing:.03em; }
.cf-hero h1 { margin:0; font-size: clamp(28px, 3.5vw, 52px); letter-spacing:-.045em; line-height:1.04; color:#fff; font-weight:800; }
.cf-hero p { margin:14px 0 0; color:#93C5FD; font-size:16px; line-height:1.7; max-width:900px; font-weight:400; }
.cf-toolbar { display:flex; flex-wrap:wrap; gap:9px; margin-top:22px; }
.cf-chip { display:inline-flex; gap:6px; align-items:center; border-radius:999px; padding:7px 13px; font-size:12px; font-weight:600; background:#EEF4FF; color:#1D4ED8; border:1px solid #DBEAFE; }
.cf-chip-dark { background:rgba(255,255,255,.09); color:#E0F2FE; border-color:rgba(255,255,255,.14); }

/* ════════════════════════════════════════════════════════════
   CARDS
   ════════════════════════════════════════════════════════════ */
.cf-card { background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 22px; padding: 24px; box-shadow: 0 4px 16px rgba(15,23,42,.08), 0 12px 32px rgba(15,23,42,.05); margin-bottom: 18px; backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur); background: var(--glass-bg); }
.cf-card h3, .cf-section-title { font-size: 16px; margin: 0 0 8px; color: #0C1322; font-weight: 700; letter-spacing: -.025em; }
.cf-muted { color: #64748B; font-size: 13.5px; line-height: 1.65; }

/* METRICS */
.cf-grid { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:14px; margin-bottom:20px; }
.cf-metric { background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 22px; padding: 20px 22px; box-shadow: 0 1px 4px rgba(15,23,42,.06), 0 4px 12px rgba(15,23,42,.04); position: relative; overflow: hidden; transition: transform .18s, box-shadow .18s; }
.cf-metric:hover { transform: translateY(-2px); box-shadow: 0 4px 16px rgba(15,23,42,.08); }
.cf-metric:before { content:""; position:absolute; top:0; left:0; right:0; height:3px; background: linear-gradient(90deg, #2563EB, #06B6D4); border-radius: 22px 22px 0 0; }
.cf-metric-label { color:#94A3B8; text-transform:uppercase; letter-spacing:.08em; font-size:10.5px; font-weight:700; }
.cf-metric-value { color:#0C1322; font-size:32px; font-weight:800; letter-spacing:-.04em; margin-top:8px; line-height:1; }
.cf-metric-sub { color:#64748B; font-size:12px; margin-top:5px; }

/* HOW IT WORKS */
.cf-quick-grid { display:grid; grid-template-columns: 1.1fr 1fr; gap:18px; align-items:start; }
.cf-source-grid { display:grid; grid-template-columns: 1fr 1fr; gap:18px; }
.cf-command { border-radius: 28px; padding: 26px; background: linear-gradient(135deg, #FFFFFF 0%, #F8FAFD 100%); border: 1px solid #E2E8F0; box-shadow: 0 4px 16px rgba(15,23,42,.08); }
.cf-command-title { font-size:20px; font-weight:800; letter-spacing:-.04em; color:#0C1322; margin:0 0 6px; }
.cf-command-steps { display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:10px; margin-top:16px; }
.cf-step { padding:14px 16px; border-radius:16px; background:#F8FAFC; border:1px solid #E2E8F0; transition: background .18s, border-color .18s; }
.cf-step:hover { background:#EEF4FF; border-color:#BFDBFE; }
.cf-step-num { width:28px; height:28px; border-radius:9px; display:inline-flex; align-items:center; justify-content:center; background: linear-gradient(135deg, #2563EB, #3B82F6); color:white; font-weight:800; font-size:12px; margin-bottom:9px; box-shadow: 0 3px 8px rgba(37,99,235,.3); }
.cf-step-title { font-size:13px; font-weight:700; color:#0C1322; }
.cf-step-desc { font-size:12px; color:#64748B; line-height:1.5; margin-top:4px; }

/* JOB CARDS */
.cf-job { border-radius: 28px; background: #FFFFFF; border: 1px solid #E2E8F0; padding: 22px 24px; margin-bottom:14px; box-shadow: 0 1px 4px rgba(15,23,42,.06), 0 4px 12px rgba(15,23,42,.04); transition: box-shadow .18s, border-color .18s, transform .18s; backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur); }
.cf-job:hover { box-shadow: 0 4px 16px rgba(15,23,42,.08); border-color: #BFDBFE; transform: translateY(-1px); }
.cf-job-top { display:flex; gap:16px; align-items:flex-start; justify-content:space-between; }
.cf-company { display:flex; gap:14px; align-items:flex-start; }
.cf-company-logo { width:46px; height:46px; border-radius:14px; flex-shrink:0; display:flex; align-items:center; justify-content:center; background: linear-gradient(135deg, #2563EB, #06B6D4); color:#fff; font-weight:800; font-size:15px; box-shadow: 0 6px 16px rgba(37,99,235,.25); }
.cf-job-title { font-size:17px; font-weight:700; letter-spacing:-.025em; color:#0C1322; margin-bottom:4px; }
.cf-job-meta { color:#64748B; font-size:13px; display:flex; flex-wrap:wrap; gap:7px; align-items:center; }
.cf-score { min-width:76px; text-align:center; border-radius:14px; padding:10px 14px; font-size:19px; font-weight:800; color:#065F46; background:linear-gradient(135deg,#D1FAE5,#A7F3D0); border:1px solid #6EE7B7; box-shadow: 0 4px 12px rgba(16,185,129,.15); }
.cf-score-mid { color:#92400E; background:linear-gradient(135deg,#FEF3C7,#FDE68A); border-color:#FCD34D; box-shadow: 0 4px 12px rgba(245,158,11,.15); }
.cf-score-low { color:#991B1B; background:linear-gradient(135deg,#FEE2E2,#FECACA); border-color:#FCA5A5; box-shadow: 0 4px 12px rgba(244,63,94,.12); }
.cf-progress { height:6px; background:#F1F5F9; border-radius:999px; overflow:hidden; margin:16px 0 12px; }
.cf-progress span { display:block; height:100%; border-radius:999px; background:linear-gradient(90deg,#2563EB,#06B6D4,#10B981); }

/* TAGS */
.cf-tags { display:flex; gap:7px; flex-wrap:wrap; margin-top:12px; }
.cf-tag { border-radius:999px; padding:5px 11px; font-size:11.5px; font-weight:600; background:#F1F5F9; color:#475569; border:1px solid #E2E8F0; }
.cf-tag-green { background:#ECFDF5; color:#047857; border-color:#A7F3D0; }
.cf-tag-purple { background:#F5F3FF; color:#6D28D9; border-color:#DDD6FE; }
.cf-tag-blue { background:#EFF6FF; color:#1D4ED8; border-color:#BFDBFE; }

/* ALERTS */
.cf-alert { padding:13px 16px; border-radius:16px; font-size:13.5px; line-height:1.6; margin:10px 0; }
.cf-alert-info { background:#EFF6FF; color:#1E40AF; border:1px solid #BFDBFE; }
.cf-alert-success { background:#ECFDF5; color:#047857; border:1px solid #A7F3D0; }
.cf-alert-warn { background:#FFFBEB; color:#92400E; border:1px solid #FDE68A; }
.cf-alert-error { background:#FEF2F2; color:#991B1B; border:1px solid #FECACA; }

/* PLATFORM BADGES */
.cf-platform { display:inline-flex; align-items:center; gap:6px; padding:5px 10px; border-radius:999px; font-weight:700; font-size:11px; letter-spacing:.02em; background:#1E293B; color:#E2E8F0; }
.cf-platform.workday { background:#312E81; color:#C7D2FE; }
.cf-platform.greenhouse { background:#064E3B; color:#A7F3D0; }
.cf-platform.ashby { background:#78350F; color:#FDE68A; }
.cf-platform.lever { background:#0C4A6E; color:#BAE6FD; }
.cf-platform.smartrecruiters { background:#881337; color:#FECDD3; }
.cf-platform.amazon { background:#111827; color:#E5E7EB; }
.cf-platform.microsoft { background:#1D4ED8; color:#DBEAFE; }
.cf-platform.custom { background:#374151; color:#D1D5DB; }
.cf-platform.auto { background:#1E293B; color:#CBD5E1; }

/* MINI LIST */
.cf-mini-list { display:flex; flex-direction:column; gap:9px; }
.cf-mini-row { padding:12px 14px; border-radius:16px; border:1px solid #E2E8F0; background:#FFFFFF; display:flex; justify-content:space-between; gap:12px; align-items:center; }
.cf-mini-row:hover { border-color:#BFDBFE; box-shadow: 0 4px 12px rgba(37,99,235,.08); }
.cf-role { font-weight:700; font-size:13px; color:#1E293B; }
.cf-role-evidence { font-size:12px; color:#64748B; margin-top:2px; }
.cf-pill-score { border-radius:999px; padding:4px 10px; font-size:12px; font-weight:700; background:#DCFCE7; color:#166534; }

/* TYPOGRAPHY */
.cf-subtitle { font-size:24px; font-weight:800; letter-spacing:-.04em; margin:28px 0 14px; color:#0C1322; }
.cf-small { font-size:12px; color:#64748B; }

/* TOGGLES & SLIDERS */
[data-testid="stToggle"] [role="switch"][aria-checked="true"] { background: #2563EB !important; }
[data-testid="stSlider"] div[role="slider"] { background: #2563EB !important; }

/* ════════════════════════════════════════════════════════════
   STREAMLIT NATIVE ALERTS — st.info / st.success / st.warning / st.error
   Force readable text. Streamlit's defaults break in mixed-theme browsers.
   ════════════════════════════════════════════════════════════ */
[data-testid="stAlert"] {
  border-radius: 14px !important;
  padding: 14px 18px !important;
  margin: 10px 0 !important;
  border: 1px solid !important;
  box-shadow: none !important;
}
/* Info (blue) */
[data-testid="stAlertContentInfo"],
[data-baseweb="notification"][kind="info"] {
  background-color: #EFF6FF !important;
  border-color: #BFDBFE !important;
}
[data-testid="stAlertContentInfo"] *,
[data-baseweb="notification"][kind="info"] * {
  color: #1E40AF !important;
  -webkit-text-fill-color: #1E40AF !important;
  fill: #1E40AF !important;
}
/* Success (green) */
[data-testid="stAlertContentSuccess"],
[data-baseweb="notification"][kind="positive"] {
  background-color: #ECFDF5 !important;
  border-color: #A7F3D0 !important;
}
[data-testid="stAlertContentSuccess"] *,
[data-baseweb="notification"][kind="positive"] * {
  color: #047857 !important;
  -webkit-text-fill-color: #047857 !important;
  fill: #047857 !important;
}
/* Warning (yellow) — THIS is the one that was unreadable */
[data-testid="stAlertContentWarning"],
[data-baseweb="notification"][kind="warning"] {
  background-color: #FFFBEB !important;
  border-color: #FDE68A !important;
}
[data-testid="stAlertContentWarning"] *,
[data-baseweb="notification"][kind="warning"] * {
  color: #92400E !important;
  -webkit-text-fill-color: #92400E !important;
  fill: #92400E !important;
}
/* Error (red) */
[data-testid="stAlertContentError"],
[data-baseweb="notification"][kind="negative"] {
  background-color: #FEF2F2 !important;
  border-color: #FECACA !important;
}
[data-testid="stAlertContentError"] *,
[data-baseweb="notification"][kind="negative"] * {
  color: #991B1B !important;
  -webkit-text-fill-color: #991B1B !important;
  fill: #991B1B !important;
}
/* Links inside alerts stay readable */
[data-testid="stAlert"] a { text-decoration: underline !important; }

/* RESPONSIVE */
@media (max-width: 1180px) { .cf-grid, .cf-command-steps { grid-template-columns:1fr 1fr; } .cf-quick-grid, .cf-source-grid { grid-template-columns:1fr; } }
@media (max-width: 760px) { .block-container { padding-left:1rem; padding-right:1rem; } .cf-grid, .cf-command-steps { grid-template-columns:1fr; } .cf-hero { padding:24px; border-radius:22px; } .cf-job-top { flex-direction:column; } }

/* ════════════════════════════════════════════════════════════
   GLASS MORPHISM
   ════════════════════════════════════════════════════════════ */
.cf-glass {
  backdrop-filter: var(--blur);
  -webkit-backdrop-filter: var(--blur);
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--glass-shadow);
}
.cf-glass-dark {
  backdrop-filter: var(--blur);
  -webkit-backdrop-filter: var(--blur);
  background: rgba(8,16,30,0.65);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: var(--radius-lg);
}

/* ════════════════════════════════════════════════════════════
   SECTION HEADER
   ════════════════════════════════════════════════════════════ */
.cf-section-header {
  position: relative;
  padding: 28px 32px;
  border-radius: var(--radius-xl);
  background: linear-gradient(135deg, #0A1628 0%, #162040 50%, #0C3254 100%);
  color: #fff;
  margin: 28px 0 20px;
  overflow: hidden;
}
.cf-section-header:before {
  content: "";
  position: absolute;
  top: -40px; right: -20px;
  width: 200px; height: 200px;
  border-radius: 999px;
  background: radial-gradient(circle, rgba(59,130,246,.25) 0%, transparent 65%);
  pointer-events: none;
}
.cf-section-header h2 {
  margin: 0 0 6px; font-size: 22px; font-weight: 800;
  letter-spacing: -.04em; color: #fff;
  position: relative; z-index: 1;
}
.cf-section-header p {
  margin: 0; color: #93C5FD; font-size: 14px;
  position: relative; z-index: 1;
}

/* ════════════════════════════════════════════════════════════
   TAB BUTTON ROW
   ════════════════════════════════════════════════════════════ */
.cf-tab-btn-row {
  display: flex; gap: 10px; margin: 20px 0 24px; flex-wrap: wrap;
}

/* ════════════════════════════════════════════════════════════
   STATUS BADGES
   ════════════════════════════════════════════════════════════ */
.cf-badge-applied {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 5px 12px; border-radius: 999px;
  background: #ECFDF5; color: #047857;
  border: 1px solid #A7F3D0;
  font-size: 12px; font-weight: 700;
}
.cf-badge-f1-ok {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 5px 12px; border-radius: 999px;
  background: #EFF6FF; color: #1D4ED8;
  border: 1px solid #BFDBFE;
  font-size: 12px; font-weight: 700;
}
.cf-badge-f1-skip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 5px 12px; border-radius: 999px;
  background: #FEF2F2; color: #991B1B;
  border: 1px solid #FECACA;
  font-size: 12px; font-weight: 700;
}

/* ════════════════════════════════════════════════════════════
   PROFILE SECTION CARD
   ════════════════════════════════════════════════════════════ */
.cf-profile-section {
  background: var(--glass-bg);
  backdrop-filter: var(--blur);
  -webkit-backdrop-filter: var(--blur);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-lg);
  padding: 22px 24px;
  margin-bottom: 18px;
  box-shadow: var(--glass-shadow);
}
.cf-profile-section h4 {
  margin: 0 0 16px; font-size: 14px; font-weight: 700;
  color: var(--blue); text-transform: uppercase;
  letter-spacing: .06em;
}

/* ════════════════════════════════════════════════════════════
   HERO EXTRA ORB
   ════════════════════════════════════════════════════════════ */
.cf-hero-orb-left {
  position: absolute; bottom: -30px; left: 40px;
  width: 180px; height: 180px; border-radius: 999px;
  background: radial-gradient(circle, rgba(16,185,129,.18) 0%, transparent 65%);
  pointer-events: none;
}

/* ════════════════════════════════════════════════════════════
   AMBIENT ANIMATION
   ════════════════════════════════════════════════════════════ */
@keyframes ambient-shift {
  from { opacity: 0.85; }
  to { opacity: 1; }
}
.cf-hero { animation: ambient-shift 8s ease-in-out infinite alternate; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

DARK_MODE_CSS = r"""
<style>
/* ════════════════════════════════════════════════════════════
   DARK THEME — overrides everything light when toggle is ON
   ════════════════════════════════════════════════════════════ */
:root { color-scheme: dark !important; }
html, body { background: #0A0F1A !important; color: #E2E8F0 !important; }

.stApp,
[data-testid="stAppViewContainer"],
[data-testid="stMain"],
.main {
  background: linear-gradient(180deg, #0A0F1A 0%, #0F1729 60%, #0A0F1A 100%) !important;
  color: #E2E8F0 !important;
}

/* Cards */
.cf-card, .cf-metric, .cf-command, .cf-job, .cf-mini-row, .cf-step {
  background: #131C2F !important;
  border-color: rgba(148,163,184,.16) !important;
  color: #E2E8F0 !important;
  box-shadow: 0 8px 24px rgba(0,0,0,.35) !important;
}

/* Hover states keep working */
.cf-job:hover { border-color: rgba(96,165,250,.40) !important; box-shadow: 0 12px 32px rgba(0,0,0,.5) !important; }
.cf-step:hover { background: #1B2640 !important; border-color: rgba(96,165,250,.30) !important; }

/* Headings & body text */
.cf-card h3, .cf-section-title, .cf-job-title, .cf-metric-value,
.cf-role, .cf-subtitle, .cf-command-title, .cf-step-title,
h1, h2, h3, h4, h5, h6 { color: #F8FAFC !important; }
.cf-muted, .cf-job-meta, .cf-metric-sub, .cf-role-evidence,
.cf-metric-label, .cf-step-desc, .cf-small { color: #94A3B8 !important; }

/* Hero stays dark gradient — already works */

/* Progress / dividers */
.cf-progress { background: rgba(148,163,184,.18) !important; }

/* Tags — softer in dark */
.cf-tag { background: rgba(30,41,59,.7) !important; color: #CBD5E1 !important; border-color: rgba(148,163,184,.20) !important; }
.cf-tag-green { background: rgba(6,95,70,.30) !important; color: #86EFAC !important; border-color: rgba(134,239,172,.30) !important; }
.cf-tag-blue { background: rgba(37,99,235,.25) !important; color: #BFDBFE !important; border-color: rgba(147,197,253,.30) !important; }
.cf-tag-purple { background: rgba(109,40,217,.28) !important; color: #DDD6FE !important; border-color: rgba(196,181,253,.30) !important; }

/* Alerts */
.cf-alert-info { background: rgba(30,64,175,.22) !important; color: #BFDBFE !important; border-color: rgba(147,197,253,.30) !important; }
.cf-alert-success { background: rgba(4,120,87,.22) !important; color: #A7F3D0 !important; border-color: rgba(167,243,208,.30) !important; }
.cf-alert-warn { background: rgba(146,64,14,.25) !important; color: #FDE68A !important; border-color: rgba(253,230,138,.30) !important; }
.cf-alert-error { background: rgba(153,27,27,.25) !important; color: #FECACA !important; border-color: rgba(254,202,202,.30) !important; }

/* Inputs in main area */
.main input, .main textarea,
[data-testid="stMain"] input, [data-testid="stMain"] textarea,
[data-testid="stAppViewContainer"] [data-testid="stMain"] [data-baseweb="input"] input,
[data-testid="stAppViewContainer"] [data-testid="stMain"] [data-baseweb="textarea"] textarea {
  background: #0E1726 !important;
  color: #F1F5F9 !important;
  -webkit-text-fill-color: #F1F5F9 !important;
  caret-color: #60A5FA !important;
  border-color: rgba(148,163,184,.18) !important;
}
.main input::placeholder, .main textarea::placeholder { color: #64748B !important; -webkit-text-fill-color: #64748B !important; }

/* Selects in main area */
[data-testid="stMain"] [data-baseweb="select"] > div,
.main [data-baseweb="select"] > div {
  background: #0E1726 !important;
  color: #F1F5F9 !important;
  border-color: rgba(148,163,184,.18) !important;
}
[data-testid="stMain"] [data-baseweb="select"] span,
[data-testid="stMain"] [data-baseweb="select"] div,
.main [data-baseweb="select"] span,
.main [data-baseweb="select"] div { color: #F1F5F9 !important; -webkit-text-fill-color: #F1F5F9 !important; }
[data-testid="stMain"] [data-baseweb="select"] svg,
.main [data-baseweb="select"] svg { color: #94A3B8 !important; fill: #94A3B8 !important; }

/* Dropdown popover */
[data-baseweb="popover"] [role="listbox"], [data-baseweb="menu"] {
  background: #131C2F !important;
  border-color: rgba(148,163,184,.20) !important;
  box-shadow: 0 12px 32px rgba(0,0,0,.5) !important;
}
[data-baseweb="popover"] [role="option"], [data-baseweb="menu"] [role="option"] {
  background: #131C2F !important;
  color: #E2E8F0 !important;
  -webkit-text-fill-color: #E2E8F0 !important;
}
[data-baseweb="popover"] [role="option"]:hover,
[data-baseweb="menu"] [role="option"]:hover,
[data-baseweb="popover"] [role="option"][aria-selected="true"],
[data-baseweb="menu"] [role="option"][aria-selected="true"] {
  background: rgba(37,99,235,.20) !important;
  color: #BFDBFE !important;
  -webkit-text-fill-color: #BFDBFE !important;
}

/* File uploader */
[data-testid="stFileUploader"] section,
[data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"],
[data-testid="stFileUploader"] > section > div {
  background: #0E1726 !important;
  border-color: rgba(148,163,184,.20) !important;
}
[data-testid="stFileUploader"] section:hover { border-color: rgba(96,165,250,.40) !important; background: rgba(37,99,235,.08) !important; }
[data-testid="stFileUploader"] span,
[data-testid="stFileUploader"] p,
[data-testid="stFileUploader"] small { color: #94A3B8 !important; -webkit-text-fill-color: #94A3B8 !important; }
[data-testid="stFileUploader"] button {
  background: #131C2F !important;
  color: #E2E8F0 !important;
  -webkit-text-fill-color: #E2E8F0 !important;
  border-color: rgba(148,163,184,.20) !important;
}

/* Secondary buttons */
.stButton > button[kind="secondary"] {
  background: #131C2F !important;
  color: #E2E8F0 !important;
  -webkit-text-fill-color: #E2E8F0 !important;
  border-color: rgba(148,163,184,.20) !important;
}
.stButton > button[kind="secondary"]:hover {
  border-color: #60A5FA !important;
  color: #BFDBFE !important;
  -webkit-text-fill-color: #BFDBFE !important;
}

/* Number input controls */
.main [data-testid="stNumberInput"] button,
[data-testid="stMain"] [data-testid="stNumberInput"] button {
  background: #1B2640 !important; color: #CBD5E1 !important; border-color: rgba(148,163,184,.20) !important;
}

/* Checkbox/radio labels */
.main .stCheckbox label, .main .stRadio label { color: #E2E8F0 !important; }

/* HTML company table generated via st.markdown */
.main table { background: #131C2F !important; border-color: rgba(148,163,184,.20) !important; }
.main table th { background: #0E1726 !important; color: #94A3B8 !important; }
.main table td { color: #E2E8F0 !important; border-color: rgba(148,163,184,.10) !important; }

/* Streamlit native alerts — dark mode */
[data-testid="stAlertContentInfo"],
[data-baseweb="notification"][kind="info"] {
  background-color: rgba(30,64,175,.22) !important;
  border-color: rgba(147,197,253,.30) !important;
}
[data-testid="stAlertContentInfo"] *,
[data-baseweb="notification"][kind="info"] * {
  color: #BFDBFE !important; -webkit-text-fill-color: #BFDBFE !important; fill: #BFDBFE !important;
}
[data-testid="stAlertContentSuccess"],
[data-baseweb="notification"][kind="positive"] {
  background-color: rgba(4,120,87,.22) !important;
  border-color: rgba(167,243,208,.30) !important;
}
[data-testid="stAlertContentSuccess"] *,
[data-baseweb="notification"][kind="positive"] * {
  color: #A7F3D0 !important; -webkit-text-fill-color: #A7F3D0 !important; fill: #A7F3D0 !important;
}
[data-testid="stAlertContentWarning"],
[data-baseweb="notification"][kind="warning"] {
  background-color: rgba(146,64,14,.30) !important;
  border-color: rgba(253,230,138,.30) !important;
}
[data-testid="stAlertContentWarning"] *,
[data-baseweb="notification"][kind="warning"] * {
  color: #FDE68A !important; -webkit-text-fill-color: #FDE68A !important; fill: #FDE68A !important;
}
[data-testid="stAlertContentError"],
[data-baseweb="notification"][kind="negative"] {
  background-color: rgba(153,27,27,.28) !important;
  border-color: rgba(254,202,202,.30) !important;
}
[data-testid="stAlertContentError"] *,
[data-baseweb="notification"][kind="negative"] * {
  color: #FECACA !important; -webkit-text-fill-color: #FECACA !important; fill: #FECACA !important;
}

/* ════════════════════════════════════════════════════════════
   DARK MODE — glass overrides
   ════════════════════════════════════════════════════════════ */
.cf-glass, .cf-card, .cf-job, .cf-command {
  background: rgba(19,28,47,0.82) !important;
  border-color: rgba(148,163,184,.15) !important;
  backdrop-filter: var(--blur);
  -webkit-backdrop-filter: var(--blur);
}
.cf-profile-section {
  background: rgba(14,23,38,0.75) !important;
  border-color: rgba(148,163,184,.12) !important;
}
.cf-section-header { /* already dark, no change needed */ }
</style>
"""


def init_state() -> None:
    defaults = {
        "profile": RuntimeProfile(),
        "matches": [],
        "jobs": [],
        "run_log": [],
        "user_companies": [],
        "last_scan_label": "Not run yet",
        "last_scan_seconds": None,
        "last_profile_seconds": None,
        "platform_summary": {},
        "source_stats": [],
        "profile_url_blob": "",
        "company_name_input": "",
        "company_url_input": "",
        "active_tab": "profile",
        "applied_job_ids": set(),
        "qa_answers": [],
        "db_profile": {},
        "profile_form_draft": {},
        "_db_loaded": False,
        "education_entries": [],
        "experience_entries": [],
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    # Load DB-backed state if not already loaded
    try:
        from careerfit import db as _db
        if not st.session_state.get("_db_loaded"):
            _prof = _db.load_profile()
            if _prof:
                st.session_state.db_profile = _prof
                st.session_state.active_tab = "matching"
            st.session_state.applied_job_ids = _db.get_applied_job_ids()
            st.session_state.qa_answers = _db.load_qa_answers()
            st.session_state["_db_loaded"] = True
    except Exception:
        pass
    # Initialize education/experience entries from saved profile
    _prof_for_edu = st.session_state.get("db_profile", {})
    if not st.session_state.get("education_entries"):
        _saved_edu = _prof_for_edu.get("education_entries", [])
        if isinstance(_saved_edu, list) and _saved_edu:
            st.session_state.education_entries = _saved_edu
        elif _prof_for_edu.get("university") or _prof_for_edu.get("degree"):
            st.session_state.education_entries = [{"school": _prof_for_edu.get("university", ""), "degree": _prof_for_edu.get("degree", ""), "major": "", "gpa": str(_prof_for_edu.get("gpa", "") or ""), "start_year": "", "end_year": str(_prof_for_edu.get("graduation_year", "") or ""), "is_current": False}]
    if not st.session_state.get("experience_entries"):
        _saved_exp = _prof_for_edu.get("experience_entries", [])
        if isinstance(_saved_exp, list) and _saved_exp:
            st.session_state.experience_entries = _saved_exp


def log(message: str) -> None:
    st.session_state.run_log.insert(0, f"{time.strftime('%H:%M:%S')}  {message}")
    st.session_state.run_log = st.session_state.run_log[:140]


@st.cache_data(show_spinner=False, ttl=120)
def get_default_companies() -> list[dict]:
    try:
        return load_companies(str(ROOT / "config" / "companies.yaml"))
    except Exception:
        return []


def load_persisted_companies() -> list[dict]:
    try:
        if RUNTIME_COMPANIES_PATH.exists():
            payload = json.loads(RUNTIME_COMPANIES_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                return payload
    except Exception:
        pass
    return []


def save_persisted_companies(companies: list[dict]) -> None:
    try:
        RUNTIME_COMPANIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUNTIME_COMPANIES_PATH.write_text(json.dumps(companies, indent=2), encoding="utf-8")
    except Exception as exc:
        log(f"Could not persist company sources: {exc}")


init_state()
if not st.session_state.user_companies:
    st.session_state.user_companies = load_persisted_companies()


def all_companies() -> list[dict]:
    merged = get_default_companies() + st.session_state.user_companies
    seen: set[str] = set()
    deduped: list[dict] = []
    for company in merged:
        key = f"{str(company.get('name','')).strip().lower()}|{str(company.get('careers_url','')).strip().lower()}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(company)
    return deduped


def company_initial(company: str) -> str:
    parts = [p for p in str(company).split() if p]
    if not parts:
        return "CF"
    return "".join(p[0].upper() for p in parts[:2])


def pct(value: float) -> int:
    return int(round(float(value) * 100))


def score_class(value: float, threshold: float) -> str:
    if value >= threshold:
        return ""
    if value >= max(0.65, threshold - 0.15):
        return "cf-score-mid"
    return "cf-score-low"


def platform_badge(source: str) -> str:
    s = (source or "custom").lower().strip()
    return f"<span class='cf-platform {escape(s)}'>{escape(s.upper())}</span>"


def matches_dataframe(matches: Iterable[JobMatch]) -> pd.DataFrame:
    rows = []
    for m in matches:
        rows.append({
            "Score": pct(m.score),
            "Decision": m.decision,
            "Company": m.company,
            "Title": m.title,
            "Location": m.location,
            "Platform": m.source,
            "Role family": m.role_family,
            "Best document": m.best_document,
            "Reason": m.reason_summary,
            "URL": m.canonical_url,
        })
    return pd.DataFrame(rows)


def job_dataframe(jobs: Iterable[NormalizedJob]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Company": j.company,
        "Title": j.title,
        "Location": j.location,
        "Platform": j.source,
        "URL": j.canonical_url,
    } for j in jobs])


def render_metric(label: str, value: str, sub: str = "") -> None:
    st.markdown(
        f"<div class='cf-metric'><div class='cf-metric-label'>{escape(label)}</div><div class='cf-metric-value'>{escape(value)}</div><div class='cf-metric-sub'>{escape(sub)}</div></div>",
        unsafe_allow_html=True,
    )


def render_role_suggestions(profile: RuntimeProfile) -> None:
    if not profile.suggested_roles:
        st.markdown("<div class='cf-alert cf-alert-warn'>Build a profile to generate role targets from the uploaded sources.</div>", unsafe_allow_html=True)
        return
    st.markdown("<div class='cf-mini-list'>", unsafe_allow_html=True)
    for item in profile.suggested_roles[:8]:
        role = str(item.get("role", "Role"))
        ev = ", ".join(str(x) for x in item.get("evidence", [])[:5])
        sc = int(float(item.get("score", 0)) * 100)
        st.markdown(
            f"<div class='cf-mini-row'><div><div class='cf-role'>🎯 {escape(role)}</div><div class='cf-role-evidence'>{escape(ev)}</div></div><div class='cf-pill-score'>{sc}%</div></div>",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)


def render_match_card(m: JobMatch, threshold: float, applied_job_ids: "set[str] | None" = None) -> None:
    score = pct(m.score)
    widths = min(100, max(2, score))
    strengths = "".join(f"<span class='cf-tag cf-tag-green'>{escape(s)}</span>" for s in m.matched_strengths[:5])
    concerns = "".join(f"<span class='cf-tag'>{escape(s)}</span>" for s in m.gaps_or_concerns[:3])
    url = escape(m.canonical_url or "#")
    profile_ready = bool(getattr(m, "profile_ready", False))
    score_css = score_class(m.score, threshold) if profile_ready else "cf-score-low"
    source_label = escape(m.best_document or ("Profile not built" if not profile_ready else "Combined profile"))
    # Build SVG ring
    _arc = round((score / 100) * 163.36, 2)
    _ring_color = "#10B981" if score >= 90 else ("#F59E0B" if score >= 70 else "#F43F5E")
    _search_note = "" if profile_ready else "<tspan x='32' dy='10' font-size='7' font-weight='600' fill='#94A3B8'>search</tspan>"
    _score_svg = (
        "<svg width='64' height='64' viewBox='0 0 64 64' xmlns='http://www.w3.org/2000/svg'>"
        "<circle cx='32' cy='32' r='26' fill='none' stroke='#E2E8F0' stroke-width='6'/>"
        f"<circle cx='32' cy='32' r='26' fill='none' stroke='{_ring_color}' stroke-width='6' "
        f"stroke-dasharray='{_arc} 163.36' stroke-linecap='round' transform='rotate(-90 32 32)'/>"
        f"<text x='32' y='36' text-anchor='middle' font-size='13' font-weight='800' "
        f"fill='{_ring_color}' font-family='DM Sans,sans-serif'>{score}%"
        + _search_note +
        "</text></svg>"
    )
    st.markdown(
        f"""
        <div class='cf-job'>
          <div class='cf-job-top'>
            <div class='cf-company'>
              <div class='cf-company-logo'>{escape(company_initial(m.company))}</div>
              <div>
                <div class='cf-job-title'>{escape(m.title)}</div>
                <div class='cf-job-meta'>
                  <span>{escape(m.company)}</span><span>•</span><span>{escape(m.location or 'Location not listed')}</span><span>•</span>{platform_badge(m.source)}
                </div>
              </div>
            </div>
            {_score_svg}
          </div>
          <div class='cf-progress'><span style='width:{widths}%'></span></div>
          <div class='cf-muted'><b style='color:inherit'>Role alignment:</b> {escape(m.role_family or 'General fit')}</div>
          <div class='cf-muted' style='margin-top:8px'>{escape(m.reason_summary)}</div>
          <div class='cf-tags'>{strengths}</div>
          <div class='cf-tags'>{concerns}</div>
          <div style='display:flex;gap:10px;align-items:center;margin-top:16px;flex-wrap:wrap'>
            <a href='{url}' target='_blank' style='text-decoration:none;background:#2563EB;color:white;padding:10px 14px;border-radius:12px;font-weight:900;font-size:13px'>Open role</a>
            <span class='cf-tag cf-tag-purple'>Profile evidence: {source_label}</span>
            <span class='cf-tag cf-tag-blue'>Decision: {escape(m.decision.replace('_', ' ').title())}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    # Applied badge
    _job_id = m.raw_job.get("external_job_id","") or ""
    _already = applied_job_ids and (m.canonical_url in applied_job_ids or _job_id in applied_job_ids)
    _f1_friendly = m.raw_job.get("f1_friendly", False)
    _f1_blocked = m.raw_job.get("f1_blocked", False)
    _badges = ""
    if _already:
        _badges += "<span class='cf-badge-applied'>&#x2713; Already Applied</span> "
    if _f1_friendly:
        _badges += "<span class='cf-badge-f1-ok'>&#x1F7E2; F-1 Friendly</span> "
    if _f1_blocked:
        _badges += "<span class='cf-badge-f1-skip'>&#x1F534; Clearance/No Sponsor</span> "
    # Auto-Apply Eligible badge
    _auto_eligible = (m.score >= 0.90 and m.decision not in ("f1_filtered", "role_filtered") and not _already)
    if _auto_eligible:
        _badges += "<span style='background:#DCFCE7;color:#166534;border-radius:20px;padding:3px 10px;font-size:11px;font-weight:700'>&#x26a1; Auto-Apply Eligible</span> "
    if _badges:
        st.markdown(f"<div style='margin-top:8px'>{_badges}</div>", unsafe_allow_html=True)
    if not _already:
        render_apply_button(m)
    else:
        st.markdown("<span style='font-size:12px;color:#10B981;font-weight:700'>&#x2713; Already applied</span>", unsafe_allow_html=True)


def selected_companies_for_run(scan_all_companies: bool, company_limit: int) -> list[dict]:
    companies = all_companies()
    if scan_all_companies:
        return companies
    return companies[: int(company_limit)]


def build_profile_action(url_blob: str, uploaded_files) -> bool:
    urls = [u.strip() for u in (url_blob or "").splitlines() if u.strip()]
    max_docs = int(os.getenv("CAREERFIT_MAX_PROFILE_DOCS", "20"))
    files = []
    for f in list(uploaded_files or [])[:max_docs]:
        files.append((f.name, f.getvalue()))
    if not urls and not files:
        st.warning("Add at least one public website URL or upload a resume/CV to build a profile.")
        return False
    start = time.perf_counter()
    with st.spinner("Building profile intelligence from websites and documents..."):
        profile = build_runtime_profile(urls, files)
    elapsed = time.perf_counter() - start
    st.session_state.profile = profile
    st.session_state.last_profile_seconds = elapsed
    parsed = sum(1 for s in profile.sources if s.status == "parsed")
    errors = sum(1 for s in profile.sources if s.status != "parsed")
    if profile.combined_text.strip():
        st.success(f"Profile built from {parsed} source(s) in {elapsed:.1f}s.")
        ok = True
    else:
        st.error("No readable profile content was extracted. Upload a text-readable PDF/DOCX or add a public website URL.")
        ok = False
    if errors:
        st.warning(f"{errors} source(s) could not be parsed. Open the diagnostics expander below for details.")
    log(f"Profile build completed: {parsed} parsed, {errors} issue(s), {elapsed:.1f}s")
    return ok


def prepare_company_for_run(company: dict, search_text: str, location_mode: str, include_unknown_locations: bool, fast_mode: bool) -> dict:
    prepared = json.loads(json.dumps(company))
    ats = prepared.setdefault("ats", {})
    if search_text:
        ats["search_text"] = search_text
    ats["us_only"] = location_mode == "United States / Remote only"
    ats["include_remote"] = True
    ats["include_unknown_locations"] = include_unknown_locations
    ats["fetch_details"] = not fast_mode
    return prepared


def run_scan_action(
    threshold: float,
    search_text: str,
    location_mode: str,
    include_unknown_locations: bool,
    fast_mode: bool,
    use_cache: bool,
    allow_unprofiled_scan: bool,
    scan_all_companies: bool,
    company_limit: int,
    parallel_workers: int,
    max_years_experience: int = 10,
    f1_filter: bool = True,
    role_type_filter: bool = True,
) -> bool:
    total_available = len(all_companies())
    companies = selected_companies_for_run(scan_all_companies, company_limit)
    if not companies:
        st.error("Add at least one company source first.")
        return False
    if not st.session_state.profile.combined_text.strip() and not allow_unprofiled_scan:
        st.error("Build a profile first. Personalized relevance requires resume/website evidence.")
        st.info("Use the one-click button after adding sources, or enable 'Allow non-personalized search' for a non-personalized search score.")
        log("Scan blocked: profile is empty.")
        return False

    if not scan_all_companies and len(companies) < total_available:
        log(f"Partial scan selected: {len(companies)} of {total_available} source(s)")
    else:
        log(f"Full scan selected: {len(companies)} source(s)")

    start = time.perf_counter()
    jobs: list[NormalizedJob] = []
    platform_counts: dict[str, int] = {}
    source_stats: list[dict] = []
    errors: list[str] = []

    def fetch_company(index: int, company: dict) -> tuple[int, str, list[NormalizedJob], str, str | None]:
        prepared = prepare_company_for_run(company, search_text, location_mode, include_unknown_locations, fast_mode)
        cname = prepared.get("name", "Company")
        try:
            source_preview = expand_company_sources(prepared)
            platform_preview = ", ".join(sorted({(s.get("ats") or {}).get("type", "custom") for s in source_preview})) or "custom"
            fetched = fetch_jobs_for_company(prepared, use_cache=use_cache)
            return index, cname, fetched, platform_preview, None
        except Exception as exc:
            return index, cname, [], "unknown", str(exc)

    with st.spinner("Fetching open roles and ranking matches..."):
        max_workers = max(1, min(int(parallel_workers), len(companies), 12))
        progress = st.progress(0, text="Preparing company-source fetches...")
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(fetch_company, idx, company) for idx, company in enumerate(companies, start=1)]
            for future in as_completed(futures):
                idx, cname, fetched, platform_preview, error = future.result()
                completed += 1
                status = "ok" if not error else "error"
                source_stats.append({"Company": cname, "Platforms": platform_preview, "Jobs fetched": len(fetched), "Status": status, "Error": error or ""})
                if error:
                    errors.append(f"{cname}: {error}")
                    log(f"ERROR {cname}: {error}")
                else:
                    jobs.extend(fetched)
                    for job in fetched:
                        platform_counts[job.source] = platform_counts.get(job.source, 0) + 1
                    log(f"[{idx}/{len(companies)}] {cname}: fetched {len(fetched)} job(s) via {platform_preview}")
                progress.progress(completed / max(len(companies), 1), text=f"Fetched {completed}/{len(companies)} company source(s)")
        # ── Augment with free public board APIs (zero key, zero signup) ────────
        if _JOB_BOARD_AVAILABLE:
            _board_keyword = (extract_intent_terms(search_text) or ["software engineer"])[0]
            _existing_ids: set[str] = {j.external_job_id for j in jobs if j.external_job_id}
            _existing_keys: set[str] = {
                f"{j.title.lower().strip()}|{j.company.lower().strip()}"
                for j in jobs
            }

            def _add_board_jobs(raw_list: list[dict], source_label: str) -> None:
                added = 0
                for _raw in (raw_list or []):
                    _jid = (_raw.get("job_id") or "").strip()
                    if _jid and _jid in _existing_ids:
                        continue
                    _nkey = (
                        (_raw.get("title") or "").lower().strip()
                        + "|"
                        + (_raw.get("company") or "").lower().strip()
                    )
                    if _nkey in _existing_keys:
                        continue
                    try:
                        _nj = _convert_to_normalized_job(_raw)
                        jobs.append(_nj)
                        platform_counts[_nj.source] = platform_counts.get(_nj.source, 0) + 1
                        if _jid:
                            _existing_ids.add(_jid)
                        _existing_keys.add(_nkey)
                        added += 1
                    except Exception:
                        pass
                if added:
                    log(f"Free {source_label} API: added {added} additional job(s)")

            try:
                _add_board_jobs(_fetch_greenhouse_public(_board_keyword), "Greenhouse")
            except Exception as _e:
                log(f"Greenhouse public board error (non-fatal): {_e}")
            try:
                _add_board_jobs(_fetch_lever_public(_board_keyword), "Lever")
            except Exception as _e:
                log(f"Lever public board error (non-fatal): {_e}")
            try:
                _add_board_jobs(_fetch_workday_public(_board_keyword), "Workday")
            except Exception as _e:
                log(f"Workday public board error (non-fatal): {_e}")

        progress.progress(1.0, text="Ranking jobs against the active profile...")
        matches = rank_jobs(
            jobs,
            st.session_state.profile,
            threshold=threshold,
            us_only=location_mode == "United States / Remote only",
            include_unknown_locations=include_unknown_locations,
            intent_terms=extract_intent_terms(search_text),
            max_years_experience=max_years_experience,
        )
        # Apply F-1 and role-type filters and tag matches with metadata
        if _apply_filters is not None and (f1_filter or role_type_filter):
            # Build a lookup from canonical_url -> NormalizedJob for fast access
            _job_lookup = {j.canonical_url: j for j in jobs}
            for _m in matches:
                _nj = _job_lookup.get(_m.canonical_url)
                if _nj is None:
                    continue
                _passes, _meta = _apply_filters(_nj, f1_filter, role_type_filter)
                # Stamp metadata onto raw_job dict for badge rendering
                _m.raw_job["f1_friendly"]    = _meta.get("f1_friendly", False)
                _m.raw_job["f1_blocked"]     = _meta.get("f1_blocked", False)
                _m.raw_job["is_target_role"] = _meta.get("is_target_role", False)
                _m.raw_job["role_types"]     = _meta.get("role_types", [])
                if not _passes:
                    if _meta.get("f1_blocked"):
                        _m.decision = "f1_filtered"
                    elif not _meta.get("is_target_role"):
                        _m.decision = "role_filtered"
            # Sort: passing matches first, then filtered ones
            matches.sort(key=lambda _x: (
                0 if _x.decision not in ("f1_filtered", "role_filtered") else 1,
                -_x.score
            ))
        progress.empty()

    elapsed = time.perf_counter() - start
    st.session_state.jobs = jobs
    st.session_state.matches = matches
    st.session_state.platform_summary = platform_counts
    st.session_state.source_stats = sorted(source_stats, key=lambda r: str(r.get("Company", "")))
    st.session_state.last_scan_seconds = elapsed
    st.session_state.last_scan_label = time.strftime("%b %d, %I:%M %p")
    log(f"Scan complete: {len(jobs)} jobs, {len(matches)} ranked, {elapsed:.1f}s using {max_workers} worker(s)")
    if errors:
        st.warning("Some sources failed or returned no parseable jobs. Open Diagnostics for details. " + "; ".join(errors[:3]))
    st.success(f"Scan complete: {len(jobs)} jobs fetched and {len(matches)} matches ranked in {elapsed:.1f}s.")
    return True


def add_company_action(name: str, url: str, default_search: str, location_mode: str, include_unknown_locations: bool) -> None:
    if not name.strip() or not url.strip():
        st.error("Enter both company name and career URL.")
        return
    detected = detect_career_source(name, url, default_search)
    ats = detected.setdefault("ats", {})
    ats["us_only"] = location_mode == "United States / Remote only"
    ats["include_remote"] = True
    ats["include_unknown_locations"] = include_unknown_locations
    existing = all_companies()
    key = f"{detected.get('name','').strip().lower()}|{detected.get('careers_url','').strip().lower()}"
    if any(f"{c.get('name','').strip().lower()}|{c.get('careers_url','').strip().lower()}" == key for c in existing):
        st.info("That company source is already in the queue.")
        return
    st.session_state.user_companies.append(detected)
    save_persisted_companies(st.session_state.user_companies)
    st.success(f"Added {detected['name']} using {ats.get('type', 'auto')} connector.")
    log(f"Added company: {detected['name']} ({ats.get('type', 'auto')})")


def render_profile_tab() -> None:
    """Render the Personal Profile Setup tab with resume parsing and all profile fields."""
    from pathlib import Path
    try:
        from careerfit import db as _db
    except Exception:
        st.error("Database module not available")
        return

    prof = st.session_state.get("profile_form_draft") or st.session_state.get("db_profile") or {}

    st.markdown("""
        <div class='cf-section-header'>
          <h2>&#x1F464; Personal Profile Setup</h2>
          <p>Fill in your details once &mdash; the system uses them to apply automatically on your behalf.</p>
        </div>
    """, unsafe_allow_html=True)

    # ── Profile Card ──────────────────────────────────────────────────────
    db_profile = st.session_state.get("db_profile", {})
    if db_profile.get("first_name") and db_profile.get("last_name"):
        _fn = db_profile.get("first_name", "")
        _ln = db_profile.get("last_name", "")
        _initials = (_fn[:1] + _ln[:1]).upper()
        _edu_list = db_profile.get("education_entries") or []
        _edu0 = _edu_list[0] if _edu_list else {}
        _exp_list = db_profile.get("experience_entries") or []
        _exp0 = _exp_list[0] if _exp_list else {}
        _edu_str = (
            (_edu0.get("major") or _edu0.get("degree") or "") + " @ " + (_edu0.get("school") or "")
            if (_edu0.get("school") or _edu0.get("degree") or _edu0.get("major"))
            else (db_profile.get("degree", "") + " @ " + db_profile.get("university", "") if db_profile.get("university") else "")
        )
        _exp_str = (_exp0.get("title", "") + " at " + _exp0.get("company", "")) if _exp0.get("company") else ""
        _visa = db_profile.get("visa_status", "")
        _visa_badge = (
            "<span style='background:#DBEAFE;color:#1D4ED8;border-radius:20px;padding:3px 10px;font-size:11px;font-weight:700'>"
            + _visa + "</span>"
        ) if _visa else ""
        _exp_html = ("<div style='font-size:14px;color:#475569;margin-bottom:2px'>" + _exp_str + "</div>") if _exp_str else ""
        _edu_html = ("<div style='font-size:13px;color:#64748B'>" + _edu_str + "</div>") if _edu_str else ""
        st.markdown(
            "<div style='background:#FFFFFF;border:1px solid #E2E8F0;border-radius:16px;padding:20px 24px;"
            "margin-bottom:20px;display:flex;gap:20px;align-items:flex-start;"
            "box-shadow:0 1px 4px rgba(15,23,42,.06)'>"
            "<div style='width:72px;height:72px;border-radius:50%;background:linear-gradient(135deg,#2563EB,#06B6D4);"
            "display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:800;color:#fff;flex-shrink:0'>"
            + _initials +
            "</div>"
            "<div style='flex:1'>"
            "<div style='font-size:20px;font-weight:700;color:#0C1322;margin-bottom:4px'>"
            + _fn + " " + _ln + " " + _visa_badge +
            "</div>"
            + _exp_html + _edu_html +
            "</div></div>",
            unsafe_allow_html=True,
        )

    # ── Resume Upload & Parse ──────────────────────────────────────────────
    st.markdown("<div class='cf-profile-section'><h4>&#x1F4C4; Upload Resume to Auto-Fill</h4>", unsafe_allow_html=True)
    resume_file = st.file_uploader(
        "Upload resume (PDF, DOCX, or TXT)",
        type=["pdf", "docx", "txt"],
        key="profile_resume_upload",
        help="CareerFit will extract name, email, phone, education, and skills automatically.",
    )
    if resume_file:
        try:
            from careerfit.resume_parser import parse_resume, merge_parsed_into_profile
            parsed = parse_resume(resume_file.getvalue(), resume_file.name)
            merged = merge_parsed_into_profile(parsed, prof)
            st.session_state.profile_form_draft = merged
            prof = merged
            # Save resume file to disk
            resume_save_path = ROOT / "data" / resume_file.name
            resume_save_path.parent.mkdir(parents=True, exist_ok=True)
            resume_save_path.write_bytes(resume_file.getvalue())
            st.session_state.profile_form_draft["resume_path"] = resume_file.name  # just filename
            st.success(f"Resume parsed from {resume_file.name} — fields pre-filled below. Review and save.")
        except ImportError:
            st.info("Resume parser module not yet available. Fill fields manually.")
        except Exception as exc:
            st.warning(f"Could not fully parse resume: {exc}. Fill remaining fields manually.")
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Personal Information ───────────────────────────────────────────────
    st.markdown("<div class='cf-profile-section'><h4>&#x1F9D1; Personal Information</h4>", unsafe_allow_html=True)
    pi1, pi2 = st.columns(2)
    with pi1:
        first_name = st.text_input("First Name *", value=prof.get("first_name",""), key="pf_first_name")
    with pi2:
        last_name = st.text_input("Last Name *", value=prof.get("last_name",""), key="pf_last_name")
    pi3, pi4 = st.columns(2)
    with pi3:
        email = st.text_input("Email *", value=prof.get("email",""), key="pf_email")
    with pi4:
        phone = st.text_input("Phone", value=prof.get("phone",""), placeholder="+1 (555) 000-0000", key="pf_phone")
    address = st.text_input("Street Address", value=prof.get("address",""), key="pf_address")
    ci1, ci2, ci3 = st.columns([2,1,1])
    with ci1:
        city = st.text_input("City", value=prof.get("city",""), key="pf_city")
    with ci2:
        state = st.text_input("State", value=prof.get("state",""), placeholder="CA", key="pf_state")
    with ci3:
        zip_code = st.text_input("ZIP", value=prof.get("zip_code",""), key="pf_zip")
    country = st.text_input("Country", value=prof.get("country","United States"), key="pf_country")
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Professional Links ────────────────────────────────────────────────
    st.markdown("<div class='cf-profile-section'><h4>&#x1F517; Professional Links</h4>", unsafe_allow_html=True)
    lk1, lk2, lk3 = st.columns(3)
    with lk1:
        linkedin_url = st.text_input("LinkedIn URL", value=prof.get("linkedin_url",""), placeholder="https://linkedin.com/in/yourname", key="pf_linkedin")
    with lk2:
        github_url = st.text_input("GitHub URL", value=prof.get("github_url",""), placeholder="https://github.com/yourname", key="pf_github")
    with lk3:
        portfolio_url = st.text_input("Portfolio / Website", value=prof.get("portfolio_url",""), key="pf_portfolio")
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Education ─────────────────────────────────────────────────────────
    st.markdown("<div class='cf-profile-section'><h4>&#x1F393; Education</h4>", unsafe_allow_html=True)
    # Initialize if not in session state
    if not st.session_state.get("education_entries"):
        _saved_edu = prof.get("education_entries", [])
        if isinstance(_saved_edu, list) and _saved_edu:
            st.session_state.education_entries = _saved_edu
        elif prof.get("university") or prof.get("degree"):
            st.session_state.education_entries = [{"school": prof.get("university", ""), "degree": prof.get("degree", ""), "major": "", "gpa": str(prof.get("gpa", "") or ""), "start_year": "", "end_year": str(prof.get("graduation_year", "") or ""), "is_current": False}]
        else:
            st.session_state.education_entries = []
    for i, _entry in enumerate(list(st.session_state.education_entries)):
        _hc, _dc = st.columns([6, 1])
        with _hc:
            st.markdown(f"<div style='font-weight:700;color:#1E293B;font-size:14px;margin-bottom:4px'>Education #{i+1}</div>", unsafe_allow_html=True)
        with _dc:
            if st.button("\u2715", key=f"del_edu_{i}", help="Remove this education entry"):
                st.session_state.education_entries.pop(i)
                st.rerun()
        _e1, _e2 = st.columns(2)
        with _e1:
            st.text_input("School / University", value=_entry.get("school", ""), key=f"edu_school_{i}")
        with _e2:
            st.text_input("Degree", value=_entry.get("degree", ""), placeholder="B.E. / M.S. / B.S.", key=f"edu_degree_{i}")
        _e3, _e4, _e5 = st.columns(3)
        with _e3:
            st.text_input("Major / Field of Study", value=_entry.get("major", ""), placeholder="Data Science", key=f"edu_major_{i}")
        with _e4:
            st.text_input("GPA", value=str(_entry.get("gpa", "")), placeholder="3.8", key=f"edu_gpa_{i}")
        with _e5:
            st.checkbox("Currently enrolled", value=bool(_entry.get("is_current", False)), key=f"edu_current_{i}")
        _e6, _e7 = st.columns(2)
        with _e6:
            st.text_input("Start Year", value=str(_entry.get("start_year", "")), placeholder="2021", key=f"edu_start_{i}")
        with _e7:
            st.text_input("End Year / Expected", value=str(_entry.get("end_year", "")), placeholder="2023", key=f"edu_end_{i}")
        st.markdown("<hr style='border:none;border-top:1px solid #E2E8F0;margin:10px 0'>", unsafe_allow_html=True)
    if st.button("\uff0b Add Education", key="add_edu_btn"):
        st.session_state.education_entries.append({"school": "", "degree": "", "major": "", "gpa": "", "start_year": "", "end_year": "", "is_current": False})
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Work Experience ────────────────────────────────────────────────────
    st.markdown("<div class='cf-profile-section'><h4>&#x1F4BC; Work Experience</h4>", unsafe_allow_html=True)
    if not st.session_state.get("experience_entries"):
        _saved_exp = prof.get("experience_entries", [])
        if isinstance(_saved_exp, list) and _saved_exp:
            st.session_state.experience_entries = _saved_exp
        else:
            st.session_state.experience_entries = []
    for i, _xentry in enumerate(list(st.session_state.experience_entries)):
        _xhc, _xdc = st.columns([6, 1])
        with _xhc:
            st.markdown(f"<div style='font-weight:700;color:#1E293B;font-size:14px;margin-bottom:4px'>Experience #{i+1}</div>", unsafe_allow_html=True)
        with _xdc:
            if st.button("\u2715", key=f"del_exp_{i}", help="Remove this experience entry"):
                st.session_state.experience_entries.pop(i)
                st.rerun()
        _x1, _x2 = st.columns(2)
        with _x1:
            st.text_input("Company", value=_xentry.get("company", ""), key=f"exp_company_{i}")
        with _x2:
            st.text_input("Job Title", value=_xentry.get("title", ""), placeholder="Software Engineer Intern", key=f"exp_title_{i}")
        _x3, _x4, _x5 = st.columns(3)
        with _x3:
            st.text_input("Location", value=_xentry.get("location", ""), placeholder="San Francisco, CA", key=f"exp_location_{i}")
        with _x4:
            st.text_input("Start Date", value=_xentry.get("start_date", ""), placeholder="MM/YYYY", key=f"exp_start_{i}")
        with _x5:
            st.checkbox("Current role", value=bool(_xentry.get("is_current", False)), key=f"exp_current_{i}")
        st.text_input("End Date", value=_xentry.get("end_date", ""), placeholder="MM/YYYY or Present", key=f"exp_end_{i}")
        st.text_area("Description", value=_xentry.get("description", ""), height=80, placeholder="Describe key responsibilities and achievements...", key=f"exp_desc_{i}")
        st.markdown("<hr style='border:none;border-top:1px solid #E2E8F0;margin:10px 0'>", unsafe_allow_html=True)
    if st.button("\uff0b Add Experience", key="add_exp_btn"):
        st.session_state.experience_entries.append({"company": "", "title": "", "location": "", "start_date": "", "end_date": "", "is_current": False, "description": ""})
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Skills ────────────────────────────────────────────────────────────
    st.markdown("<div class='cf-profile-section'><h4>&#x1F527; Skills</h4>", unsafe_allow_html=True)
    _skills_val = prof.get("skills", "")
    if isinstance(_skills_val, list):
        _skills_val = ", ".join(_skills_val)
    skills_text = st.text_area(
        "Skills (comma-separated or one per line)",
        value=_skills_val,
        height=100,
        placeholder="Python, SQL, PyTorch, React, AWS, Machine Learning...",
        key="pf_skills",
        help="Used for cover letter generation and application auto-fill.",
    )
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Visa & Work Authorization ──────────────────────────────────────────
    st.markdown("<div class='cf-profile-section'><h4>&#x1F6C2; Visa & Work Authorization</h4>", unsafe_allow_html=True)
    vs1, vs2 = st.columns(2)
    _visa_options = ["F-1/OPT", "F-1/CPT", "H-1B", "Green Card", "US Citizen", "Other"]
    _visa_default = prof.get("visa_status", "F-1/OPT")
    _visa_idx = _visa_options.index(_visa_default) if _visa_default in _visa_options else 0
    with vs1:
        visa_status = st.selectbox("Visa Status", _visa_options, index=_visa_idx, key="pf_visa")
    with vs2:
        _spon_options = ["Yes", "No"]
        _spon_default = prof.get("requires_sponsorship", "Yes")
        _spon_idx = _spon_options.index(_spon_default) if _spon_default in _spon_options else 0
        requires_sponsorship = st.selectbox("Will you require visa sponsorship?", _spon_options, index=_spon_idx, key="pf_sponsorship")
    # Auto-set work authorization based on visa status
    work_authorization = "Yes" if visa_status in ("F-1/OPT", "F-1/CPT") else prof.get("work_authorization", "Yes")
    st.caption(f"Work authorization: **{work_authorization}** (auto-set based on visa status)")
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Target Roles ──────────────────────────────────────────────────────
    st.markdown("<div class='cf-profile-section'><h4>&#x1F3AF; Target Roles & Experience</h4>", unsafe_allow_html=True)
    _tr_val = prof.get("target_roles","")
    if isinstance(_tr_val, list):
        _tr_val = "\n".join(_tr_val)
    target_roles_text = st.text_area(
        "Target role titles (one per line)",
        value=_tr_val,
        height=100,
        placeholder="Software Engineer Intern\nData Science Intern\nMachine Learning Engineer",
        key="pf_target_roles",
        help="These are used for role-matching. Add the exact job titles you are targeting.",
    )
    tr1, tr2 = st.columns(2)
    with tr1:
        try:
            _ye_default = int(prof.get("years_experience") or 0)
        except (ValueError, TypeError):
            _ye_default = 0
        years_experience = st.slider("Years of relevant experience", 0, 5, _ye_default, key="pf_years_exp")
    with tr2:
        available_start_date = st.text_input("Available start date", value=prof.get("available_start_date","Immediately"), key="pf_start_date")
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Application Preferences ───────────────────────────────────────────
    st.markdown("<div class='cf-profile-section'><h4>&#x2699;&#xFE0F; Application Preferences</h4>", unsafe_allow_html=True)
    ap1, ap2 = st.columns(2)
    with ap1:
        salary_expectation = st.text_input("Salary expectation (optional)", value=prof.get("salary_expectation",""), placeholder="e.g. $25/hr or $80,000/yr", key="pf_salary")
    with ap2:
        referral_source = st.text_input("How did you hear about roles?", value=prof.get("referral_source","LinkedIn"), key="pf_referral")
    cover_letter_text = st.text_area(
        "Cover letter template (optional)",
        value=prof.get("cover_letter_text",""),
        height=140,
        placeholder="Dear Hiring Team,\n\nI am excited to apply for the {job_title} role at {company}...\n\nBest regards,\n{first_name}",
        key="pf_cover_letter",
        help="Use {job_title}, {company}, {first_name}, {skills} as placeholders.",
    )
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Platform Credentials ──────────────────────────────────────────────
    st.markdown("<div class='cf-profile-section'><h4>&#x1F511; Platform Login Credentials</h4>", unsafe_allow_html=True)
    st.markdown("<p class='cf-muted'>Credentials are stored locally in the SQLite database on your machine. They are never sent to any server.</p>", unsafe_allow_html=True)
    _platforms = ["Workday", "Greenhouse", "Ashby", "Lever", "SmartRecruiters"]
    _existing_platforms = prof.get("platforms") or {}
    if isinstance(_existing_platforms, str):
        try:
            import json as _json; _existing_platforms = _json.loads(_existing_platforms)
        except Exception:
            _existing_platforms = {}
    platform_creds = {}
    for _plat in _platforms:
        _plat_key = _plat.lower()
        _existing = _existing_platforms.get(_plat_key, {})
        pc1, pc2 = st.columns(2)
        with pc1:
            _pe = st.text_input(f"{_plat} Email", value=_existing.get("email",""), key=f"pf_plat_email_{_plat_key}")
        with pc2:
            _pp = st.text_input(f"{_plat} Password", value=_existing.get("password",""), type="password", key=f"pf_plat_pass_{_plat_key}")
        if _pe or _pp:
            platform_creds[_plat_key] = {"email": _pe, "password": _pp}
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Save Button ───────────────────────────────────────────────────────
    sv1, sv2 = st.columns([1,2])
    with sv1:
        save_clicked = st.button("&#x1F4BE; Save Profile", type="primary", use_container_width=True, key="save_profile_btn")
    with sv2:
        st.markdown("<p class='cf-muted' style='padding-top:8px'>Your profile is saved locally and used for all future job applications.</p>", unsafe_allow_html=True)

    if save_clicked:
        if not email or not first_name:
            st.error("First name and email are required.")
        else:
            _target_roles_list = [r.strip() for r in target_roles_text.splitlines() if r.strip()]
            # Collect education entries from indexed session state keys
            _edu_entries = []
            for i in range(len(st.session_state.get("education_entries", []))):
                _edu_entries.append({
                    "school": st.session_state.get(f"edu_school_{i}", ""),
                    "degree": st.session_state.get(f"edu_degree_{i}", ""),
                    "major": st.session_state.get(f"edu_major_{i}", ""),
                    "gpa": st.session_state.get(f"edu_gpa_{i}", ""),
                    "start_year": st.session_state.get(f"edu_start_{i}", ""),
                    "end_year": st.session_state.get(f"edu_end_{i}", ""),
                    "is_current": st.session_state.get(f"edu_current_{i}", False),
                })
            # Collect experience entries from indexed session state keys
            _exp_entries = []
            for i in range(len(st.session_state.get("experience_entries", []))):
                _exp_entries.append({
                    "company": st.session_state.get(f"exp_company_{i}", ""),
                    "title": st.session_state.get(f"exp_title_{i}", ""),
                    "location": st.session_state.get(f"exp_location_{i}", ""),
                    "start_date": st.session_state.get(f"exp_start_{i}", ""),
                    "end_date": st.session_state.get(f"exp_end_{i}", ""),
                    "is_current": st.session_state.get(f"exp_current_{i}", False),
                    "description": st.session_state.get(f"exp_desc_{i}", ""),
                })
            profile_data = {
                "first_name": first_name, "last_name": last_name,
                "email": email, "phone": phone,
                "address": address, "city": city, "state": state,
                "zip_code": zip_code, "country": country,
                "linkedin_url": linkedin_url, "github_url": github_url,
                "portfolio_url": portfolio_url,
                "visa_status": visa_status,
                "target_roles": _target_roles_list,
                "years_experience": int(years_experience),
                "available_start_date": available_start_date,
                "salary_expectation": salary_expectation,
                "work_authorization": work_authorization,
                "requires_sponsorship": requires_sponsorship,
                "referral_source": referral_source,
                "cover_letter_text": cover_letter_text,
                "resume_path": prof.get("resume_path", ""),
                "platforms": platform_creds,
                "education_entries": _edu_entries,
                "experience_entries": _exp_entries,
                "skills": st.session_state.get("pf_skills", "").strip(),
            }
            # Backward-compat flat fields from first education entry
            if _edu_entries:
                _fe = _edu_entries[0]
                profile_data.setdefault("university", _fe.get("school", ""))
                profile_data.setdefault("degree", _fe.get("major", "") or _fe.get("degree", ""))
                try:
                    profile_data.setdefault("gpa", float(_fe.get("gpa", 0) or 0))
                except Exception:
                    pass
                try:
                    profile_data.setdefault("graduation_year", int(_fe.get("end_year", 2026) or 2026))
                except Exception:
                    pass
            try:
                _db.save_profile(profile_data)
                st.session_state.db_profile = profile_data
                st.session_state.profile_form_draft = profile_data
                st.session_state.education_entries = _edu_entries
                st.session_state.experience_entries = _exp_entries
                st.session_state["_db_loaded"] = False  # force reload on next render
                st.success("Profile saved successfully! Switching to Job Matching tab.")
                st.session_state.active_tab = "matching"
                st.rerun()
            except Exception as exc:
                st.error(f"Failed to save profile: {exc}")


def render_autoapply_tab() -> None:
    """Render the full Auto-Apply Center tab."""
    # run_continuous_apply and _render_results are inlined at module level above

    st.markdown("""
        <div class='cf-section-header'>
          <h2>&#x1F680; Auto-Apply Center</h2>
          <p>Zero-interaction job applications: the system matches, filters, and applies on your behalf.</p>
        </div>
    """, unsafe_allow_html=True)

    db_profile = st.session_state.get("db_profile", {})
    profile_ready = bool(db_profile.get("email"))

    if not profile_ready:
        st.warning("Please complete your Profile Setup before using Auto-Apply.")
        if st.button("Go to Profile Setup", key="goto_profile_autoapply"):
            st.session_state.active_tab = "profile"
            st.rerun()
        return

    # ── Platform Credentials Summary ─────────────────────────────────────
    st.markdown("<div class='cf-card'><h3>🔑 Platform Credentials</h3>", unsafe_allow_html=True)
    _platforms_data = db_profile.get("platforms") or {}
    if isinstance(_platforms_data, str):
        try:
            import json as _jj; _platforms_data = _jj.loads(_platforms_data)
        except Exception:
            _platforms_data = {}
    _plat_names = ["Workday", "Greenhouse", "Ashby", "Lever", "SmartRecruiters"]
    _plat_rows = ""
    for _p in _plat_names:
        _pk = _p.lower()
        _pcreds = _platforms_data.get(_pk, {})
        _email_ok = "✅" if _pcreds.get("email") else "⬜"
        _pass_ok  = "✅" if _pcreds.get("password") else "⬜"
        _plat_rows += f"<tr><td style='padding:7px 12px;font-size:13px;color:#1E293B'>{_p}</td><td style='padding:7px 12px;font-size:12px'>{_email_ok} {_pcreds.get('email','—')[:30]}</td><td style='padding:7px 12px;text-align:center;font-size:13px'>{_pass_ok}</td></tr>"
    st.markdown(
        f"<table style='width:100%;border-collapse:collapse;border:1px solid #E2E8F0;border-radius:10px;overflow:hidden'>"
        f"<thead><tr>"
        f"<th style='padding:9px 12px;background:#F1F5F9;color:#334155;font-size:11px;text-align:left'>Platform</th>"
        f"<th style='padding:9px 12px;background:#F1F5F9;color:#334155;font-size:11px;text-align:left'>Email</th>"
        f"<th style='padding:9px 12px;background:#F1F5F9;color:#334155;font-size:11px;text-align:center'>Password</th>"
        f"</tr></thead><tbody>{_plat_rows}</tbody></table>",
        unsafe_allow_html=True,
    )
    st.caption("To update credentials, go to the Profile Setup tab.")
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Q&A Answers ───────────────────────────────────────────────────────
    render_qa_section()

    # ── Continuous Apply Settings ─────────────────────────────────────────
    st.markdown("<div class='cf-card'><h3>⚙️ Continuous Apply Settings</h3>", unsafe_allow_html=True)
    ca1, ca2 = st.columns(2)
    with ca1:
        delay_between_secs = st.slider("Delay between applications (seconds)", 10, 120, 45, key="ca_delay")
        max_applications   = st.number_input("Max applications per run", 1, 200, 50, key="ca_max_apps")
    with ca2:
        apply_threshold    = st.slider("Minimum match score to apply", 0.85, 0.99, 0.90, 0.01, key="ca_threshold",
                                        help="Minimum 85% enforced. 90% recommended for quality applications.")
        dry_run_continuous = st.toggle("Dry run (fill forms, do not submit)", value=True, key="ca_dry_run")
        headless_cont      = st.toggle("Headless browser", value=True, key="ca_headless")
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Apply Status ──────────────────────────────────────────────────────
    matches_count     = len(st.session_state.get("matches", []))
    eligible_count    = sum(
        1 for m in st.session_state.get("matches", [])
        if m.score >= apply_threshold and m.decision not in ("f1_filtered","role_filtered")
    )
    applied_count     = len(st.session_state.get("applied_job_ids", set()))

    st.markdown("<div class='cf-grid'>", unsafe_allow_html=True)
    mc = st.columns(3)
    with mc[0]:
        render_metric("Total ranked matches", str(matches_count), "from last scan")
    with mc[1]:
        render_metric(f"Eligible (>= {int(apply_threshold*100)}%)", str(eligible_count), "after filters + threshold")
    with mc[2]:
        render_metric("Already applied", str(applied_count), "tracked in DB")
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Start / Stop Controls ─────────────────────────────────────────────
    is_running = st.session_state.get("continuous_apply_running", False)
    no_matches = not st.session_state.get("matches")

    ctrl1, ctrl2, ctrl3 = st.columns([2, 1, 2])
    with ctrl1:
        start_clicked = st.button(
            "🚀 Start Continuous Apply",
            type="primary",
            use_container_width=True,
            disabled=(is_running or no_matches or not profile_ready),
            key="start_continuous_apply_btn",
            help="Runs a full automated apply loop on all eligible jobs." if not no_matches else "Run Job Matching first to get results.",
        )
    with ctrl2:
        if st.button("⏹ Stop", use_container_width=True, key="stop_continuous_apply_btn",
                     disabled=not is_running):
            st.session_state.continuous_apply_stop = True
            st.rerun()

    with ctrl3:
        if no_matches:
            st.markdown("<div class='cf-alert cf-alert-warn'>Run Job Matching first to populate results.</div>", unsafe_allow_html=True)
        elif is_running:
            st.markdown("<div class='cf-alert cf-alert-info'>Auto-apply loop is running…</div>", unsafe_allow_html=True)

    if start_clicked:
        st.session_state.continuous_apply_running = True
        st.session_state.continuous_apply_stop    = False
        st.rerun()

    # ── Running loop ──────────────────────────────────────────────────────
    if st.session_state.get("continuous_apply_running", False):
        run_continuous_apply(
            threshold    = apply_threshold,
            delay_secs   = delay_between_secs,
            max_apps     = max_applications,
            dry_run      = dry_run_continuous,
            headless     = headless_cont,
            qa_answers   = st.session_state.get("qa_answers", []),
        )

    # ── Past results ──────────────────────────────────────────────────────
    past = st.session_state.get("continuous_apply_results") or st.session_state.get("apply_results", [])
    if past:
        st.markdown("---")
        st.markdown("### Apply Results")
        _render_results(past)


def render_qa_section() -> None:
    """Render the Q&A Answer Store section inside the Auto-Apply tab."""
    try:
        from careerfit import db as _db
    except Exception:
        st.error("Database module not available")
        return

    st.markdown("""
        <div class='cf-section-header'>
          <h2>&#x1F4AC; Q&A Answer Store</h2>
          <p>Pre-enter answers to common application questions. The system auto-fills matching questions during applications.</p>
        </div>
    """, unsafe_allow_html=True)

    # Load F-1 Defaults button
    F1_DEFAULTS = [
        ("work authorization", "Yes"),
        ("authorized to work", "Yes"),
        ("require sponsorship", "Yes"),
        ("visa sponsorship", "F-1 OPT — requires sponsorship"),
        ("graduation", "2026"),
        ("expected graduation", "May 2026"),
        ("gpa", "3.8"),
        ("us citizen", "No"),
        ("permanent resident", "No"),
        ("security clearance", "No"),
        ("veteran", "I am not a protected veteran"),
        ("disability", "No, I don't have a disability"),
        ("gender", "Prefer not to say"),
        ("ethnicity", "Prefer not to say"),
        ("salary expectation", "Open to discussion"),
        ("how did you hear", "LinkedIn"),
        ("start date", "Immediately"),
    ]

    if st.button("&#x1F4CB; Load F-1 Student Defaults", key="load_f1_defaults_btn",
                 help="Adds pre-configured answers for common F-1 student questions."):
        existing_patterns = {qa["question_pattern"].lower() for qa in st.session_state.get("qa_answers", [])}
        added = 0
        for pattern, answer in F1_DEFAULTS:
            if pattern.lower() not in existing_patterns:
                _db.save_qa_answer(pattern, answer)
                added += 1
        st.session_state.qa_answers = _db.load_qa_answers()
        if added:
            st.success(f"Added {added} default Q&A answers.")
        else:
            st.info("All defaults already loaded.")
        st.rerun()

    # Display existing answers
    qa_list = st.session_state.get("qa_answers", [])
    if qa_list:
        st.markdown(f"<div class='cf-muted' style='margin-bottom:12px'>{len(qa_list)} answer(s) configured</div>", unsafe_allow_html=True)
        for qa in qa_list:
            qa_id = qa.get("id")
            q_pattern = qa.get("question_pattern", "")
            answer = qa.get("answer", "")
            qa_c1, qa_c2, qa_c3 = st.columns([3, 3, 1])
            with qa_c1:
                st.markdown(f"<div class='cf-muted' style='font-size:12px;padding:6px 0'><b>Pattern:</b> {escape(q_pattern[:80])}</div>", unsafe_allow_html=True)
            with qa_c2:
                st.markdown(f"<div class='cf-muted' style='font-size:12px;padding:6px 0'><b>Answer:</b> {escape(answer[:60])}</div>", unsafe_allow_html=True)
            with qa_c3:
                if st.button("&#x2715;", key=f"del_qa_{qa_id}", help="Delete this answer"):
                    _db.delete_qa_answer(qa_id)
                    st.session_state.qa_answers = _db.load_qa_answers()
                    st.rerun()
    else:
        st.markdown("<div class='cf-alert cf-alert-info'>No Q&A answers configured yet. Click 'Load F-1 Student Defaults' to get started.</div>", unsafe_allow_html=True)

    # Add new answer form
    st.markdown("<div class='cf-card' style='margin-top:18px'>", unsafe_allow_html=True)
    st.markdown("<h3>Add Custom Answer</h3>", unsafe_allow_html=True)
    with st.form("add_qa_form", clear_on_submit=True):
        new_pattern = st.text_input(
            "Question pattern",
            placeholder="e.g. 'require sponsorship', 'work authorization', 'graduation year'",
            help="A keyword or phrase that will be matched against the question text on application forms.",
        )
        new_answer = st.text_area(
            "Your answer",
            placeholder="e.g. Yes, No, 2026, F-1 OPT, etc.",
            height=80,
        )
        if st.form_submit_button("Add Answer", type="primary"):
            if new_pattern and new_answer:
                _db.save_qa_answer(new_pattern.strip(), new_answer.strip())
                st.session_state.qa_answers = _db.load_qa_answers()
                st.success("Answer added.")
                st.rerun()
            else:
                st.error("Both question pattern and answer are required.")
    st.markdown("</div>", unsafe_allow_html=True)


# Sidebar controls only. Navigation has been removed.
with st.sidebar:
    st.markdown(
        """
        <div class='cf-brand'>
          <div class='cf-logo'>CF</div>
          <div><div class='cf-brand-title'>CareerFit<br/>Studio</div><div class='cf-brand-sub'>multi-user matching workspace</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    dark_mode = st.toggle("Dark mode", value=False, help="Switch the workspace to a darker high-contrast theme.")
    if dark_mode:
        st.markdown(DARK_MODE_CSS, unsafe_allow_html=True)
    st.markdown("<div class='cf-side-label'>Matching controls</div>", unsafe_allow_html=True)
    threshold = st.slider("High-fit threshold", 0.50, 0.99, DEFAULT_THRESHOLD, 0.01)
    total_sources_now = max(1, len(all_companies()))
    scan_all_companies = st.toggle("Scan all configured companies", value=True, help="Recommended for end users: scan every company source currently available in this workspace.")
    if scan_all_companies:
        company_limit = total_sources_now
        st.caption(f"{total_sources_now} source(s) queued")
    else:
        company_limit = st.number_input("Companies per scan", min_value=1, max_value=max(100, total_sources_now), value=min(total_sources_now, 5), step=1)
    parallel_workers = st.slider("Parallel fetch workers", 1, 8, int(os.getenv("CAREERFIT_COMPANY_WORKERS", "4")))
    experience_level = st.selectbox(
        "Experience level",
        ["Intern only", "Entry-level only (0-2 yrs)", "Intern + Entry-level (recommended)", "All levels"],
        index=2,
        help="Filters results by seniority. Entry-level includes new grad, junior, associate, and roles asking for 0-2 years of experience.",
    )
    _level_to_search = {
        "Intern only": "intern",
        "Entry-level only (0-2 yrs)": "entry level",
        "Intern + Entry-level (recommended)": "intern",
        "All levels": "",
    }
    default_search = _level_to_search.get(experience_level, "intern")
    search_text = st.text_input(
        "Role search keyword (optional override)",
        value=os.getenv("CAREERFIT_DEFAULT_SEARCH", default_search),
        placeholder="Examples: intern, data analyst, software engineer, product manager",
        help="Single keyword sent to ATS platforms. Most ATSes (Workday, Amazon, Microsoft) treat multi-word queries as exact phrases — keep this short. The experience-level filter is applied during ranking, not at search time.",
    )
    max_years_experience = st.slider(
        "Max years of experience required",
        min_value=0, max_value=10, value=2, step=1,
        help="Roles asking for more than this many years of experience get penalized in ranking. Set to 10 to disable.",
    )
    f1_filter = st.toggle("F-1 Visa filter (skip clearance/no-sponsor)", value=True,
                           help="Hides roles requiring security clearance, US citizenship only, or no sponsorship.", key="f1_filter_toggle")
    role_type_filter = st.toggle("Fall/New Grad roles only", value=True,
                                  help="Shows only Fall Intern, Fall Co-op, New Grad, and Entry Level roles.", key="role_type_filter_toggle")
    st.session_state["f1_filter"] = f1_filter
    st.session_state["role_type_filter"] = role_type_filter
    location_mode = st.selectbox("Location preference", ["United States / Remote only", "Global"], index=0)
    include_unknown_locations = st.checkbox("Include roles with unspecified locations", value=False)
    fast_mode = st.toggle("Fast matching mode", value=True, help="Uses listing metadata for faster results. Disable only when deeper job-description fetching is required.")
    if (f1_filter or role_type_filter) and fast_mode:
        st.markdown("<div class='cf-side-note' style='color:#F59E0B;border-color:rgba(245,158,11,.3)'>&#x26A0; F-1/role filters work best with Fast Mode OFF — job descriptions needed to detect clearance/sponsorship language.</div>", unsafe_allow_html=True)
    use_cache = st.toggle("Reuse recent career-site results", value=True)
    allow_unprofiled_scan = st.checkbox("Allow non-personalized search", value=False, help="Use only public job titles and search terms. Personalized match scoring requires resume, document, or website evidence.")
    if st.button("Clear cached job data", use_container_width=True):
        clear_fetch_cache()
        st.success("Cache cleared.")
    st.markdown("<div class='cf-side-note'>Single-workspace product flow: add candidate inputs, add company sources, run matching, and review results without switching pages.</div>", unsafe_allow_html=True)
    render_apply_queue_panel()

# Hero
st.markdown(
    """
    <div class='cf-hero'>
      <div class='cf-hero-content'>
        <div class='cf-eyebrow'>Intelligent Job Matching &middot; F-1 Student Edition</div>
        <h1>Personalized job matching for resumes, portfolios, and company career sources.</h1>
        <p>Designed for multi-user career discovery: each user can provide profile websites, upload career documents, add employer career pages, and run an end-to-end matching workflow from a single workspace.</p>
        <div class='cf-toolbar'>
          <span class='cf-chip cf-chip-dark'>Resume + portfolio intelligence</span>
          <span class='cf-chip cf-chip-dark'>ATS auto-detection</span>
          <span class='cf-chip cf-chip-dark'>Workday · Greenhouse · Ashby · Lever · SmartRecruiters · Custom</span>
          <span class='cf-chip cf-chip-dark'>User-controlled matching</span>
        </div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Tab navigation
tab_cols = st.columns([1, 1, 1])
with tab_cols[0]:
    if st.button("&#x1F464; Profile Setup", use_container_width=True,
                 type="primary" if st.session_state.active_tab == "profile" else "secondary",
                 key="tab_profile"):
        st.session_state.active_tab = "profile"
        st.rerun()
with tab_cols[1]:
    if st.button("&#x1F50D; Job Matching", use_container_width=True,
                 type="primary" if st.session_state.active_tab == "matching" else "secondary",
                 key="tab_matching"):
        st.session_state.active_tab = "matching"
        st.rerun()
with tab_cols[2]:
    if st.button("&#x1F680; Auto-Apply", use_container_width=True,
                 type="primary" if st.session_state.active_tab == "autoapply" else "secondary",
                 key="tab_autoapply"):
        st.session_state.active_tab = "autoapply"
        st.rerun()
st.markdown("<hr style='border:none;border-top:1px solid #E2E8F0;margin:4px 0 20px'>", unsafe_allow_html=True)

if st.session_state.active_tab == "matching":
    # Agent Status Bar — KPI tiles
    try:
        from careerfit import db as _status_db
        _conn = _status_db.get_conn()
        _today_count = _conn.execute("SELECT COUNT(*) FROM applied_jobs WHERE date(applied_at)=date('now')").fetchone()[0]
        _total_count = _conn.execute("SELECT COUNT(*) FROM applied_jobs").fetchone()[0]
        _conn.close()
    except Exception:
        _today_count = 0
        _total_count = 0
    _jobs_scanned = len(st.session_state.get("jobs", []))
    _best_match = max((m.score for m in st.session_state.get("matches", [])), default=0)
    _best_pct = f"{int(_best_match * 100)}%" if _best_match else "--"
    _kpi_cols = st.columns(4)
    _kpi_data = [
        ("Applied Today", str(_today_count), "#10B981"),
        ("Total Applied", str(_total_count), "#2563EB"),
        ("Jobs Scanned", str(_jobs_scanned), "#7C3AED"),
        ("Best Match", _best_pct, "#F59E0B"),
    ]
    for _kc, (_label, _val, _color) in zip(_kpi_cols, _kpi_data):
        with _kc:
            st.markdown(
                f"<div style='background:linear-gradient(135deg,#0A1628,#0D1F3C);border-radius:14px;"
                f"padding:18px 20px;border-top:3px solid {_color};margin-bottom:16px'>"
                f"<div style='font-size:26px;font-weight:800;color:#FFFFFF;margin-bottom:4px'>{_val}</div>"
                f"<div style='font-size:12px;color:#94A3B8;letter-spacing:.04em'>{_label}</div></div>",
                unsafe_allow_html=True,
            )

    # Status metrics
    high_count = len([m for m in st.session_state.matches if m.score >= threshold])
    profile_ready = bool(st.session_state.profile.combined_text.strip())
    st.markdown("<div class='cf-grid'>", unsafe_allow_html=True)
    mc = st.columns(4)
    with mc[0]:
        render_metric("Candidate profile", "Ready" if profile_ready else "Not built", f"{len(st.session_state.profile.combined_text):,} chars")
    with mc[1]:
        render_metric("Career sources", str(len(all_companies())), "available sources")
    with mc[2]:
        render_metric("Ranked roles", str(len(st.session_state.matches)), "latest run")
    with mc[3]:
        render_metric(f"High-fit matches >= {pct(threshold)}%", str(high_count), st.session_state.last_scan_label)
    st.markdown("</div>", unsafe_allow_html=True)

    # Input + command area
    st.markdown("<div class='cf-subtitle'>Unified matching workspace</div>", unsafe_allow_html=True)
    left, right = st.columns([1.05, .95])
    with left:
        st.markdown("<div class='cf-card'><h3>1. Candidate profile inputs</h3><p class='cf-muted'>Add public profile pages and upload career documents. CareerFit uses these sources to infer skills, experience signals, role targets, and domain preferences for the current user session.</p>", unsafe_allow_html=True)
        default_url_text = st.session_state.get("profile_url_blob") or ""
        url_blob = st.text_area(
            "Public profile or portfolio URLs",
            value=default_url_text,
            placeholder="https://your-portfolio.com\nhttps://github.com/your-username\nhttps://your-personal-site.com",
            height=116,
            help="Use publicly readable pages only. Private or login-protected pages may not be accessible.",
        )
        st.caption("Tip: add a portfolio, GitHub profile, personal website, project page, or public profile export URL.")
        st.session_state.profile_url_blob = url_blob
        max_docs = int(os.getenv("CAREERFIT_MAX_PROFILE_DOCS", "20"))
        uploaded = st.file_uploader(f"Career documents (PDF or DOCX, up to {max_docs})", type=["pdf", "docx"], accept_multiple_files=True, help="Upload resumes, CVs, transcripts, portfolio exports, or role-specific documents. Text-readable PDF and real DOCX files work best.")
        if uploaded and len(uploaded) > max_docs:
            st.warning(f"Only the first {max_docs} files will be processed.")
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown("<div class='cf-card'><h3>2. Career sources</h3><p class='cf-muted'>Add public employer career pages or ATS job-board URLs. CareerFit attempts to detect the platform and select the appropriate connector automatically.</p>", unsafe_allow_html=True)
        with st.form("add_company_form", clear_on_submit=False):
            c1, c2 = st.columns([.35, .65])
            with c1:
                company_name = st.text_input("Employer name", placeholder="Example: Amazon, Microsoft, NVIDIA, Tesla")
            with c2:
                company_url = st.text_input("Public careers URL", placeholder="Paste the company careers page or ATS job-board URL")
            default_search = st.text_input("Default role keyword for this employer", value=search_text or "intern", placeholder="Examples: intern, software engineer, marketing analyst")
            f1, f2 = st.columns([.4, .6])
            with f1:
                preview = st.form_submit_button("Preview detected connector")
            with f2:
                add = st.form_submit_button("Add employer source", type="primary")
        if preview and company_url:
            detected = detect_career_source(company_name or "Company", company_url, default_search)
            ats = detected.get("ats") or {}
            st.markdown(f"<div class='cf-alert cf-alert-success'>Detected connector: {platform_badge(ats.get('type','auto'))}</div>", unsafe_allow_html=True)
            st.json(detected)
        if add:
            add_company_action(company_name, company_url, default_search, location_mode, include_unknown_locations)
        sources = all_companies()
        if sources:
            rows = []
            for c in sources:
                try:
                    expanded = expand_company_sources(c)
                    platforms = ", ".join(sorted({(x.get("ats") or {}).get("type", "custom") for x in expanded}))
                except Exception:
                    platforms = (c.get("ats") or {}).get("type", "custom")
                rows.append({"Company": c.get("name"), "Connector(s)": platforms, "URL": c.get("careers_url")})
            # Use HTML table instead of st.dataframe to avoid canvas dark-mode rendering
            tbl_rows = "".join(
                f"<tr><td style='padding:8px 12px;border-bottom:1px solid #E2E8F0;color:#0B1220;font-size:13px'>{r['Company']}</td>"
                f"<td style='padding:8px 12px;border-bottom:1px solid #E2E8F0;color:#0B1220;font-size:13px'>{r['Connector(s)']}</td>"
                f"<td style='padding:8px 12px;border-bottom:1px solid #E2E8F0;color:#475569;font-size:12px;word-break:break-all'>{r['URL']}</td></tr>"
                for r in rows
            )
            st.markdown(
                f"<table style='width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #E2E8F0'>"
                f"<thead><tr>"
                f"<th style='padding:10px 12px;background:#F1F5F9;color:#334155;font-size:12px;text-align:left;font-weight:700'>Company</th>"
                f"<th style='padding:10px 12px;background:#F1F5F9;color:#334155;font-size:12px;text-align:left;font-weight:700'>Connector(s)</th>"
                f"<th style='padding:10px 12px;background:#F1F5F9;color:#334155;font-size:12px;text-align:left;font-weight:700'>URL</th>"
                f"</tr></thead><tbody>{tbl_rows}</tbody></table>",
                unsafe_allow_html=True
            )
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("Clear manually added employer sources", use_container_width=True):
                st.session_state.user_companies = []
                save_persisted_companies([])
                st.success("Manually added employer sources were cleared. Default configured sources remain available.")
        with cc2:
            st.caption("Employer sources added here are saved locally for future sessions.")
        st.markdown("</div>", unsafe_allow_html=True)

    with right:
        st.markdown("<div class='cf-command'><div class='cf-command-title'>3. Matching workflow</div><p class='cf-muted'>Run the complete matching pipeline for the current user: extract profile signals, retrieve open roles from employer sources, apply location preferences, score relevance, and refresh the ranked results below.</p>", unsafe_allow_html=True)
        st.markdown(
            """
            <div class='cf-command-steps'>
              <div class='cf-step'><div class='cf-step-num'>1</div><div class='cf-step-title'>Extract</div><div class='cf-step-desc'>Parse resumes, CVs, portfolios, and public profile pages.</div></div>
              <div class='cf-step'><div class='cf-step-num'>2</div><div class='cf-step-title'>Fetch</div><div class='cf-step-desc'>Detect ATS platforms and retrieve open roles.</div></div>
              <div class='cf-step'><div class='cf-step-num'>3</div><div class='cf-step-title'>Rank</div><div class='cf-step-desc'>Score roles against skills, keywords, experience, and role targets.</div></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown("</div>", unsafe_allow_html=True)
        b1, b2 = st.columns([.58, .42])
        with b1:
            run_all = st.button("Run full match analysis", type="primary", use_container_width=True)
        with b2:
            run_scan = st.button("Scan with current profile", use_container_width=True)
        b3, b4 = st.columns([.5, .5])
        with b3:
            build_only = st.button("Analyze profile only", use_container_width=True)
        with b4:
            export_ready = bool(st.session_state.matches)
            if export_ready:
                st.download_button(
                    "Export matches CSV",
                    matches_dataframe(st.session_state.matches).to_csv(index=False).encode("utf-8"),
                    "careerfit_matches.csv",
                    "text/csv",
                    use_container_width=True,
                )
            else:
                st.button("Export matches CSV", disabled=True, use_container_width=True)

        if run_all:
            ok = build_profile_action(url_blob, uploaded)
            if ok or allow_unprofiled_scan:
                run_scan_action(threshold, search_text, location_mode, include_unknown_locations, fast_mode, use_cache, allow_unprofiled_scan, scan_all_companies, company_limit, parallel_workers, max_years_experience,
                                f1_filter=st.session_state.get("f1_filter", True),
                                role_type_filter=st.session_state.get("role_type_filter", True))
        if build_only:
            build_profile_action(url_blob, uploaded)
        if run_scan:
            run_scan_action(threshold, search_text, location_mode, include_unknown_locations, fast_mode, use_cache, allow_unprofiled_scan, scan_all_companies, company_limit, parallel_workers, max_years_experience,
                            f1_filter=st.session_state.get("f1_filter", True),
                            role_type_filter=st.session_state.get("role_type_filter", True))

        st.markdown("<div class='cf-card'><h3>Suggested role targets</h3>", unsafe_allow_html=True)
        render_role_suggestions(st.session_state.profile)
        st.markdown("</div>", unsafe_allow_html=True)

    # Ranked job matches
    st.markdown("<div class='cf-subtitle'>Ranked job matches</div>", unsafe_allow_html=True)
    if not st.session_state.matches:
        st.markdown("<div class='cf-alert cf-alert-info'>No ranked results yet. Add candidate profile inputs and employer career sources, then click <b>Run full match analysis</b>.</div>", unsafe_allow_html=True)
    else:
        filt = st.columns([1.25, .85, .85, .85])
        with filt[0]:
            q = st.text_input("Search ranked jobs", placeholder="Search by title, skill, company, platform, or location")
        with filt[1]:
            company_filter = st.selectbox("Company", ["All"] + sorted({m.company for m in st.session_state.matches}))
        with filt[2]:
            platform_filter = st.selectbox("Platform", ["All"] + sorted({m.source for m in st.session_state.matches}))
        with filt[3]:
            view = st.selectbox("View", ["All ranked roles", "High-fit matches", "Review queue"], index=0,
                               help="All ranked roles shows every fetched job sorted by score. High-fit matches shows only roles above the threshold.")

        matches = list(st.session_state.matches)
        if q:
            s = q.lower()
            matches = [m for m in matches if s in " ".join([m.company, m.title, m.location or "", m.role_family, m.reason_summary]).lower()]
        if company_filter != "All":
            matches = [m for m in matches if m.company == company_filter]
        if platform_filter != "All":
            matches = [m for m in matches if m.source == platform_filter]
        if view == "High-fit matches":
            matches = [m for m in matches if m.score >= threshold]
        elif view == "Review queue":
            matches = [m for m in matches if m.score < threshold and m.score >= 0.45]

        if not matches:
            st.markdown("<div class='cf-alert cf-alert-warn'>No jobs match the current filters. Try All ranked roles, lower the threshold, or broaden the search text.</div>", unsafe_allow_html=True)
        for m in matches[:100]:
            render_match_card(m, threshold, applied_job_ids=st.session_state.get("applied_job_ids", set()))

    run_apply_queue()

    # Insights and diagnostics remain on the same page, collapsed by default.
    with st.expander("Source health, analytics, and diagnostics", expanded=False):
        c1, c2 = st.columns([1, 1])
        with c1:
            st.markdown("### Connector distribution")
            if st.session_state.platform_summary:
                st.bar_chart(pd.DataFrame([{"Platform": k, "Jobs": v} for k, v in st.session_state.platform_summary.items()]).set_index("Platform"))
            else:
                st.info("Run a match analysis to populate connector analytics.")
            st.markdown("### Employer fetch status")
            if st.session_state.source_stats:
                st.dataframe(pd.DataFrame(st.session_state.source_stats), use_container_width=True, hide_index=True)
            else:
                st.info("No source diagnostics are available yet.")
        with c2:
            st.markdown("### Parsed candidate profile sources")
            sources = [s.__dict__ for s in st.session_state.profile.sources]
            if sources:
                st.dataframe(pd.DataFrame(sources), use_container_width=True, hide_index=True)
            else:
                st.info("No candidate profile sources have been analyzed yet.")
            st.markdown("### Execution log")
            if st.session_state.run_log:
                for item in st.session_state.run_log[:30]:
                    st.code(item)
            else:
                st.info("No execution logs are available yet.")
        st.markdown("### Fetched role table")
        if st.session_state.jobs:
            st.dataframe(job_dataframe(st.session_state.jobs), use_container_width=True, hide_index=True)
        else:
            st.info("No roles have been fetched yet.")
        with st.expander("Candidate profile text preview"):
            st.text_area("Profile evidence text", st.session_state.profile.combined_text[:15000], height=260, label_visibility="collapsed")
        with st.expander("Raw match data"):
            st.json([m.to_dict() for m in st.session_state.matches[:15]])

elif st.session_state.active_tab == "profile":
    render_profile_tab()

elif st.session_state.active_tab == "autoapply":
    render_autoapply_tab()

# Insights and diagnostics remain on the same page, collapsed by default.
with st.expander("Source health, analytics, and diagnostics", expanded=False):
    c1, c2 = st.columns([1, 1])
    with c1:
        st.markdown("### Connector distribution")
        if st.session_state.platform_summary:
            st.bar_chart(pd.DataFrame([{"Platform": k, "Jobs": v} for k, v in st.session_state.platform_summary.items()]).set_index("Platform"))
        else:
            st.info("Run a match analysis to populate connector analytics.")
        st.markdown("### Employer fetch status")
        if st.session_state.source_stats:
            st.dataframe(pd.DataFrame(st.session_state.source_stats), use_container_width=True, hide_index=True)
        else:
            st.info("No source diagnostics are available yet.")
    with c2:
        st.markdown("### Parsed candidate profile sources")
        sources = [s.__dict__ for s in st.session_state.profile.sources]
        if sources:
            st.dataframe(pd.DataFrame(sources), use_container_width=True, hide_index=True)
        else:
            st.info("No candidate profile sources have been analyzed yet.")
        st.markdown("### Execution log")
        if st.session_state.run_log:
            for item in st.session_state.run_log[:30]:
                st.code(item)
        else:
            st.info("No execution logs are available yet.")
    st.markdown("### Fetched role table")
    if st.session_state.jobs:
        st.dataframe(job_dataframe(st.session_state.jobs), use_container_width=True, hide_index=True)
    else:
        st.info("No roles have been fetched yet.")
    with st.expander("Candidate profile text preview"):
        st.text_area("Profile evidence text", st.session_state.profile.combined_text[:15000], height=260, label_visibility="collapsed")
    with st.expander("Raw match data"):
        st.json([m.to_dict() for m in st.session_state.matches[:15]])
