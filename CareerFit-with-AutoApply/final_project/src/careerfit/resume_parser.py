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

# ── Experience / Education section patterns ──────────────────────────────────
_EXPERIENCE_SECTION_RE = re.compile(
    r"(?im)^\s*(work experience|experience|employment history|professional experience|work history)\s*[:\-]?\s*$"
)
_EDUCATION_SECTION_RE = re.compile(
    r"(?im)^\s*(education|academic background|academic history|qualifications|educational background)\s*[:\-]?\s*$"
)
_SECTION_HEADER_RE = re.compile(
    r"(?im)^\s*(skills|technical skills|certifications|projects|publications|awards|summary|objective|"
    r"volunteer|languages|interests|references|work experience|experience|employment history|"
    r"professional experience|work history|education|academic background|academic history|"
    r"qualifications|educational background)\s*[:\-]?\s*$"
)
_DATE_RANGE_RE = re.compile(
    r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[,\s]+\d{4}|\d{1,2}[/\-]\d{4}|\d{4})"
    r"\s*(?:\u2013|-|to|\u2014)\s*"
    r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[,\s]+\d{4}|\d{1,2}[/\-]\d{4}|\d{4}|"
    r"Present|present|Current|current|Now|now)",
    re.I,
)


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

def _extract_section_text(text: str, section_re: re.Pattern) -> str:
    """Return the text between the first match of section_re and the next section header."""
    m = section_re.search(text)
    if not m:
        return ""
    start = m.end()
    # Find next section header after start
    next_m = _SECTION_HEADER_RE.search(text, start)
    end = next_m.start() if next_m else len(text)
    return text[start:end]


def _extract_experience_entries(text: str) -> list:
    """
    Parse the work experience section of a resume into structured entries.

    Returns a list of dicts with keys:
      company, title, start_date, end_date, description, is_current
    Returns [] if no experience section is found or on any error.
    """
    try:
        section = _extract_section_text(text, _EXPERIENCE_SECTION_RE)
        if not section.strip():
            return []

        lines = [ln.rstrip() for ln in section.splitlines()]
        entries: list = []

        i = 0
        while i < len(lines) and len(entries) < 5:
            line = lines[i].strip()
            m = _DATE_RANGE_RE.search(line)
            if m:
                start_date = m.group(1).strip()
                end_date = m.group(2).strip()
                is_current = end_date.lower() in ("present", "current", "now")

                # Look backwards (up to 3 lines) for title and company
                # Convention: line immediately before date = title, line before that = company
                title = ""
                company = ""
                preceding_lines = []
                for back in range(1, 4):
                    idx = i - back
                    if idx < 0:
                        break
                    candidate = lines[idx].strip()
                    if not candidate or _DATE_RANGE_RE.search(candidate):
                        break
                    preceding_lines.append(candidate)
                # preceding_lines[0] = closest to date line (title), [1] = company
                if len(preceding_lines) >= 2:
                    title = preceding_lines[0]
                    company = preceding_lines[1]
                elif len(preceding_lines) == 1:
                    title = preceding_lines[0]

                # Dates and title/company may be on the same line
                # (e.g. "Software Engineer | Google   Jan 2022 - May 2023")
                line_no_date = _DATE_RANGE_RE.sub("", line).strip().strip("|,;-").strip()
                if line_no_date and not title and not company:
                    parts = re.split(r"\s*[\|,@]\s*", line_no_date, maxsplit=1)
                    if len(parts) == 2:
                        title, company = parts[0].strip(), parts[1].strip()
                    else:
                        title = parts[0].strip()
                elif line_no_date and not title:
                    title = line_no_date

                # Collect description bullets forward
                desc_lines: list = []
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].strip()
                    if _DATE_RANGE_RE.search(next_line) or _SECTION_HEADER_RE.search(next_line):
                        break
                    if next_line:
                        desc_lines.append(next_line.lstrip("-\u2022*").strip())
                    j += 1

                entries.append({
                    "company": company,
                    "title": title,
                    "start_date": start_date,
                    "end_date": end_date,
                    "description": " ".join(desc_lines[:6]),
                    "is_current": is_current,
                })
                i = j
                continue
            i += 1

        return entries
    except Exception:
        return []


