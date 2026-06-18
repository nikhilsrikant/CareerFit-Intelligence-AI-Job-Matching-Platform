from __future__ import annotations

# Auto-install Playwright Chromium on Streamlit Cloud
import subprocess, sys as _sys
subprocess.run(
    [_sys.executable, "-m", "playwright", "install", "chromium"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

import asyncio, json, os, sys, time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import streamlit as st

# ---------------------------------------------------------------------------
# Backend module imports — all guarded so partial failures don't crash the app
# ---------------------------------------------------------------------------
try:
    from careerfit import db as _db
    _db.init_db()
except Exception as _e:
    _db = None  # type: ignore[assignment]
    print(f"DB init warning: {_e}")

try:
    from careerfit.resume_parser import parse_resume
except Exception:
    parse_resume = None  # type: ignore[assignment]

try:
    from careerfit.source_intelligence import build_runtime_profile, RuntimeProfile
except Exception:
    build_runtime_profile = None  # type: ignore[assignment]
    RuntimeProfile = None  # type: ignore[assignment]

try:
    from careerfit.matching import rank_jobs, JobMatch
except Exception:
    rank_jobs = None  # type: ignore[assignment]
    JobMatch = None  # type: ignore[assignment]

try:
    from careerfit.filters import apply_filters
except Exception:
    apply_filters = None  # type: ignore[assignment]

try:
    from careerfit.job_board import fetch_all_jobs, detect_ats, convert_to_normalized_job
except Exception:
    fetch_all_jobs = None  # type: ignore[assignment]
    detect_ats = None  # type: ignore[assignment]
    convert_to_normalized_job = None  # type: ignore[assignment]

try:
    from careerfit.fetchers import NormalizedJob
except Exception:
    NormalizedJob = None  # type: ignore[assignment]

try:
    from careerfit.apply_agent.engine import ApplicationEngine
except Exception:
    ApplicationEngine = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# App constants
# ---------------------------------------------------------------------------
APP_TITLE = "CareerFit Agent"
DEFAULT_THRESHOLD = float(os.getenv("CAREERFIT_DEFAULT_THRESHOLD", "0.90"))

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="✦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# CSS — dark glassmorphism theme inspired by JobRight AI
# ---------------------------------------------------------------------------
_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

:root {
    --navy: #0A0E1A;
    --card: #141928;
    --border: rgba(99,102,241,0.15);
    --accent: #6366F1;
    --cyan: #22D3EE;
    --text: #E2E8F0;
    --muted: #64748B;
    --green: #10B981;
    --red: #F43F5E;
    --yellow: #F59E0B;
}

* { box-sizing: border-box; }

html, body, [class*="css"], [data-testid="stAppViewContainer"],
[data-testid="stMain"], .main, .block-container,
[data-testid="stVerticalBlock"] {
    background-color: var(--navy) !important;
    color: var(--text) !important;
    font-family: 'Inter', sans-serif !important;
}

[data-testid="stHeader"],
header[data-testid="stHeader"],
#MainMenu, footer,
[data-testid="stToolbar"],
[data-testid="stDecoration"] {
    display: none !important;
    visibility: hidden !important;
}

[data-testid="stSidebar"] {
    background-color: #0D1117 !important;
    border-right: 1px solid rgba(99,102,241,0.1) !important;
    min-width: 240px !important;
    max-width: 240px !important;
}
[data-testid="stSidebar"] > div {
    background-color: #0D1117 !important;
}

.stButton > button {
    background: linear-gradient(135deg, #6366F1, #8B5CF6) !important;
    color: white !important;
    border: none !important;
    border-radius: 12px !important;
    font-family: 'Inter', sans-serif !important;
    font-weight: 500 !important;
    transition: all 0.2s ease !important;
    width: 100% !important;
}
.stButton > button:hover {
    opacity: 0.9 !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 8px 24px rgba(99,102,241,0.4) !important;
}
.stButton > button:active { transform: translateY(0) !important; }

.stTextInput > div > div > input,
.stTextArea > div > div > textarea,
.stSelectbox > div > div {
    background-color: #1E2535 !important;
    border: 1px solid rgba(99,102,241,0.2) !important;
    border-radius: 10px !important;
    color: var(--text) !important;
    font-family: 'Inter', sans-serif !important;
}
.stTextInput > div > div > input:focus,
.stTextArea > div > div > textarea:focus {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 2px rgba(99,102,241,0.25) !important;
}

.stSlider > div > div > div { background: var(--accent) !important; }

label, .stCheckbox label, .stRadio label,
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li {
    color: var(--text) !important;
    font-family: 'Inter', sans-serif !important;
}

.stFileUploader {
    background-color: #1A1F35 !important;
    border: 2px dashed rgba(99,102,241,0.3) !important;
    border-radius: 16px !important;
    padding: 2rem !important;
}
.stFileUploader label { color: var(--text) !important; }
.stFileUploader:hover { border-color: var(--accent) !important; }

div[data-testid="stExpander"] {
    background-color: var(--card) !important;
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
}

.stRadio > div { flex-direction: row !important; gap: 0.5rem !important; }

.glass-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    backdrop-filter: blur(16px);
    padding: 20px;
    color: var(--text);
    margin-bottom: 12px;
}
.kpi-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    backdrop-filter: blur(16px);
    padding: 24px 20px;
    color: var(--text);
    text-align: center;
}
.kpi-number {
    font-size: 2.2rem;
    font-weight: 700;
    background: linear-gradient(135deg, #6366F1, #22D3EE);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    line-height: 1;
    margin-bottom: 6px;
}
.kpi-label {
    font-size: 0.78rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 500;
}
.job-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 18px 20px;
    margin-bottom: 10px;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
    color: var(--text);
}
.job-card:hover {
    border-color: rgba(99,102,241,0.4);
    box-shadow: 0 4px 20px rgba(99,102,241,0.12);
}
.pill-btn {
    display: inline-block;
    border: 1px solid rgba(99,102,241,0.3);
    background: transparent;
    color: var(--text);
    border-radius: 20px;
    padding: 6px 16px;
    cursor: pointer;
    font-size: 13px;
    font-family: 'Inter', sans-serif;
    margin-right: 6px;
    transition: all 0.15s;
}
.pill-btn.active,
.pill-btn:hover {
    background: #6366F1;
    border-color: #6366F1;
    color: white;
}
.badge {
    display: inline-block;
    border-radius: 10px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.02em;
}
.badge-green { background: rgba(16,185,129,0.18); color: #10B981; }
.badge-indigo { background: rgba(99,102,241,0.18); color: #818CF8; }
.badge-yellow { background: rgba(245,158,11,0.18); color: #F59E0B; }
.badge-red { background: rgba(244,63,94,0.18); color: #F43F5E; }
.badge-cyan { background: rgba(34,211,238,0.18); color: #22D3EE; }

.company-circle {
    width: 44px;
    height: 44px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 13px;
    color: white;
    flex-shrink: 0;
}
@keyframes pulse {
    0%,100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.5; transform: scale(0.85); }
}
.pulsing-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    background: #10B981;
    border-radius: 50%;
    animation: pulse 2s infinite;
    margin-right: 6px;
    vertical-align: middle;
}
.idle-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    background: #475569;
    border-radius: 50%;
    margin-right: 6px;
    vertical-align: middle;
}
.nav-active .stButton > button {
    background: rgba(99,102,241,0.2) !important;
    border: 1px solid rgba(99,102,241,0.4) !important;
    color: #818CF8 !important;
}
.section-title {
    font-size: 1rem;
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 12px;
    margin-top: 4px;
}
.page-header {
    font-size: 1.7rem;
    font-weight: 700;
    color: var(--text);
    margin-bottom: 4px;
}
.page-sub {
    color: var(--muted);
    font-size: 0.92rem;
    margin-bottom: 24px;
}
.skill-chip {
    display: inline-block;
    background: rgba(99,102,241,0.15);
    border: 1px solid rgba(99,102,241,0.25);
    color: #818CF8;
    border-radius: 8px;
    padding: 4px 10px;
    font-size: 12px;
    margin: 3px;
    font-weight: 500;
}
.apply-progress-bar {
    height: 6px;
    background: rgba(99,102,241,0.15);
    border-radius: 3px;
    overflow: hidden;
    margin: 8px 0;
}
.apply-progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #6366F1, #22D3EE);
    border-radius: 3px;
    transition: width 0.4s ease;
}
</style>
"""
st.markdown(_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

def _init_session_state() -> None:
    defaults = {
        "page": "upload",
        "profile": {},
        "runtime_profile": None,
        "jobs": [],          # list[JobMatch]
        "jobs_raw": [],      # list[NormalizedJob]
        "applying": False,
        "apply_stop": False,
        "apply_progress": 0,
        "apply_total": 0,
        "apply_log": [],
        "filter_active": "All",
        "jobs_last_fetched": None,
        "dry_run": True,
        "keyword_override": "",
        "f1_filter": True,
        "role_filter": True,
        "match_threshold": DEFAULT_THRESHOLD,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


_init_session_state()

# Auto-restore from DB on first load
if st.session_state["page"] == "upload" and _db is not None:
    _saved = _db.load_profile()
    if _saved and _saved.get("email"):
        st.session_state["profile"] = _saved
        st.session_state["page"] = "dashboard"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

_GRADIENT_PALETTES = [
    ("135deg, #6366F1, #8B5CF6"),
    ("135deg, #0EA5E9, #6366F1"),
    ("135deg, #10B981, #0EA5E9"),
    ("135deg, #F59E0B, #EF4444"),
    ("135deg, #EC4899, #8B5CF6"),
    ("135deg, #14B8A6, #6366F1"),
]


def _company_initials(company_name: str) -> str:
    words = (company_name or "?").split()
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return (company_name or "?")[:2].upper()


def _gradient_for(name: str) -> str:
    idx = sum(ord(c) for c in (name or "")) % len(_GRADIENT_PALETTES)
    return _GRADIENT_PALETTES[idx]


def _score_ring_svg(score_pct: int, size: int = 48) -> str:
    r = (size / 2) - 5
    cx = cy = size / 2
    circumference = 2 * 3.14159 * r
    filled = circumference * score_pct / 100
    if score_pct >= 90:
        color = "#10B981"
    elif score_pct >= 70:
        color = "#F59E0B"
    else:
        color = "#F43F5E"
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" style="transform:rotate(-90deg)">'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="rgba(255,255,255,0.07)" stroke-width="4"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="4"'
        f' stroke-dasharray="{filled:.1f} {circumference:.1f}" stroke-linecap="round"/>'
        f'</svg>'
        f'<div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:11px;font-weight:700;color:{color}">'
        f'{score_pct}%</div>'
    )


def _score_ring_html(score_pct: int, size: int = 48) -> str:
    return (
        f'<div style="position:relative;width:{size}px;height:{size}px;flex-shrink:0">'
        + _score_ring_svg(score_pct, size)
        + "</div>"
    )


def _ats_badge_html(ats_name: str) -> str:
    labels = {
        "workday": ("Workday", "badge-yellow"),
        "greenhouse": ("Greenhouse", "badge-green"),
        "ashby": ("Ashby", "badge-cyan"),
        "lever": ("Lever", "badge-indigo"),
        "smartrecruiters": ("SmartRecruiters", "badge-indigo"),
        "generic": ("Direct", "badge-indigo"),
    }
    label, cls = labels.get((ats_name or "").lower(), (ats_name or "Unknown", "badge-indigo"))
    return f'<span class="badge {cls}">{label}</span>'


def _get_today_applied_count() -> int:
    if _db is None:
        return 0
    try:
        import sqlite3
        from careerfit.db import DB_PATH
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        cur = conn.execute("SELECT COUNT(*) FROM applied_jobs WHERE date(applied_at)=date('now')")
        row = cur.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0


def _make_job_card_html(match, index: int, already_applied: bool) -> str:
    score_pct = int(round(match.score * 100))
    initials = _company_initials(match.company)
    gradient = _gradient_for(match.company)
    ats = detect_ats(match.canonical_url) if detect_ats else "generic"
    location_str = match.location or "Remote / US"
    title_safe = (match.title or "").replace("<", "&lt;").replace(">", "&gt;")
    company_safe = (match.company or "").replace("<", "&lt;").replace(">", "&gt;")
    location_safe = location_str.replace("<", "&lt;").replace(">", "&gt;")

    applied_badge = (
        '<span class="badge badge-green">&#10003; Applied</span>'
        if already_applied
        else ""
    )
    strengths_html = ""
    if match.matched_strengths:
        chips = "".join(
            f'<span class="skill-chip">{s}</span>'
            for s in match.matched_strengths[:4]
        )
        strengths_html = f'<div style="margin-top:8px">{chips}</div>'

    return f"""
<div class="job-card">
  <div style="display:flex;align-items:flex-start;gap:14px">
    <div class="company-circle" style="background:linear-gradient({gradient})">{initials}</div>
    <div style="flex:1;min-width:0">
      <div style="font-weight:600;font-size:0.97rem;color:#E2E8F0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{title_safe}</div>
      <div style="color:#64748B;font-size:0.83rem;margin-top:2px">{company_safe} &middot; {location_safe}</div>
      <div style="margin-top:6px;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        {_ats_badge_html(ats)}
        {applied_badge}
      </div>
      {strengths_html}
    </div>
    {_score_ring_html(score_pct, 52)}
  </div>
</div>
"""


def _fetch_and_rank_jobs(force_refresh: bool = False) -> None:
    """Fetch jobs from APIs and rank them against the runtime profile."""
    if not force_refresh and st.session_state.get("jobs"):
        return  # already cached

    if _db is None or fetch_all_jobs is None:
        return

    api_keys = _db.load_api_keys()
    has_keys = bool(
        api_keys.get("jsearch_key")
        or (api_keys.get("adzuna_app_id") and api_keys.get("adzuna_app_key"))
    )
    if not has_keys:
        return

    runtime_profile = st.session_state.get("runtime_profile")
    keyword_override = st.session_state.get("keyword_override", "")

    if keyword_override:
        keywords = [k.strip() for k in keyword_override.split(",") if k.strip()]
    elif runtime_profile and hasattr(runtime_profile, "skills"):
        keywords = (runtime_profile.skills or [])[:10]
    else:
        profile = st.session_state.get("profile", {})
        raw_skills = profile.get("skills", "")
        keywords = [s.strip() for s in (raw_skills or "").split(",") if s.strip()][:10]

    with st.spinner("Fetching jobs from job boards..."):
        try:
            raw_jobs = fetch_all_jobs(keywords, api_keys)
        except Exception as exc:
            st.warning(f"Job fetch error: {exc}")
            return

    if not raw_jobs:
        st.session_state["jobs"] = []
        st.session_state["jobs_raw"] = []
        st.session_state["jobs_last_fetched"] = datetime.now(timezone.utc)
        return

    if convert_to_normalized_job:
        normalized = [convert_to_normalized_job(r) for r in raw_jobs]
    else:
        st.session_state["jobs"] = []
        return

    st.session_state["jobs_raw"] = normalized

    threshold = st.session_state.get("match_threshold", DEFAULT_THRESHOLD)
    if rank_jobs and runtime_profile:
        try:
            matches = rank_jobs(normalized, runtime_profile, threshold=threshold)
        except Exception:
            matches = []
    else:
        if not runtime_profile:
            st.warning(
                "Profile not built — jobs were fetched but cannot be ranked. "
                "Re-upload your resume to enable matching."
            )
        matches = []

    st.session_state["jobs"] = matches
    st.session_state["jobs_last_fetched"] = datetime.now(timezone.utc)


def _run_apply_loop(eligible: list, runtime_profile, dry_run: bool) -> None:
    """Execute the auto-apply loop for a list of eligible job matches."""
    progress_box = st.empty()
    total = len(eligible)
    st.session_state["apply_total"] = total

    for i, match in enumerate(eligible):
        if st.session_state.get("apply_stop"):
            break

        st.session_state["apply_progress"] = i + 1
        pct = int((i + 1) / max(total, 1) * 100)
        progress_box.markdown(
            f"""
<div class="glass-card" style="padding:14px 20px">
  <div style="font-size:0.9rem;color:#64748B;margin-bottom:6px">
    Auto-applying &nbsp;&#8212;&nbsp; <strong style="color:#E2E8F0">{i+1}/{total}</strong>
  </div>
  <div class="apply-progress-bar">
    <div class="apply-progress-fill" style="width:{pct}%"></div>
  </div>
  <div style="font-size:0.82rem;color:#64748B;margin-top:4px">
    {match.company} &mdash; {match.title}
  </div>
</div>
""",
            unsafe_allow_html=True,
        )

        ats_name = detect_ats(match.canonical_url) if detect_ats else "generic"
        job_dict = {
            "company": match.company,
            "title": match.title,
            "canonical_url": match.canonical_url,
            "source": ats_name,
            "ats": ats_name,
        }
        job_id = (match.raw_job or {}).get("job_id", "") or ""

        if not dry_run and ApplicationEngine is not None:
            try:
                results = ApplicationEngine.run_sync(
                    [job_dict],
                    runtime_profile=runtime_profile,
                    headless=True,
                )
                result = results[0] if results else {"status": "failed"}
            except Exception as exc:
                result = {"status": "failed", "reason": str(exc)}
        else:
            result = {"status": "dry_run", "company": match.company, "title": match.title}

        status = result.get("status", "failed")
        if status in ("applied", "dry_run"):
            if _db is not None:
                try:
                    _db.mark_applied(job_id, match.canonical_url, match.company, match.title, ats_name)
                except Exception:
                    pass
            prefix = "&#10003;" if status == "applied" else "&#9711;"
            st.session_state["apply_log"].append(
                f"{prefix} {match.company} — {match.title}"
            )
        else:
            reason = result.get("reason", "failed")
            st.session_state["apply_log"].append(
                f"&#10007; {match.company} — {match.title}: {reason}"
            )

        time.sleep(0.5)

    progress_box.empty()
    st.session_state["applying"] = False
    st.session_state["apply_progress"] = 0


def _build_eligible_jobs() -> list:
    """Return the list of job matches eligible for auto-apply."""
    jobs = st.session_state.get("jobs", [])
    threshold = st.session_state.get("match_threshold", DEFAULT_THRESHOLD)
    eligible = []
    for match in jobs:
        if match.score < threshold:
            continue
        job_id = (match.raw_job or {}).get("job_id", "") or ""
        if _db is not None:
            try:
                if _db.is_already_applied(job_id, match.canonical_url):
                    continue
            except Exception:
                pass
        if apply_filters is not None and NormalizedJob is not None:
            try:
                norm = NormalizedJob(
                    company=match.company,
                    source=match.source,
                    external_job_id=job_id or None,
                    canonical_url=match.canonical_url,
                    title=match.title,
                    location=match.location,
                    description_text=(match.raw_job or {}).get("description", ""),
                )
                passes, _ = apply_filters(
                    norm,
                    f1_filter_enabled=st.session_state.get("f1_filter", True),
                    role_type_filter_enabled=st.session_state.get("role_filter", True),
                )
                if not passes:
                    continue
            except Exception:
                pass
        eligible.append(match)
    eligible.sort(key=lambda m: m.score, reverse=True)
    return eligible


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar() -> None:
    with st.sidebar:
        # Logo
        st.markdown(
            """
<div style="display:flex;align-items:center;gap:10px;padding:8px 0 16px">
  <div style="width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#6366F1,#8B5CF6);
              display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;color:white;flex-shrink:0">
    CF
  </div>
  <span style="font-weight:700;font-size:1rem;color:#E2E8F0">CareerFit Agent</span>
</div>
""",
            unsafe_allow_html=True,
        )
        st.divider()

        current_page = st.session_state.get("page", "dashboard")

        applied_count = 0
        if _db is not None:
            try:
                applied_count = len(_db.get_applied_jobs())
            except Exception:
                pass

        nav_items = [
            ("dashboard", "&#127968;  Dashboard"),
            ("resume", "&#128196;  Resume"),
            ("jobs", "&#128269;  Jobs"),
            ("applied", f"&#128640;  Applied ({applied_count})"),
            ("settings", "&#9881;  Settings"),
        ]

        for page_key, label in nav_items:
            is_active = current_page == page_key
            style = (
                "background:rgba(99,102,241,0.18);border:1px solid rgba(99,102,241,0.4);color:#818CF8;"
                if is_active
                else "background:transparent;border:1px solid transparent;color:#94A3B8;"
            )
            if st.button(
                label,
                key=f"nav_{page_key}",
                help=None,
                use_container_width=True,
            ):
                if page_key != current_page:
                    st.session_state["page"] = page_key
                    st.rerun()
            # Re-style just the last rendered button via inline CSS trick
            st.markdown(
                f"""<style>
div[data-testid="stButton"] button[kind="secondary"]:last-of-type {{
    {style}
}}
</style>""",
                unsafe_allow_html=True,
            )

        st.divider()

        # Agent status
        applying = st.session_state.get("applying", False)
        if applying:
            st.markdown(
                '<div style="font-size:0.82rem;color:#64748B">'
                '<span class="pulsing-dot"></span><strong style="color:#10B981">Agent Active</strong></div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div style="font-size:0.82rem;color:#64748B">'
                '<span class="idle-dot"></span>Idle</div>',
                unsafe_allow_html=True,
            )


# ---------------------------------------------------------------------------
# Upload page
# ---------------------------------------------------------------------------

def render_upload_page() -> None:
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown(
            """
<div style="text-align:center;padding:40px 0 20px">
  <div style="width:100px;height:100px;border-radius:28px;
              background:linear-gradient(135deg,#6366F1,#8B5CF6);
              display:flex;align-items:center;justify-content:center;
              font-size:2rem;font-weight:900;color:white;
              margin:0 auto 20px;
              box-shadow:0 20px 60px rgba(99,102,241,0.35)">CF</div>
  <h1 style="font-size:2.2rem;font-weight:800;margin:0 0 10px;
             background:linear-gradient(135deg,#E2E8F0,#818CF8);
             -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text">
    CareerFit Agent
  </h1>
  <p style="color:#64748B;font-size:1rem;margin:0 0 32px">
    Drop your resume. The agent handles the rest — zero manual interaction.
  </p>
</div>
""",
            unsafe_allow_html=True,
        )

        uploaded_file = st.file_uploader(
            "Drop your resume here to get started",
            type=["pdf", "docx", "txt"],
            key="resume_upload",
            label_visibility="visible",
        )

        if uploaded_file is not None:
            with st.spinner("Parsing your resume and building your profile..."):
                file_bytes = uploaded_file.read()

                # Save resume to data/
                data_dir = ROOT / "data"
                data_dir.mkdir(parents=True, exist_ok=True)
                save_path = data_dir / uploaded_file.name
                save_path.write_bytes(file_bytes)

                # Parse resume
                parsed: dict = {}
                if parse_resume is not None:
                    try:
                        parsed = parse_resume(file_bytes, uploaded_file.name) or {}
                    except Exception as exc:
                        st.warning(f"Resume parse warning: {exc}")

                # Build runtime profile
                runtime_profile = None
                if build_runtime_profile is not None:
                    try:
                        runtime_profile = build_runtime_profile(
                            [], [(uploaded_file.name, file_bytes)]
                        )
                    except Exception as exc:
                        st.warning(f"Profile build warning: {exc}")

                # Infer visa_status from resume text rather than hardcoding
                _resume_text_lower = (parsed.get("raw_text", "") or "").lower()
                if any(kw in _resume_text_lower for kw in ("f-1", " f1 ", "opt ", " cpt ", "curricular practical")):
                    _inferred_visa = "F-1"
                elif any(kw in _resume_text_lower for kw in ("green card", "permanent resident", "lawful permanent")):
                    _inferred_visa = "PR"
                else:
                    _inferred_visa = ""  # user can set in Settings if needed

                # Compose profile dict
                profile_dict: dict = {
                    "first_name": parsed.get("first_name", ""),
                    "last_name": parsed.get("last_name", ""),
                    "email": parsed.get("email", ""),
                    "phone": parsed.get("phone", ""),
                    "linkedin_url": parsed.get("linkedin_url", ""),
                    "github_url": parsed.get("github_url", ""),
                    "gpa": parsed.get("gpa"),
                    "graduation_year": parsed.get("graduation_year"),
                    "university": parsed.get("university", ""),
                    "degree": parsed.get("degree", ""),
                    "skills": parsed.get("skills_text", ""),
                    "resume_path": str(save_path),
                    "visa_status": _inferred_visa,
                }

                if _db is not None:
                    try:
                        _db.save_profile(profile_dict)
                    except Exception as exc:
                        st.warning(f"Profile save warning: {exc}")

                st.session_state["profile"] = profile_dict
                st.session_state["runtime_profile"] = runtime_profile
                st.session_state["page"] = "dashboard"
                st.rerun()

        st.markdown(
            """
<div style="text-align:center;margin-top:20px">
  <div style="color:#475569;font-size:0.82rem">Supports PDF, DOCX, TXT</div>
  <div style="color:#475569;font-size:0.78rem;margin-top:8px">
    Need API keys?
    <a href="https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch"
       style="color:#818CF8;text-decoration:none" target="_blank">JSearch (free tier)</a>
    &nbsp;&middot;&nbsp;
    <a href="https://developer.adzuna.com"
       style="color:#818CF8;text-decoration:none" target="_blank">Adzuna (free)</a>
  </div>
</div>
""",
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Dashboard — job feed + KPIs + agent control
# ---------------------------------------------------------------------------

def _render_job_feed(jobs: list, show_apply_buttons: bool = True) -> None:
    """Render filtered job card list with apply buttons."""
    if not jobs:
        st.markdown(
            """
<div class="glass-card" style="text-align:center;padding:48px 24px">
  <div style="font-size:2.5rem;margin-bottom:12px">&#128269;</div>
  <div style="font-size:1rem;font-weight:600;color:#E2E8F0;margin-bottom:6px">No jobs found yet</div>
  <div style="color:#64748B;font-size:0.875rem">
    Click <strong>Fetch Jobs</strong> above or add API keys in Settings to start finding opportunities.
  </div>
</div>
""",
            unsafe_allow_html=True,
        )
        return

    filter_active = st.session_state.get("filter_active", "All")
    threshold = st.session_state.get("match_threshold", DEFAULT_THRESHOLD)
    filtered = []
    for match in jobs:
        if filter_active == "All":
            filtered.append(match)
        elif filter_active == "High Match" and match.score >= threshold:
            filtered.append(match)
        elif filter_active == "F-1 Safe":
            desc = (match.raw_job or {}).get("description", "").lower()
            if "sponsorship" in desc or "f-1" in desc or "opt" in desc or "cpt" in desc:
                filtered.append(match)
            elif "clearance" not in desc and "no sponsorship" not in desc:
                filtered.append(match)
        elif filter_active == "Intern":
            if "intern" in (match.title or "").lower():
                filtered.append(match)
        elif filter_active == "New Grad":
            if "new grad" in (match.title or "").lower() or "entry" in (match.title or "").lower():
                filtered.append(match)

    if not filtered:
        st.markdown(
            '<div style="color:#64748B;text-align:center;padding:32px">No jobs match this filter.</div>',
            unsafe_allow_html=True,
        )
        return

    for i, match in enumerate(filtered[:50]):
        already_applied = False
        if _db is not None:
            try:
                job_id = (match.raw_job or {}).get("job_id", "") or ""
                already_applied = _db.is_already_applied(job_id, match.canonical_url)
            except Exception:
                pass

        st.markdown(_make_job_card_html(match, i, already_applied), unsafe_allow_html=True)

        if show_apply_buttons and not already_applied:
            btn_col, _ = st.columns([1, 3])
            with btn_col:
                if st.button("Apply Now", key=f"apply_single_{i}_{match.canonical_url[:30]}"):
                    _apply_single_job(match)


def _apply_single_job(match) -> None:
    """Apply to a single job immediately."""
    runtime_profile = st.session_state.get("runtime_profile")
    dry_run = st.session_state.get("dry_run", True)
    ats_name = detect_ats(match.canonical_url) if detect_ats else "generic"
    job_dict = {
        "company": match.company,
        "title": match.title,
        "canonical_url": match.canonical_url,
        "source": ats_name,
        "ats": ats_name,
    }
    job_id = (match.raw_job or {}).get("job_id", "") or ""

    with st.spinner(f"Applying to {match.company}..."):
        if not dry_run and ApplicationEngine is not None:
            try:
                results = ApplicationEngine.run_sync(
                    [job_dict],
                    runtime_profile=runtime_profile,
                    headless=True,
                )
                result = results[0] if results else {"status": "failed"}
            except Exception as exc:
                result = {"status": "failed", "reason": str(exc)}
        else:
            result = {"status": "dry_run"}

    status = result.get("status", "failed")
    if status in ("applied", "dry_run"):
        if _db is not None:
            try:
                _db.mark_applied(job_id, match.canonical_url, match.company, match.title, ats_name)
            except Exception:
                pass
        label = "Applied (dry run)!" if status == "dry_run" else "Applied!"
        st.success(f"{label} — {match.company}: {match.title}")
    else:
        st.error(f"Failed: {result.get('reason', 'unknown error')}")
    st.rerun()


def render_dashboard() -> None:
    profile = st.session_state.get("profile", {})
    first_name = profile.get("first_name", "")
    greeting = f"Welcome back, {first_name}." if first_name else "Welcome back."

    st.markdown(
        f'<div class="page-header">{greeting}</div>'
        '<div class="page-sub">Your autonomous job agent is ready.</div>',
        unsafe_allow_html=True,
    )

    # Check API keys
    api_keys: dict = {}
    if _db is not None:
        try:
            api_keys = _db.load_api_keys()
        except Exception:
            pass
    has_keys = bool(
        api_keys.get("jsearch_key")
        or (api_keys.get("adzuna_app_id") and api_keys.get("adzuna_app_key"))
    )

    jobs = st.session_state.get("jobs", [])
    threshold = st.session_state.get("match_threshold", DEFAULT_THRESHOLD)
    high_match_count = sum(1 for j in jobs if j.score >= threshold)
    today_applied = _get_today_applied_count()

    # KPI row
    k1, k2, k3 = st.columns(3)
    with k1:
        st.markdown(
            f'<div class="kpi-card"><div class="kpi-number">{len(jobs)}</div>'
            '<div class="kpi-label">Jobs Found</div></div>',
            unsafe_allow_html=True,
        )
    with k2:
        st.markdown(
            f'<div class="kpi-card"><div class="kpi-number">{high_match_count}</div>'
            '<div class="kpi-label">High Match</div></div>',
            unsafe_allow_html=True,
        )
    with k3:
        st.markdown(
            f'<div class="kpi-card"><div class="kpi-number">{today_applied}</div>'
            '<div class="kpi-label">Applied Today</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='margin-top:20px'></div>", unsafe_allow_html=True)

    # No API keys CTA
    if not has_keys:
        st.markdown(
            """
<div class="glass-card" style="text-align:center;padding:32px">
  <div style="font-size:2rem;margin-bottom:10px">&#128273;</div>
  <div style="font-weight:600;font-size:1rem;color:#E2E8F0;margin-bottom:6px">Connect Job Boards</div>
  <div style="color:#64748B;font-size:0.875rem;margin-bottom:16px">
    Add your JSearch or Adzuna API keys to start fetching real job listings.
  </div>
</div>
""",
            unsafe_allow_html=True,
        )
        if st.button("&#9881;  Go to Settings", key="cta_settings"):
            st.session_state["page"] = "settings"
            st.rerun()
        return

    # Fetch / refresh controls
    fetch_col, refresh_col = st.columns([3, 1])
    with fetch_col:
        if not jobs:
            if st.button("&#128269;  Fetch Jobs", key="fetch_jobs_btn", use_container_width=True):
                _fetch_and_rank_jobs(force_refresh=True)
                st.rerun()
    with refresh_col:
        if jobs:
            if st.button("&#8635; Refresh", key="refresh_jobs_btn", use_container_width=True):
                _fetch_and_rank_jobs(force_refresh=True)
                st.rerun()

    # Auto-apply agent control strip
    applying = st.session_state.get("applying", False)
    dry_run = st.session_state.get("dry_run", True)

    st.markdown("<div style='margin-top:8px'></div>", unsafe_allow_html=True)

    if applying:
        # Running apply loop
        stop_col, _ = st.columns([1, 3])
        with stop_col:
            if st.button("&#9632;  Stop Agent", key="stop_apply_btn", use_container_width=True):
                st.session_state["apply_stop"] = True

        eligible = _build_eligible_jobs()
        runtime_profile = st.session_state.get("runtime_profile")
        try:
            _run_apply_loop(eligible, runtime_profile, dry_run)
        finally:
            st.session_state["applying"] = False
        st.rerun()
    else:
        mode_label = "(Dry Run)" if dry_run else "(Live)"
        eligible_count = len(_build_eligible_jobs())
        if jobs:
            start_col, _ = st.columns([2, 2])
            with start_col:
                btn_label = f"&#9654;  Start Auto-Apply {mode_label} &mdash; {eligible_count} eligible"
                if st.button(btn_label, key="start_apply_btn", use_container_width=True):
                    st.session_state["applying"] = True
                    st.session_state["apply_stop"] = False
                    st.session_state["apply_log"] = []
                    st.session_state["apply_progress"] = 0
                    st.rerun()

    # Apply log
    apply_log = st.session_state.get("apply_log", [])
    if apply_log:
        with st.expander(f"Apply Log ({len(apply_log)} entries)", expanded=False):
            for entry in apply_log:
                color = "#10B981" if "&#10003;" in entry or "&#9711;" in entry else "#F43F5E"
                st.markdown(
                    f'<div style="font-size:0.85rem;padding:3px 0;color:{color}">{entry}</div>',
                    unsafe_allow_html=True,
                )

    st.markdown("<div style='margin-top:16px'></div>", unsafe_allow_html=True)
    st.markdown('<div class="section-title">Job Feed</div>', unsafe_allow_html=True)

    # Filter pills
    filter_options = ["All", "High Match", "F-1 Safe", "Intern", "New Grad"]
    selected_filter = st.radio(
        "Filter",
        options=filter_options,
        index=filter_options.index(st.session_state.get("filter_active", "All")),
        horizontal=True,
        label_visibility="collapsed",
        key="dashboard_filter",
    )
    if selected_filter != st.session_state.get("filter_active"):
        st.session_state["filter_active"] = selected_filter

    _render_job_feed(jobs)


# ---------------------------------------------------------------------------
# Resume view
# ---------------------------------------------------------------------------

def render_resume() -> None:
    st.markdown(
        '<div class="page-header">&#128196;  Your Resume</div>'
        '<div class="page-sub">Parsed automatically from your uploaded file.</div>',
        unsafe_allow_html=True,
    )

    profile = st.session_state.get("profile") or {}
    if _db is not None and not profile:
        try:
            profile = _db.load_profile() or {}
        except Exception:
            pass

    if not profile:
        st.info("No profile found. Please upload your resume first.")
        if st.button("Upload Resume"):
            st.session_state["page"] = "upload"
            st.rerun()
        return

    first = profile.get("first_name", "")
    last = profile.get("last_name", "")
    full_name = f"{first} {last}".strip() or "Your Name"
    email = profile.get("email", "")
    phone = profile.get("phone", "")
    linkedin = profile.get("linkedin_url", "")
    github = profile.get("github_url", "")

    st.markdown(
        f"""
<div class="glass-card">
  <h2 style="margin:0 0 6px;font-size:1.5rem;color:#E2E8F0">{full_name}</h2>
  <div style="color:#64748B;font-size:0.875rem;display:flex;flex-wrap:wrap;gap:12px">
    {"<span>&#128139; " + email + "</span>" if email else ""}
    {"<span>&#128222; " + phone + "</span>" if phone else ""}
    {"<a href='" + linkedin + "' target='_blank' style='color:#818CF8;text-decoration:none'>LinkedIn</a>" if linkedin else ""}
    {"<a href='" + github + "' target='_blank' style='color:#818CF8;text-decoration:none'>GitHub</a>" if github else ""}
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

    # Education
    university = profile.get("university", "")
    degree = profile.get("degree", "")
    grad_year = profile.get("graduation_year", "")
    gpa = profile.get("gpa")
    if university or degree:
        gpa_str = f" &middot; GPA {gpa}" if gpa else ""
        st.markdown(
            f"""
<div class="glass-card">
  <div class="section-title">Education</div>
  <div style="font-weight:600;color:#E2E8F0">{university or "University"}</div>
  <div style="color:#64748B;font-size:0.875rem">{degree or ""} &middot; {grad_year or ""}{gpa_str}</div>
</div>
""",
            unsafe_allow_html=True,
        )

    # Skills
    skills_raw = profile.get("skills", "")
    if skills_raw:
        skills_list = [s.strip() for s in skills_raw.split(",") if s.strip()]
        chips = "".join(f'<span class="skill-chip">{s}</span>' for s in skills_list)
        st.markdown(
            f'<div class="glass-card"><div class="section-title">Skills</div><div>{chips}</div></div>',
            unsafe_allow_html=True,
        )

    # Runtime profile keywords
    runtime_profile = st.session_state.get("runtime_profile")
    if runtime_profile and hasattr(runtime_profile, "suggested_roles") and runtime_profile.suggested_roles:
        roles_html = "".join(
            f'<span class="skill-chip">{r.get("title", r) if isinstance(r, dict) else r}</span>'
            for r in runtime_profile.suggested_roles[:8]
        )
        st.markdown(
            f'<div class="glass-card"><div class="section-title">Suggested Roles</div><div>{roles_html}</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='margin-top:16px'></div>", unsafe_allow_html=True)
    if st.button("&#8679;  Re-upload Resume", key="reupload_btn"):
        st.session_state["page"] = "upload"
        st.rerun()


# ---------------------------------------------------------------------------
# Jobs view (full-screen)
# ---------------------------------------------------------------------------

def render_jobs() -> None:
    st.markdown(
        '<div class="page-header">&#128269;  Job Listings</div>'
        '<div class="page-sub">All fetched jobs with advanced filters.</div>',
        unsafe_allow_html=True,
    )

    # Keyword search
    search_col, btn_col = st.columns([4, 1])
    with search_col:
        keyword_search = st.text_input(
            "Search jobs",
            placeholder="e.g. machine learning, backend engineer",
            key="jobs_search_input",
            label_visibility="collapsed",
        )
    with btn_col:
        if st.button("&#128269;  Search", key="jobs_search_btn", use_container_width=True):
            if keyword_search:
                st.session_state["keyword_override"] = keyword_search
            _fetch_and_rank_jobs(force_refresh=True)
            st.rerun()

    # Filters row
    f1, f2, f3 = st.columns(3)
    with f1:
        filter_options = ["All", "High Match", "F-1 Safe", "Intern", "New Grad"]
        selected_filter = st.selectbox(
            "Role type",
            filter_options,
            index=filter_options.index(st.session_state.get("filter_active", "All")),
            key="jobs_filter_select",
            label_visibility="visible",
        )
        if selected_filter != st.session_state.get("filter_active"):
            st.session_state["filter_active"] = selected_filter

    with f2:
        st.selectbox(
            "Employment type",
            ["Any", "Internship", "Full-time", "Part-time", "Contract"],
            key="jobs_emp_type",
            label_visibility="visible",
        )

    with f3:
        if st.button("&#8635;  Refresh Jobs", key="jobs_refresh_btn", use_container_width=True):
            _fetch_and_rank_jobs(force_refresh=True)
            st.rerun()

    st.markdown("<div style='margin-top:8px'></div>", unsafe_allow_html=True)

    jobs = st.session_state.get("jobs", [])
    if not jobs:
        api_keys: dict = {}
        if _db is not None:
            try:
                api_keys = _db.load_api_keys()
            except Exception:
                pass
        has_keys = bool(
            api_keys.get("jsearch_key")
            or (api_keys.get("adzuna_app_id") and api_keys.get("adzuna_app_key"))
        )
        if not has_keys:
            st.markdown(
                """
<div class="glass-card" style="text-align:center;padding:32px">
  <div style="font-size:2rem;margin-bottom:10px">&#128273;</div>
  <div style="font-weight:600;font-size:1rem;color:#E2E8F0;margin-bottom:6px">No API Keys Configured</div>
  <div style="color:#64748B;font-size:0.875rem">Add JSearch or Adzuna API keys in Settings to fetch jobs.</div>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            if st.button("&#128269;  Fetch Jobs Now", key="jobs_fetch_now_btn", use_container_width=True):
                _fetch_and_rank_jobs(force_refresh=True)
                st.rerun()
    else:
        _render_job_feed(jobs, show_apply_buttons=True)


# ---------------------------------------------------------------------------
# Applied view
# ---------------------------------------------------------------------------

def render_applied() -> None:
    st.markdown(
        '<div class="page-header">&#128640;  Applications</div>'
        '<div class="page-sub">Track every application submitted by the agent.</div>',
        unsafe_allow_html=True,
    )

    if _db is None:
        st.error("Database not available.")
        return

    try:
        applied = _db.get_applied_jobs()
    except Exception as exc:
        st.error(f"Could not load applications: {exc}")
        return

    if not applied:
        st.markdown(
            """
<div class="glass-card" style="text-align:center;padding:48px 24px">
  <div style="font-size:2.5rem;margin-bottom:12px">&#128640;</div>
  <div style="font-size:1rem;font-weight:600;color:#E2E8F0;margin-bottom:6px">No Applications Yet</div>
  <div style="color:#64748B;font-size:0.875rem">
    Once the agent starts applying, all submissions will appear here.
  </div>
</div>
""",
            unsafe_allow_html=True,
        )
        return

    import pandas as pd
    rows = []
    for job in applied:
        applied_at = job.get("applied_at", "")
        if applied_at:
            try:
                dt = datetime.fromisoformat(str(applied_at).replace("Z", "+00:00"))
                applied_at = dt.strftime("%b %d, %Y %H:%M")
            except Exception:
                pass
        rows.append({
            "Company": job.get("company", ""),
            "Title": job.get("title", ""),
            "Platform": job.get("platform", ""),
            "Applied At": applied_at,
            "Status": job.get("status", "applied").capitalize(),
        })

    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Status": st.column_config.TextColumn("Status"),
            "Applied At": st.column_config.TextColumn("Applied At"),
        },
    )


# ---------------------------------------------------------------------------
# Settings view
# ---------------------------------------------------------------------------

def render_settings() -> None:
    st.markdown(
        '<div class="page-header">&#9881;  Settings</div>'
        '<div class="page-sub">Configure job board API keys and agent behaviour.</div>',
        unsafe_allow_html=True,
    )

    # Load current values
    current_keys: dict = {"jsearch_key": "", "adzuna_app_id": "", "adzuna_app_key": ""}
    if _db is not None:
        try:
            current_keys = _db.load_api_keys()
        except Exception:
            pass

    current_profile: dict = st.session_state.get("profile") or {}
    if _db is not None and not current_profile:
        try:
            current_profile = _db.load_profile() or {}
        except Exception:
            pass

    with st.form("settings_form"):
        st.markdown('<div class="section-title">&#128273;  Job Board API Keys</div>', unsafe_allow_html=True)
        st.markdown(
            "Get free API keys: "
            "[JSearch via RapidAPI](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) &nbsp;|&nbsp; "
            "[Adzuna Developer](https://developer.adzuna.com)",
            unsafe_allow_html=True,
        )

        jsearch_key = st.text_input(
            "JSearch API Key (RapidAPI)",
            value=current_keys.get("jsearch_key", ""),
            type="password",
            placeholder="Enter your RapidAPI key for JSearch",
        )
        adzuna_app_id = st.text_input(
            "Adzuna App ID",
            value=current_keys.get("adzuna_app_id", ""),
            placeholder="Your Adzuna application ID",
        )
        adzuna_app_key = st.text_input(
            "Adzuna App Key",
            value=current_keys.get("adzuna_app_key", ""),
            type="password",
            placeholder="Your Adzuna application key",
        )

        st.divider()
        st.markdown('<div class="section-title">&#128269;  Search Preferences</div>', unsafe_allow_html=True)

        keyword_override = st.text_input(
            "Job search keywords (optional — overrides resume skills)",
            value=st.session_state.get("keyword_override", ""),
            placeholder="e.g. software engineer intern, data science",
        )

        f1_filter = st.checkbox(
            "F-1 Visa Filter (skip jobs requiring clearance / no sponsorship)",
            value=st.session_state.get("f1_filter", True),
        )
        role_filter = st.checkbox(
            "Role Type Filter (intern / new grad / entry level only)",
            value=st.session_state.get("role_filter", True),
        )
        match_threshold = st.slider(
            "Minimum Match Score for Auto-Apply",
            min_value=0.70,
            max_value=0.97,
            value=float(st.session_state.get("match_threshold", DEFAULT_THRESHOLD)),
            step=0.01,
            format="%.0f%%",
            help="Only jobs scoring at or above this threshold will be auto-applied to.",
        )

        st.divider()
        st.markdown('<div class="section-title">&#128100;  Platform Credentials</div>', unsafe_allow_html=True)
        st.caption("Used by the apply agent to log in on job platforms (Workday, Greenhouse, etc.)")

        platform_email = st.text_input(
            "Platform Login Email",
            value=current_profile.get("email", ""),
            placeholder="your@email.com",
        )
        platform_password = st.text_input(
            "Platform Password",
            value="",
            type="password",
            placeholder="Your job platform password",
        )

        st.divider()
        st.markdown('<div class="section-title">&#9881;  Agent Behaviour</div>', unsafe_allow_html=True)

        dry_run = st.checkbox(
            "Dry Run Mode — fill forms but do NOT submit applications",
            value=st.session_state.get("dry_run", True),
            help="Uncheck this to allow the agent to actually submit applications.",
        )
        if dry_run:
            st.caption("&#128311;  Dry run is ON. The agent will fill forms but not click Submit.")
        else:
            st.caption("&#128308;  Dry run is OFF. The agent will submit real applications.")

        submitted = st.form_submit_button("Save Settings", use_container_width=True)

    if submitted:
        # Save API keys
        if _db is not None:
            try:
                _db.save_api_keys(jsearch_key, adzuna_app_id, adzuna_app_key)
            except Exception as exc:
                st.error(f"Could not save API keys: {exc}")

        # Update profile with platform credentials (email persisted; password stays in session only)
        updated_profile = dict(current_profile)
        if platform_email:
            updated_profile["email"] = platform_email
        if platform_email:
            platforms = {}
            if isinstance(updated_profile.get("platforms"), dict):
                platforms = updated_profile["platforms"]
            platforms["login_email"] = platform_email
            # Do NOT persist login_password to SQLite — store only in session_state
            updated_profile["platforms"] = platforms

        # Keep password in session_state only (not written to DB)
        if platform_password:
            st.session_state["platform_password"] = platform_password

        if _db is not None and updated_profile:
            try:
                _db.save_profile(updated_profile)
            except Exception as exc:
                st.error(f"Could not save profile: {exc}")

        # Update session state
        st.session_state["profile"] = updated_profile
        st.session_state["keyword_override"] = keyword_override
        st.session_state["f1_filter"] = f1_filter
        st.session_state["role_filter"] = role_filter
        st.session_state["match_threshold"] = match_threshold
        st.session_state["dry_run"] = dry_run

        # Clear job cache so next fetch uses new keys/preferences
        st.session_state["jobs"] = []
        st.session_state["jobs_raw"] = []
        st.session_state["jobs_last_fetched"] = None

        st.success("Settings saved! Job cache cleared — fetch fresh jobs from the Dashboard.")


# ---------------------------------------------------------------------------
# Main routing
# ---------------------------------------------------------------------------

def main() -> None:
    page = st.session_state.get("page", "upload")

    if page == "upload":
        render_upload_page()
    elif page == "dashboard":
        render_sidebar()
        render_dashboard()
    elif page == "resume":
        render_sidebar()
        render_resume()
    elif page == "jobs":
        render_sidebar()
        render_jobs()
    elif page == "applied":
        render_sidebar()
        render_applied()
    elif page == "settings":
        render_sidebar()
        render_settings()
    else:
        st.session_state["page"] = "upload"
        st.rerun()


main()
