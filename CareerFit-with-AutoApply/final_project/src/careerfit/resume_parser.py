"""
resume_parser.py
Lightweight regex-based resume parser.  Extracts structured fields from
PDF, DOCX, or plain-text resume content without heavy NLP dependencies.
"""
from __future__ import annotations

import re
from typing import Optional

from careerfit.source_intelligence import (
    extract_pdf_text,
    extract_docx_text,
    extract_skills,
)

# ── Patterns ────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I
)
_PHONE_RE = re.compile(
    r"(?:\+1[\s.\-]?)?(?:\(?[0-9]{3}\)?[\s.\-]?[0-9]{3}[\s.\-]?[0-9]{4})"
)
_LINKEDIN_RE = re.compile(r"linkedin\.com/in/[\w\-]+", re.I)
_GITHUB_RE   = re.compile(r"github\.com/[\w\-]+", re.I)
_GPA_RE      = re.compile(r"GPA[:\s]+([0-9]\.[0-9]{1,2})", re.I)
_GRAD_YEAR_RE = re.compile(
    r"(?:expected|graduation|class\s+of|degree|anticipated)[^\n]{0,40}(20[2-9][0-9])",
    re.I,
)
_ANY_YEAR_RE  = re.compile(r"\b(20[2-9][0-9])\b")
_UNIVERSITY_RE = re.compile(
    r"(?:^|\n)([^\n]{0,80}(?:university|college|institute|school)[^\n]{0,60})",
    re.I,
)
_DEGREE_RE = re.compile(
    r"(B\.?S\.?|B\.?E\.?|Bachelor(?:'s)?|M\.?S\.?|Master(?:'s)?|Ph\.?D\.?)"
    r"(?:[\s,]+(?:of|in)[\s]+([A-Za-z\s]{2,45}))?",
    re.I,
)
_NAME_SPLIT_RE = re.compile(r"[^a-zA-Z\s\-']")


# ── Extractors ───────────────────────────────────────────────────────────────

def _extract_name(text: str) -> tuple[str, str]:
    """Best-effort name extraction from the first 350 characters of the resume."""
    head = text[:350]
    # Drop lines that look like section headers or contact info
    for line in head.splitlines():
        line = line.strip()
        if not line or len(line) < 3:
            continue
        # Skip lines that contain @, http, digits, or are ALL CAPS section labels
        if re.search(r"[@:/\d]", line):
            continue
        if line.isupper() and len(line) > 20:
            continue
        words = line.split()
        # A name is typically 2-4 capitalized words with no punctuation
        if 2 <= len(words) <= 4 and all(w[0].isupper() for w in words if w):
            cleaned = _NAME_SPLIT_RE.sub("", line).strip()
            if cleaned:
                parts = cleaned.split()
                return parts[0], " ".join(parts[1:]) if len(parts) > 1 else ""
    return "", ""


def _extract_graduation_year(text: str) -> str:
    m = _GRAD_YEAR_RE.search(text)
    if m:
        return m.group(1)
    # Fall back: find the largest 202x year in the document
    years = [int(y) for y in _ANY_YEAR_RE.findall(text)]
    if years:
        return str(max(years))
    return ""


def _extract_university(text: str) -> str:
    m = _UNIVERSITY_RE.search(text)
    if m:
        return m.group(1).strip()
    return ""


def _extract_degree(text: str) -> str:
    m = _DEGREE_RE.search(text)
    if not m:
        return ""
    deg = m.group(1).strip()
    subject = (m.group(2) or "").strip()
    if subject:
        return f"{deg} in {subject}"
    return deg


# ── Public API ───────────────────────────────────────────────────────────────

def parse_resume(file_bytes: bytes, filename: str) -> dict:
    """
    Extract structured fields from a PDF, DOCX, or TXT resume.

    Returns a dict with keys:
      first_name, last_name, email, phone, linkedin_url, github_url,
      gpa, graduation_year, university, degree, skills_text
    Empty string for fields not found.
    """
    fname_lower = (filename or "").lower()
    if fname_lower.endswith(".pdf"):
        text = extract_pdf_text(file_bytes)
    elif fname_lower.endswith(".docx"):
        text = extract_docx_text(file_bytes)
    else:
        # Plain text or .txt
        text = file_bytes.decode("utf-8", errors="replace")

    first_name, last_name = _extract_name(text)

    email_m = _EMAIL_RE.search(text)
    email = email_m.group(0) if email_m else ""

    phone_m = _PHONE_RE.search(text)
    phone = phone_m.group(0).strip() if phone_m else ""

    linkedin_m = _LINKEDIN_RE.search(text)
    linkedin_url = ("https://" + linkedin_m.group(0)) if linkedin_m else ""

    github_m = _GITHUB_RE.search(text)
    github_url = ("https://" + github_m.group(0)) if github_m else ""

    gpa_m = _GPA_RE.search(text)
    gpa = gpa_m.group(1) if gpa_m else ""

    graduation_year = _extract_graduation_year(text)
    university = _extract_university(text)
    degree = _extract_degree(text)

    skills = extract_skills(text)
    skills_text = ", ".join(skills)

    return {
        "first_name":      first_name,
        "last_name":       last_name,
        "email":           email,
        "phone":           phone,
        "linkedin_url":    linkedin_url,
        "github_url":      github_url,
        "gpa":             gpa,
        "graduation_year": graduation_year,
        "university":      university,
        "degree":          degree,
        "skills_text":     skills_text,
        "raw_text":        text,
    }


def merge_parsed_into_profile(parsed: dict, existing_profile: dict) -> dict:
    """
    Merge non-empty parsed resume values into existing_profile.

    Existing non-empty values take precedence -- parsing never overwrites
    information the user has already entered.
    """
    merged = dict(existing_profile)
    for key, parsed_val in parsed.items():
        if not parsed_val:
            continue
        current = merged.get(key)
        if not current:          # empty / None / 0 / [] -> use parsed value
            merged[key] = parsed_val
    return merged