def _extract_education_entries(text: str) -> list:
    """
    Parse the education section of a resume into structured entries.

    Returns a list of dicts with keys:
      school, degree, major, gpa, start_year, end_year, is_current
    Returns [] if no education section is found or on any error.
    """
    try:
        section = _extract_section_text(text, _EDUCATION_SECTION_RE)
        if not section.strip():
            return []

        # Split into blocks by blank lines or lines that look like school names
        lines = [ln.rstrip() for ln in section.splitlines()]
        # Identify school-name lines (contain university/college/institute keywords)
        school_line_re = re.compile(r"university|college|institute|school|academy", re.I)

        blocks: list = []
        current_block: list = []
        for line in lines:
            stripped = line.strip()
            if school_line_re.search(stripped) and current_block:
                blocks.append(current_block)
                current_block = [stripped]
            elif stripped:
                current_block.append(stripped)
        if current_block:
            blocks.append(current_block)

        entries: list = []
        # More precise degree regex requiring word boundary (avoids matching "Be" in "Berkeley")
        _BLOCK_DEGREE_RE = re.compile(
            r"\b(B\.S\.|B\.E\.|Bachelor(?:'s)?|M\.S\.|Master(?:'s)?|Ph\.D\.?|B\.Sc\.|M\.Sc\.|"
            r"B\.Tech\.|M\.Tech\.|Associate(?:'s)?|A\.S\.|A\.A\.)"
            r"(?:[\s,]+(?:of|in)[\s]+([A-Za-z\s]{2,45}))?",
            re.I,
        )
        for block in blocks[:3]:
            block_text = "\n".join(block)

            school_m = _UNIVERSITY_RE.search(block_text)
            school = school_m.group(1).strip() if school_m else (block[0] if block else "")

            deg_m = _BLOCK_DEGREE_RE.search(block_text)
            if deg_m:
                degree = deg_m.group(1).strip()
                major = (deg_m.group(2) or "").strip()
            else:
                degree = ""
                major = ""

            # If degree found but no major captured yet, try the line containing the degree
            if degree and not major:
                for bl in block:
                    if re.search(re.escape(degree), bl, re.I):
                        # Remove the degree abbreviation then take what remains as major
                        after = re.sub(
                            r"\b(?:B\.S\.|B\.E\.|Bachelor(?:'s)?|M\.S\.|Master(?:'s)?|Ph\.D\.?|"
                            r"B\.Sc\.|M\.Sc\.|B\.Tech\.|M\.Tech\.|Associate(?:'s)?|A\.S\.|A\.A\.)\s*(?:of|in)?\s*",
                            "", bl, flags=re.I
                        ).strip().strip(",;:").strip()
                        if after and len(after) > 2 and not school_line_re.search(after):
                            major = after
                        break

            # Try to find major after "in" or "of" within non-school lines
            if not major:
                for bl in block:
                    if school_line_re.search(bl):
                        continue  # skip school name lines
                    major_m = re.search(r"\b(?:in|of)\s+([A-Za-z][A-Za-z\s]{2,40})", bl, re.I)
                    if major_m:
                        candidate = major_m.group(1).strip()
                        if not school_line_re.search(candidate):
                            major = candidate
                            break

            gpa_m = _GPA_RE.search(block_text)
            gpa = gpa_m.group(1) if gpa_m else ""

            # Extract years
            years_found = _ANY_YEAR_RE.findall(block_text)
            start_year = years_found[0] if years_found else ""
            end_year = years_found[-1] if len(years_found) > 1 else ""

            # is_current if "present" or "current" mentioned
            is_current = bool(re.search(r"\b(present|current|now)\b", block_text, re.I))
            if is_current and not end_year:
                end_year = "Present"

            entries.append({
                "school": school,
                "degree": degree,
                "major": major,
                "gpa": gpa,
                "start_year": start_year,
                "end_year": end_year,
                "is_current": is_current,
            })

        return entries
    except Exception:
        return []




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
        "first_name":         first_name,
        "last_name":          last_name,
        "email":              email,
        "phone":              phone,
        "linkedin_url":       linkedin_url,
        "github_url":         github_url,
        "gpa":                gpa,
        "graduation_year":    graduation_year,
        "university":         university,
        "degree":             degree,
        "skills_text":        skills_text,
        "raw_text":           text,
        "experience_entries": _extract_experience_entries(text),
        "education_entries":  _extract_education_entries(text),
    }


def merge_parsed_into_profile(parsed: dict, existing_profile: dict) -> dict:
    """
    Merge non-empty parsed resume values into existing_profile.

    Existing non-empty values take precedence -- parsing never overwrites
    information the user has already entered.

    For list fields (experience_entries, education_entries): only replace
    if parsed has entries AND existing is empty.
    """
    merged = dict(existing_profile)
    _LIST_FIELDS = {"experience_entries", "education_entries"}
    for key, parsed_val in parsed.items():
        if key in _LIST_FIELDS:
            # Only set if parsed is non-empty and existing is empty/None/[]
            if parsed_val:
                current = merged.get(key)
                if not current:
                    merged[key] = parsed_val
        else:
            if not parsed_val:
                continue
            current = merged.get(key)
            if not current:          # empty / None / 0 / [] -> use parsed value
                merged[key] = parsed_val
    return merged
