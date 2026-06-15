"""
SQLite persistence layer for CareerFit.

Tables:
  profile      - single-row user profile (id=1)
  applied_jobs - dedup log of submitted applications
  qa_answers   - user-supplied question/answer pairs for form auto-fill
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
# __file__ = final_project/src/careerfit/db.py
# .parent       -> final_project/src/careerfit/
# .parent.parent -> final_project/src/
# .parent.parent.parent -> final_project/
_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = _ROOT / "data" / "careerfit.db"


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    """Open (or create) the SQLite database and return a connection.

    Creates the parent directory if it does not yet exist.
    Sets row_factory to sqlite3.Row so rows behave like dicts.
    check_same_thread=False is required when the same connection is shared
    across threads (e.g. Streamlit's rerun model).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables if they do not already exist."""
    conn = get_conn()
    try:
        cur = conn.cursor()

        # -- profile (single row, id is always 1) ---------------------------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS profile (
                id                  INTEGER PRIMARY KEY DEFAULT 1,
                first_name          TEXT,
                last_name           TEXT,
                email               TEXT,
                phone               TEXT,
                address             TEXT,
                city                TEXT,
                state               TEXT,
                zip_code            TEXT,
                country             TEXT,
                linkedin_url        TEXT,
                github_url          TEXT,
                portfolio_url       TEXT,
                visa_status         TEXT,
                graduation_year     INTEGER,
                gpa                 REAL,
                university          TEXT,
                degree              TEXT,
                target_roles        TEXT,
                years_experience    INTEGER,
                available_start_date TEXT,
                salary_expectation  TEXT,
                work_authorization  TEXT,
                requires_sponsorship TEXT,
                referral_source     TEXT,
                cover_letter_text   TEXT,
                resume_path         TEXT,
                platforms           TEXT,
                updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # -- applied_jobs ----------------------------------------------------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS applied_jobs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id        TEXT UNIQUE,
                canonical_url TEXT,
                company       TEXT,
                title         TEXT,
                platform      TEXT,
                applied_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status        TEXT DEFAULT 'applied'
            )
        """)

        # -- qa_answers -------------------------------------------------------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qa_answers (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                question_pattern TEXT NOT NULL,
                answer           TEXT NOT NULL,
                created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# JSON helpers for list/dict fields stored as TEXT
# ---------------------------------------------------------------------------

_JSON_FIELDS = {"target_roles", "platforms"}


def _encode_profile(data: dict) -> dict:
    """JSON-encode list/dict fields before writing to SQLite."""
    out = dict(data)
    for field in _JSON_FIELDS:
        if field in out and not isinstance(out[field], str):
            out[field] = json.dumps(out[field])
    return out


def _decode_profile(row: dict) -> dict:
    """JSON-decode list/dict fields after reading from SQLite."""
    out = dict(row)
    for field in _JSON_FIELDS:
        if field in out and isinstance(out[field], str) and out[field]:
            try:
                out[field] = json.loads(out[field])
            except (json.JSONDecodeError, ValueError):
                pass  # leave as raw string if parsing fails
    return out


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------

def save_profile(data: dict) -> None:
    """Insert or replace the single profile row (id=1).

    Any list or dict values in the JSON fields (target_roles, platforms) are
    automatically serialised to JSON strings before storage.
    """
    encoded = _encode_profile(data)
    # Ensure id is always 1
    encoded["id"] = 1

    columns = ", ".join(encoded.keys())
    placeholders = ", ".join("?" for _ in encoded)
    values = list(encoded.values())

    conn = get_conn()
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO profile ({columns}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def load_profile() -> dict:
    """Return the stored profile as a plain dict, or {} if none exists."""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM profile WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            return {}
        return _decode_profile(dict(row))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Applied-jobs dedup
# ---------------------------------------------------------------------------

def mark_applied(
    job_id: str,
    canonical_url: str,
    company: str,
    title: str,
    platform: str,
) -> None:
    """Record a submitted application.  INSERT OR IGNORE prevents duplicates."""
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO applied_jobs
                (job_id, canonical_url, company, title, platform)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, canonical_url, company, title, platform),
        )
        conn.commit()
    finally:
        conn.close()


def is_already_applied(job_id: str, canonical_url: str) -> bool:
    """Return True if a job with the given id OR url has already been applied to."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "SELECT 1 FROM applied_jobs WHERE job_id = ? OR canonical_url = ? LIMIT 1",
            (job_id, canonical_url),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def get_applied_job_ids() -> "set[str]":
    """Return the set of all job_id values recorded in applied_jobs."""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT job_id FROM applied_jobs")
        return {row["job_id"] for row in cur.fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Q&A answer bank
# ---------------------------------------------------------------------------

def save_qa_answer(pattern: str, answer: str) -> None:
    """Persist a question-pattern / answer pair."""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO qa_answers (question_pattern, answer) VALUES (?, ?)",
            (pattern, answer),
        )
        conn.commit()
    finally:
        conn.close()


def load_qa_answers() -> "list[dict]":
    """Return all Q&A pairs ordered by insertion id."""
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM qa_answers ORDER BY id")
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def delete_qa_answer(answer_id: int) -> None:
    """Remove a single Q&A pair by its id."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM qa_answers WHERE id = ?", (answer_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bootstrap on import
# ---------------------------------------------------------------------------
init_db()
