"""
streamlit_apply_patch.py
Adds the Auto-Apply Queue panel + Apply buttons to CareerFit Intelligence.
Reads applicant profile from st.secrets (Streamlit Cloud) — no YAML file needed.
"""
from __future__ import annotations
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
SRC  = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_STORAGE_STATE = str(ROOT / "data" / "browser_session.json")

_STATUS_COLOR = {
    "success": "#10B981", "failed": "#F43F5E",
    "skipped": "#F59E0B", "dry_run": "#7C3AED",
}

# ── 1. Session state ──────────────────────────────────────────────────────────

def init_apply_session_state():
    defaults = {
        "apply_queue":        [],
        "apply_results":      [],
        "apply_running":      False,
        "apply_dry_run":      True,
        "apply_headless":     True,
        "apply_max_concurrent": 2,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ── 2. Queue button (goes inside render_match_card) ───────────────────────────

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


# ── 3. Sidebar panel ──────────────────────────────────────────────────────────

def render_apply_queue_panel() -> None:
    st.markdown("---")
    st.markdown("<div class='cf-side-label'>⚡ Auto-Apply Queue</div>", unsafe_allow_html=True)

    queue = st.session_state.apply_queue
    n = len(queue)

    if n == 0:
        st.markdown(
            "<div class='cf-side-note'>No jobs queued yet.<br>"
            "Click <b>⚡ Queue to Apply</b> on any job card.</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"<div class='cf-side-note'><b>{n}</b> job(s) in queue.</div>",
            unsafe_allow_html=True,
        )
        for i, job in enumerate(list(queue)):
            c1, c2 = st.columns([5, 1])
            with c1:
                chip = _chip(job.get("source", ""))
                st.markdown(
                    f"<span style='font-size:12px'>{chip} <b>{job['company']}</b>"
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

    # ── Agent settings ──────────────────────────────────────────────────────
    st.markdown("<div class='cf-side-label'>Agent Settings</div>", unsafe_allow_html=True)

    # Check if secrets are configured
    secrets_ok = _secrets_configured()
    if not secrets_ok:
        st.markdown(
            "<div class='cf-side-note' style='color:#F43F5E'>⚠ No profile found.<br>"
            "Go to Streamlit Cloud → App Settings → Secrets and paste your profile.</div>",
            unsafe_allow_html=True,
        )

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

    can_run = n > 0 and secrets_ok and not st.session_state.apply_running
    if st.button("🚀 Run Auto-Apply", type="primary", use_container_width=True,
                 disabled=not can_run, key="run_apply_btn"):
        st.session_state.apply_running = True
        st.rerun()


# ── 4. Engine runner (call at bottom of main body) ────────────────────────────

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
            # Inject dry_run into secrets dict for this run
            from careerfit.apply_agent.engine import ApplicationEngine, load_profile
            cfg = load_profile()
            cfg["dry_run"] = dry

            # Monkey-patch load_profile to return our modified cfg
            import careerfit.apply_agent.engine as _eng
            _orig = _eng.load_profile
            _eng.load_profile = lambda *a, **kw: cfg

            results = ApplicationEngine.run_sync(
                jobs=jobs,
                runtime_profile=st.session_state.get("profile"),
                headless=st.session_state.apply_headless,
                max_concurrent=st.session_state.apply_max_concurrent,
                storage_state=DEFAULT_STORAGE_STATE,
            )
            _eng.load_profile = _orig  # restore

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
        ok  = sum(1 for r in results if r.get("status") == "success")
        bad = sum(1 for r in results if r.get("status") == "failed")
        skip = sum(1 for r in results if r.get("status") == "skipped")
        note = " (dry run — not submitted)" if dry else ""
        st.success(f"Done{note}: {ok} ✅  {skip} ⏭  {bad} ❌")
    st.rerun()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _secrets_configured() -> bool:
    try:
        return hasattr(st, "secrets") and bool(st.secrets.get("email"))
    except Exception:
        return False


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


def _chip(source: str) -> str:
    colours = {
        "greenhouse":      ("#22C55E", "#F0FDF4"),
        "workday":         ("#3B82F6", "#EFF6FF"),
        "lever":           ("#A855F7", "#FAF5FF"),
        "ashby":           ("#F59E0B", "#FFFBEB"),
        "smartrecruiters": ("#EF4444", "#FEF2F2"),
        "amazon":          ("#F97316", "#FFF7ED"),
        "microsoft":       ("#0EA5E9", "#F0F9FF"),
    }
    fg, bg = colours.get((source or "").lower(), ("#64748B", "#F1F5F9"))
    label = source.title() if source else "?"
    return (f"<span style='background:{bg};color:{fg};padding:2px 8px;"
            f"border-radius:999px;font-size:11px;font-weight:700'>{label}</span>")
