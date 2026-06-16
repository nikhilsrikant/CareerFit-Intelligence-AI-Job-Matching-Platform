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


def build_apply_cfg(db_profile: dict, platform: str) -> dict:
    """Build the applicant config dict for a specific platform from the DB profile.

    Merges global profile fields with per-platform credentials.
    Falls back to global email/password if no per-platform entry exists.
    """
    cfg = dict(db_profile)
    platforms_data = cfg.pop("platforms", None) or {}
    if isinstance(platforms_data, str):
        try:
            import json as _json
            platforms_data = _json.loads(platforms_data)
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
    """
    Continuous auto-apply loop. Fetches ranked matches from session_state, filters
    by threshold + F-1/role decisions + duplicate check, and applies sequentially.
    Updates progress in real time via st.empty().
    """
    if threshold < 0.90:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            f"Threshold {threshold:.2f} below 0.90 minimum — forcing to 0.90"
        )
        threshold = 0.90
    import time as _time
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

    # Build apply list: score threshold + not filtered + not already applied
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
    results_container = st.empty()
    all_results = []

    # NOTE: This is a synchronous loop that blocks the Streamlit render thread.
    # For a production deployment, this should run in a background thread.
    # For single-user local use, this is acceptable.
    st.warning("Auto-apply is running synchronously. The browser tab will be unresponsive until complete. Use the Stop button to interrupt between applications.")

    for i, match in enumerate(apply_list):
        # Check stop flag
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

        # Build per-platform config
        apply_cfg = build_apply_cfg(db_profile, match.source)
        apply_cfg["dry_run"] = dry_run

        # Monkey-patch load_profile to return our cfg for this run
        # NOTE: this module-level attribute swap is safe for single-user local deployments.
        # In a multi-user production deployment, refactor ApplicationEngine to accept
        # profile_dict as a constructor argument to eliminate the patch.
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
            _eng.load_profile = _orig_lp

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
            st.session_state.apply_results = all_results  # also show in sidebar panel

        # Delay between applications (skip on last)
        # NOTE: time.sleep here keeps the render thread blocked for the full delay.
        if i < len(apply_list) - 1 and not st.session_state.get("continuous_apply_stop", False):
            _time.sleep(delay_secs)

    st.session_state.continuous_apply_running = False
    st.session_state.continuous_apply_stop = False
    ok   = sum(1 for r in all_results if r.get("status") == "success")
    bad  = sum(1 for r in all_results if r.get("status") == "failed")
    skip = sum(1 for r in all_results if r.get("status") == "skipped")
    note = " (dry run — not submitted)" if dry_run else ""
    progress_container.success(f"Auto-apply complete{note}: {ok} ✅  {skip} ⏭  {bad} ❌  out of {len(all_results)} attempted")
    st.rerun()


# ── 1. Session state ──────────────────────────────────────────────────────────

def init_apply_session_state():
    defaults = {
        "apply_queue":        [],
        "apply_results":      [],
        "apply_running":      False,
        "apply_dry_run":      True,
        "apply_headless":     True,
        "apply_max_concurrent": 2,
        "continuous_apply_running": False,
        "continuous_apply_stop": False,
        "continuous_apply_results": [],
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
                qa_answers=st.session_state.get("qa_answers", []),
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
