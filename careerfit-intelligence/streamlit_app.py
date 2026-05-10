from __future__ import annotations

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
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pandas as pd
import streamlit as st

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

APP_TITLE = "CareerFit Studio"
DEFAULT_THRESHOLD = float(os.getenv("CAREERFIT_DEFAULT_THRESHOLD", "0.85"))
RUNTIME_COMPANIES_PATH = ROOT / "data" / "runtime_companies.json"

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

CSS = r"""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;0,9..40,800;1,9..40,400&family=DM+Mono:wght@400;500&display=swap');

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
  --sidebar: #08101E;
  --radius-sm: 10px;
  --radius:    16px;
  --radius-lg: 22px;
  --radius-xl: 28px;
  --shadow-sm: 0 1px 4px rgba(15,23,42,.06), 0 4px 12px rgba(15,23,42,.04);
  --shadow:    0 4px 16px rgba(15,23,42,.08), 0 12px 32px rgba(15,23,42,.05);
  --shadow-lg: 0 8px 32px rgba(15,23,42,.10), 0 24px 56px rgba(15,23,42,.07);
  --shadow-blue: 0 8px 24px rgba(37,99,235,.22);
  --transition: 0.18s cubic-bezier(.4,0,.2,1);
}

html, body, [class*="css"], .stApp {
  font-family: 'DM Sans', ui-sans-serif, system-ui, sans-serif;
  font-feature-settings: 'kern' 1, 'liga' 1;
}

.stApp {
  background: #F4F6FB;
  background-image:
    radial-gradient(ellipse 80% 50% at 0% 0%, rgba(37,99,235,.07) 0%, transparent 60%),
    radial-gradient(ellipse 60% 40% at 100% 100%, rgba(6,182,212,.05) 0%, transparent 60%);
  color: var(--ink);
}
section.main > div { padding-top: 0.75rem; }
#MainMenu, footer { visibility: hidden; }
.block-container { padding-left: 2rem; padding-right: 2rem; max-width: 1600px; }

/* SIDEBAR */
[data-testid="stSidebar"] {
  background: linear-gradient(160deg, #08101E 0%, #0D1829 55%, #0F1F35 100%);
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

/* BRAND */
.cf-brand { display:flex; gap:12px; align-items:center; padding:12px 0 20px; border-bottom:1px solid rgba(255,255,255,.07); margin-bottom:20px; }
.cf-logo { width:42px; height:42px; border-radius:13px; display:flex; align-items:center; justify-content:center; font-weight:800; font-size:15px; color:#fff; background: linear-gradient(135deg, #2563EB 0%, #06B6D4 100%); box-shadow: 0 6px 20px rgba(37,99,235,.40); }
.cf-brand-title { font-size:14.5px; font-weight:700; line-height:1.1; color:#F1F5F9; letter-spacing:-.01em; }
.cf-brand-sub { font-size:11px; color:#64748B; margin-top:2px; }
.cf-side-label { margin-top:20px; margin-bottom:6px; font-size:10px; letter-spacing:.12em; color:#475569; font-weight:700; text-transform:uppercase; }
.cf-side-note { padding:11px 13px; border-radius:12px; background: rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.07); color:#94A3B8; font-size:12px; line-height:1.6; }

/* HERO */
.cf-hero { position:relative; overflow:hidden; padding:36px 38px; border-radius:var(--radius-xl); background: linear-gradient(135deg, #0A1628 0%, #162040 40%, #0C3254 70%, #0E3D5C 100%); color:#fff; margin-bottom:24px; box-shadow: var(--shadow-lg); }
.cf-hero:before { content:""; position:absolute; top:-60%; right:-10%; width:500px; height:500px; border-radius:999px; background: radial-gradient(circle, rgba(59,130,246,.30) 0%, transparent 65%); pointer-events:none; }
.cf-hero:after { content:""; position:absolute; bottom:-80px; right:60px; width:260px; height:260px; border-radius:999px; background: radial-gradient(circle, rgba(16,185,129,.22) 0%, transparent 65%); pointer-events:none; }
.cf-hero-content { position:relative; z-index:1; }
.cf-eyebrow { display:inline-flex; align-items:center; gap:7px; border:1px solid rgba(255,255,255,.15); background:rgba(255,255,255,.08); padding:6px 12px; border-radius:999px; font-size:11.5px; font-weight:700; color:#BFDBFE; margin-bottom:14px; letter-spacing:.03em; }
.cf-hero h1 { margin:0; font-size: clamp(28px, 3.5vw, 52px); letter-spacing:-.045em; line-height:1.04; color:#fff; font-weight:800; }
.cf-hero p { margin:14px 0 0; color:#93C5FD; font-size:16px; line-height:1.7; max-width:900px; font-weight:400; }
.cf-toolbar { display:flex; flex-wrap:wrap; gap:9px; margin-top:22px; }
.cf-chip { display:inline-flex; gap:6px; align-items:center; border-radius:999px; padding:7px 13px; font-size:12px; font-weight:600; background:#EEF4FF; color:#1D4ED8; border:1px solid #DBEAFE; transition: box-shadow var(--transition); }
.cf-chip:hover { box-shadow: 0 4px 12px rgba(37,99,235,.18); }
.cf-chip-dark { background:rgba(255,255,255,.09); color:#E0F2FE; border-color:rgba(255,255,255,.14); }

/* CARDS */
.cf-card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 24px; box-shadow: var(--shadow); margin-bottom: 18px; transition: box-shadow var(--transition); }
.cf-card:hover { box-shadow: var(--shadow-lg); }
.cf-card h3, .cf-section-title { font-size: 16px; margin: 0 0 8px; color: var(--ink); font-weight: 700; letter-spacing: -.025em; }
.cf-muted { color: var(--muted); font-size: 13.5px; line-height: 1.65; }

/* METRICS */
.cf-grid { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:14px; margin-bottom:20px; }
.cf-metric { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 20px 22px; box-shadow: var(--shadow-sm); position: relative; overflow: hidden; transition: transform var(--transition), box-shadow var(--transition); }
.cf-metric:hover { transform: translateY(-2px); box-shadow: var(--shadow); }
.cf-metric:before { content:""; position:absolute; top:0; left:0; right:0; height:3px; background: linear-gradient(90deg, var(--blue), var(--cyan)); border-radius: var(--radius-lg) var(--radius-lg) 0 0; }
.cf-metric-label { color:var(--muted-2); text-transform:uppercase; letter-spacing:.08em; font-size:10.5px; font-weight:700; }
.cf-metric-value { color:var(--ink); font-size:32px; font-weight:800; letter-spacing:-.04em; margin-top:8px; line-height:1; }
.cf-metric-sub { color:var(--muted); font-size:12px; margin-top:5px; }

/* HOW IT WORKS */
.cf-quick-grid { display:grid; grid-template-columns: 1.1fr 1fr; gap:18px; align-items:start; }
.cf-source-grid { display:grid; grid-template-columns: 1fr 1fr; gap:18px; }
.cf-command { border-radius: var(--radius-xl); padding: 26px; background: linear-gradient(135deg, #FFFFFF 0%, #F8FAFD 100%); border: 1px solid var(--line); box-shadow: var(--shadow); }
.cf-command-title { font-size:20px; font-weight:800; letter-spacing:-.04em; color:var(--ink); margin:0 0 6px; }
.cf-command-steps { display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:10px; margin-top:16px; }
.cf-step { padding:14px 16px; border-radius:var(--radius); background:#F8FAFC; border:1px solid var(--line); transition: background var(--transition), border-color var(--transition); }
.cf-step:hover { background:#EEF4FF; border-color:#BFDBFE; }
.cf-step-num { width:28px; height:28px; border-radius:9px; display:inline-flex; align-items:center; justify-content:center; background: linear-gradient(135deg, var(--blue), var(--blue-lt)); color:white; font-weight:800; font-size:12px; margin-bottom:9px; box-shadow: 0 3px 8px rgba(37,99,235,.3); }
.cf-step-title { font-size:13px; font-weight:700; color:var(--ink); }
.cf-step-desc { font-size:12px; color:var(--muted); line-height:1.5; margin-top:4px; }

/* JOB CARDS */
.cf-job { border-radius: var(--radius-xl); background: var(--panel); border: 1px solid var(--line); padding: 22px 24px; margin-bottom:14px; box-shadow: var(--shadow-sm); transition: box-shadow var(--transition), border-color var(--transition), transform var(--transition); }
.cf-job:hover { box-shadow: var(--shadow); border-color: #BFDBFE; transform: translateY(-1px); }
.cf-job-top { display:flex; gap:16px; align-items:flex-start; justify-content:space-between; }
.cf-company { display:flex; gap:14px; align-items:flex-start; }
.cf-company-logo { width:46px; height:46px; border-radius:14px; flex-shrink:0; display:flex; align-items:center; justify-content:center; background: linear-gradient(135deg, var(--blue), var(--cyan)); color:#fff; font-weight:800; font-size:15px; box-shadow: 0 6px 16px rgba(37,99,235,.25); }
.cf-job-title { font-size:17px; font-weight:700; letter-spacing:-.025em; color:var(--ink); margin-bottom:4px; }
.cf-job-meta { color:var(--muted); font-size:13px; display:flex; flex-wrap:wrap; gap:7px; align-items:center; }
.cf-score { min-width:76px; text-align:center; border-radius:14px; padding:10px 14px; font-size:19px; font-weight:800; color:#065F46; background:linear-gradient(135deg,#D1FAE5,#A7F3D0); border:1px solid #6EE7B7; box-shadow: 0 4px 12px rgba(16,185,129,.15); }
.cf-score-mid { color:#92400E; background:linear-gradient(135deg,#FEF3C7,#FDE68A); border-color:#FCD34D; box-shadow: 0 4px 12px rgba(245,158,11,.15); }
.cf-score-low { color:#991B1B; background:linear-gradient(135deg,#FEE2E2,#FECACA); border-color:#FCA5A5; box-shadow: 0 4px 12px rgba(244,63,94,.12); }
.cf-progress { height:6px; background:var(--line-2); border-radius:999px; overflow:hidden; margin:16px 0 12px; }
.cf-progress span { display:block; height:100%; border-radius:999px; background:linear-gradient(90deg,var(--blue),var(--cyan),var(--mint)); }

/* TAGS */
.cf-tags { display:flex; gap:7px; flex-wrap:wrap; margin-top:12px; }
.cf-tag { border-radius:999px; padding:5px 11px; font-size:11.5px; font-weight:600; background:#F1F5F9; color:#475569; border:1px solid var(--line); }
.cf-tag-green { background:#ECFDF5; color:#047857; border-color:#A7F3D0; }
.cf-tag-purple { background:#F5F3FF; color:#6D28D9; border-color:#DDD6FE; }
.cf-tag-blue { background:#EFF6FF; color:#1D4ED8; border-color:#BFDBFE; }

/* ALERTS */
.cf-alert { padding:13px 16px; border-radius:var(--radius); font-size:13.5px; line-height:1.6; margin:10px 0; }
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
.cf-mini-row { padding:12px 14px; border-radius:var(--radius); border:1px solid var(--line); background:var(--panel); display:flex; justify-content:space-between; gap:12px; align-items:center; transition: border-color var(--transition), box-shadow var(--transition); }
.cf-mini-row:hover { border-color:#BFDBFE; box-shadow: 0 4px 12px rgba(37,99,235,.08); }
.cf-role { font-weight:700; font-size:13px; color:var(--ink-2); }
.cf-role-evidence { font-size:12px; color:var(--muted); margin-top:2px; }
.cf-pill-score { border-radius:999px; padding:4px 10px; font-size:12px; font-weight:700; background:#DCFCE7; color:#166534; }

/* TYPOGRAPHY */
.cf-subtitle { font-size:24px; font-weight:800; letter-spacing:-.04em; margin:28px 0 14px; color:var(--ink); }
.cf-small { font-size:12px; color:var(--muted); }

/* INPUTS (MAIN AREA) */
.main input, .main textarea, .stTextInput input, .stTextArea textarea,
[data-baseweb="input"] input, [data-baseweb="textarea"] textarea {
  background: #FFFFFF !important;
  color: var(--ink) !important;
  -webkit-text-fill-color: var(--ink) !important;
  caret-color: var(--blue) !important;
  border: 1.5px solid var(--line) !important;
  border-radius: var(--radius-sm) !important;
  font-family: 'DM Sans', sans-serif !important;
  font-size: 14px !important;
}
.main input:focus, .main textarea:focus,
[data-baseweb="input"] input:focus, [data-baseweb="textarea"] textarea:focus {
  border-color: var(--blue) !important;
  box-shadow: 0 0 0 3px rgba(37,99,235,.12) !important;
}
.main input::placeholder, .main textarea::placeholder { color:#CBD5E1 !important; -webkit-text-fill-color:#CBD5E1 !important; opacity:1 !important; }

/* FILE UPLOADER */
[data-testid="stFileUploader"] section,
[data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"],
[data-testid="stFileUploaderDropzone"],
[data-testid="stFileUploader"] > section > div {
  background: #FAFBFF !important;
  border: 1.5px dashed #C7D7F5 !important;
  border-radius: var(--radius) !important;
}
[data-testid="stFileUploader"] section:hover { border-color: var(--blue) !important; background: #EEF4FF !important; }
[data-testid="stFileUploader"] span,
[data-testid="stFileUploader"] p,
[data-testid="stFileUploader"] small { color: var(--muted) !important; -webkit-text-fill-color: var(--muted) !important; }
[data-testid="stFileUploader"] button,
[data-testid="stFileUploaderDropzone"] button { background: #FFFFFF !important; color: var(--ink) !important; -webkit-text-fill-color: var(--ink) !important; border: 1.5px solid var(--line) !important; border-radius: var(--radius-sm) !important; font-weight: 600 !important; }
[data-testid="stFileUploader"] [class*="uploadedFile"],
[data-testid="stFileUploader"] > div > div { background: #FAFBFF !important; color: var(--ink) !important; }

/* HELP ICON */
[data-testid="stTooltipIcon"] svg,
[data-testid="stTooltipHoverTarget"] svg { color: #CBD5E1 !important; fill: #CBD5E1 !important; }
.main svg { color: unset; fill: unset; }

/* BUTTONS */
.stButton > button, [data-testid="stFormSubmitButton"] > button {
  background: linear-gradient(135deg, var(--blue) 0%, var(--blue-lt) 100%) !important;
  color: #FFFFFF !important;
  -webkit-text-fill-color: #FFFFFF !important;
  border: none !important;
  border-radius: var(--radius-sm) !important;
  font-weight: 700 !important;
  font-size: 14px !important;
  font-family: 'DM Sans', sans-serif !important;
  box-shadow: var(--shadow-blue) !important;
  padding: 0.55em 1.2em !important;
  transition: opacity var(--transition), transform var(--transition) !important;
}
.stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover { opacity: 0.92 !important; transform: translateY(-1px) !important; }
[data-testid="stFormSubmitButton"]:first-child > button,
.stButton > button[kind="secondary"] {
  background: #FFFFFF !important;
  color: var(--ink-2) !important;
  -webkit-text-fill-color: var(--ink-2) !important;
  border: 1.5px solid var(--line) !important;
  box-shadow: var(--shadow-sm) !important;
}
[data-testid="stFormSubmitButton"]:first-child > button:hover,
.stButton > button[kind="secondary"]:hover {
  border-color: var(--blue) !important;
  color: var(--blue) !important;
  -webkit-text-fill-color: var(--blue) !important;
}

/* RESPONSIVE */
@media (max-width: 1180px) { .cf-grid, .cf-command-steps { grid-template-columns:1fr 1fr; } .cf-quick-grid, .cf-source-grid { grid-template-columns:1fr; } }
@media (max-width: 760px) { .block-container { padding-left:1rem; padding-right:1rem; } .cf-grid, .cf-command-steps { grid-template-columns:1fr; } .cf-hero { padding:24px; border-radius:var(--radius-lg); } .cf-job-top { flex-direction:column; } }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

DARK_MODE_CSS = r"""
<style>
.stApp { background: radial-gradient(circle at top left, #0B1220 0%, #111827 42%, #020617 100%) !important; color:#E5E7EB !important; }
.block-container { color:#E5E7EB !important; }
.cf-card, .cf-metric, .cf-command, .cf-job, .cf-mini-row, .cf-step {
  background: rgba(15,23,42,.86) !important;
  border-color: rgba(148,163,184,.26) !important;
  color: #E5E7EB !important;
  box-shadow: 0 18px 48px rgba(0,0,0,.26) !important;
}
.cf-card h3, .cf-section-title, .cf-job-title, .cf-metric-value, .cf-role, .cf-subtitle, .cf-command-title, .cf-step-title, h1,h2,h3,h4,h5,h6 { color:#F8FAFC !important; }
.cf-muted, .cf-job-meta, .cf-metric-sub, .cf-role-evidence, .cf-metric-label, .cf-step-desc, .cf-small { color:#A7B0C0 !important; }
.cf-progress { background: rgba(148,163,184,.30) !important; }
.cf-tag { background:rgba(30,41,59,.88) !important; color:#CBD5E1 !important; border-color:rgba(148,163,184,.28) !important; }
.cf-tag-green { background:rgba(6,95,70,.24) !important; color:#86EFAC !important; border-color:rgba(134,239,172,.32) !important; }
.cf-tag-blue { background:rgba(37,99,235,.22) !important; color:#BFDBFE !important; border-color:rgba(147,197,253,.34) !important; }
.cf-tag-purple { background:rgba(109,40,217,.24) !important; color:#DDD6FE !important; border-color:rgba(196,181,253,.34) !important; }
.cf-alert-info { background:rgba(30,64,175,.22) !important; color:#BFDBFE !important; border-color:rgba(147,197,253,.32) !important; }
.cf-alert-success { background:rgba(4,120,87,.18) !important; color:#A7F3D0 !important; border-color:rgba(167,243,208,.30) !important; }
.cf-alert-warn { background:rgba(146,64,14,.20) !important; color:#FDE68A !important; border-color:rgba(253,230,138,.30) !important; }
.cf-alert-error { background:rgba(153,27,27,.22) !important; color:#FECACA !important; border-color:rgba(254,202,202,.32) !important; }
[data-testid="stAppViewContainer"] [data-testid="stTextInput"] input,
[data-testid="stAppViewContainer"] [data-testid="stTextArea"] textarea,
[data-testid="stAppViewContainer"] [data-testid="stNumberInput"] input,
[data-testid="stAppViewContainer"] [data-baseweb="input"] input,
[data-testid="stAppViewContainer"] [data-baseweb="textarea"] textarea {
  background:#0F172A !important; color:#F8FAFC !important; -webkit-text-fill-color:#F8FAFC !important; caret-color:#60A5FA !important; border-color:rgba(148,163,184,.38) !important;
}
[data-testid="stAppViewContainer"] [data-baseweb="select"] > div { background:#0F172A !important; color:#F8FAFC !important; border-color:rgba(148,163,184,.38) !important; }
[data-testid="stAppViewContainer"] [data-baseweb="select"] span,
[data-testid="stAppViewContainer"] [data-baseweb="select"] div,
[data-testid="stAppViewContainer"] [data-baseweb="select"] svg { color:#F8FAFC !important; fill:#F8FAFC !important; }
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
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


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


def render_match_card(m: JobMatch, threshold: float) -> None:
    score = pct(m.score)
    widths = min(100, max(2, score))
    strengths = "".join(f"<span class='cf-tag cf-tag-green'>{escape(s)}</span>" for s in m.matched_strengths[:5])
    concerns = "".join(f"<span class='cf-tag'>{escape(s)}</span>" for s in m.gaps_or_concerns[:3])
    url = escape(m.canonical_url or "#")
    profile_ready = bool(getattr(m, "profile_ready", False))
    score_label = f"{score}%" if profile_ready else f"{score}%<br/><span style='font-size:10px;font-weight:800'>search score</span>"
    score_css = score_class(m.score, threshold) if profile_ready else "cf-score-low"
    source_label = escape(m.best_document or ("Profile not built" if not profile_ready else "Combined profile"))
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
            <div class='cf-score {score_css}'>{score_label}</div>
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
        "Entry-level only (0-2 yrs)": "entry level new grad junior associate",
        "Intern + Entry-level (recommended)": "intern entry level new grad junior associate",
        "All levels": "",
    }
    default_search = _level_to_search.get(experience_level, "intern entry level new grad junior associate")
    search_text = st.text_input(
        "Role search keyword (optional override)",
        value=os.getenv("CAREERFIT_DEFAULT_SEARCH", default_search),
        placeholder="Examples: intern, data analyst, software engineer, product manager",
        help="Optional keyword passed to supported ATS platforms. Leave as-is to use the experience-level preset above.",
    )
    max_years_experience = st.slider(
        "Max years of experience required",
        min_value=0, max_value=10, value=2, step=1,
        help="Roles asking for more than this many years of experience get penalized in ranking. Set to 10 to disable.",
    )
    location_mode = st.selectbox("Location preference", ["United States / Remote only", "Global"], index=0)
    include_unknown_locations = st.checkbox("Include roles with unspecified locations", value=False)
    fast_mode = st.toggle("Fast matching mode", value=True, help="Uses listing metadata for faster results. Disable only when deeper job-description fetching is required.")
    use_cache = st.toggle("Reuse recent career-site results", value=True)
    allow_unprofiled_scan = st.checkbox("Allow non-personalized search", value=False, help="Use only public job titles and search terms. Personalized match scoring requires resume, document, or website evidence.")
    if st.button("Clear cached job data", use_container_width=True):
        clear_fetch_cache()
        st.success("Cache cleared.")
    st.markdown("<div class='cf-side-note'>Single-workspace product flow: add candidate inputs, add company sources, run matching, and review results without switching pages.</div>", unsafe_allow_html=True)

# Hero
st.markdown(
    """
    <div class='cf-hero'>
      <div class='cf-hero-content'>
        <div class='cf-eyebrow'>CareerFit Intelligence Platform</div>
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
    render_metric(f"High-fit matches ≥ {pct(threshold)}%", str(high_count), st.session_state.last_scan_label)
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
            run_scan_action(threshold, search_text, location_mode, include_unknown_locations, fast_mode, use_cache, allow_unprofiled_scan, scan_all_companies, company_limit, parallel_workers, max_years_experience)
    if build_only:
        build_profile_action(url_blob, uploaded)
    if run_scan:
        run_scan_action(threshold, search_text, location_mode, include_unknown_locations, fast_mode, use_cache, allow_unprofiled_scan, scan_all_companies, company_limit, parallel_workers, max_years_experience)

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
        view = st.selectbox("View", ["High-fit matches", "All ranked roles", "Review queue"])

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
        render_match_card(m, threshold)

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
