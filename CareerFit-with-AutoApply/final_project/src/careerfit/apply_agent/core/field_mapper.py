"""
FieldMapper — bridges CareerFit's RuntimeProfile + applicant_profile.yaml → form values.

Design:
  - RuntimeProfile (already built by source_intelligence.py) supplies skills/roles/resume text.
  - applicant_profile.yaml supplies the structured personal data (name, email, phone …).
  - Matching uses multi-stage: exact label → synonym group → fuzzy ratio.
  - resolve() is the single public entry-point called by every ATS adapter.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Optional

# Optional import — gracefully absent during cold-start before the module is created
try:
    from careerfit.apply_agent.core.dropdown_matcher import resolve_dropdown_value as _resolve_dropdown_value
except ImportError:
    _resolve_dropdown_value = None  # type: ignore[assignment]


# ────────────────────────────────────────────────────────────────────────────
# Synonym groups — add more as you encounter new field labels in the wild
# ────────────────────────────────────────────────────────────────────────────
FIELD_SYNONYMS: dict[str, list[str]] = {
    "first_name":          ["first name", "given name", "fname", "first", "prénom"],
    "last_name":           ["last name", "family name", "surname", "lname", "last", "nom"],
    "full_name":           ["full name", "name", "your name", "legal name", "applicant name"],
    "email":               ["email", "e-mail", "email address", "work email", "personal email"],
    "phone":               ["phone", "phone number", "mobile", "cell", "telephone", "contact number", "mobile number"],
    "linkedin":            ["linkedin", "linkedin url", "linkedin profile", "linkedin profile url", "linkedin link"],
    "github":              ["github", "github url", "github profile", "portfolio", "github/portfolio"],
    "website":             ["website", "personal website", "portfolio url", "personal url", "personal site", "blog"],
    "location":            ["location", "city, state", "current location", "where are you located", "city/state"],
    "city":                ["city", "city name", "municipality"],
    "state":               ["state", "province", "region", "state/province"],
    "country":             ["country", "country of residence", "country/region"],
    "zip_code":            ["zip", "zip code", "postal code", "postcode"],
    "address":             ["address", "street address", "mailing address"],
    "resume":              ["resume", "cv", "upload resume", "attach resume", "upload cv", "resume/cv"],
    "cover_letter":        ["cover letter", "cover letter text", "why do you want to work here",
                            "why this company", "motivation letter", "why are you interested",
                            "tell us about yourself", "additional message"],
    "years_experience":    ["years of experience", "years experience", "how many years",
                            "years of relevant experience", "total years of experience"],
    "current_company":     ["current employer", "current company", "where do you work",
                            "employer", "most recent employer"],
    "current_title":       ["current title", "current job title", "current position",
                            "job title", "most recent title", "your title"],
    "start_date":          ["start date", "available start", "earliest start date",
                            "when can you start", "availability", "available from",
                            "desired start date"],
    "salary_expectation":  ["salary", "desired salary", "expected salary", "compensation",
                            "salary expectation", "pay expectation", "base salary expectation",
                            "expected compensation"],
    "work_authorization":  ["work authorization", "authorized to work", "visa status",
                            "are you legally authorized", "work permit", "right to work",
                            "us work authorization", "legally authorized"],
    "sponsorship":         ["sponsorship", "require sponsorship", "visa sponsorship",
                            "do you require sponsorship", "need sponsorship",
                            "will you now or in the future require sponsorship"],
    "gender":              ["gender", "gender identity", "pronouns"],
    "ethnicity":           ["ethnicity", "race", "race/ethnicity", "racial background"],
    "veteran_status":      ["veteran", "military", "veteran status", "are you a veteran",
                            "military service", "protected veteran"],
    "disability":          ["disability", "disability status", "do you have a disability",
                            "accommodation"],
    "referral":            ["how did you hear", "referral", "referred by", "source",
                            "how did you find this job", "where did you hear about us",
                            "how did you learn about this opening"],
    "university":          ["university", "school", "college", "institution", "alma mater"],
    "degree":              ["degree", "degree type", "highest education", "level of education",
                            "degree level", "degree earned"],
    "major":               ["field of study", "major", "major field", "area of study",
                            "program of study", "concentration", "specialization"],
    "graduation_year":     ["graduation year", "grad year", "year of graduation",
                            "expected graduation", "graduation date"],
    "gpa":                 ["gpa", "grade point average", "cumulative gpa"],
    "additional_info":     ["additional information", "anything else", "other information",
                            "tell us more", "comments", "additional comments",
                            "is there anything else"],
}


class FieldMapper:
    """
    Maps form field labels → values from the combined applicant profile.

    Usage:
        mapper = FieldMapper(applicant_cfg, runtime_profile)
        value = mapper.resolve("Current Job Title")
    """

    def __init__(self, applicant_cfg: dict, runtime_profile=None, qa_answers: "list[dict] | None" = None):
        """
        applicant_cfg: loaded from applicant_profile.yaml
        runtime_profile: careerfit.source_intelligence.RuntimeProfile (optional but recommended)
        qa_answers: list of dicts with 'question_pattern' and 'answer' keys
        """
        self._cfg = applicant_cfg
        self._profile = runtime_profile
        self._qa_answers = qa_answers or []
        self._cache: dict[str, Optional[Any]] = {}

    # ── Public API ──────────────────────────────────────────────────────────

    def resolve_qa(self, label: str) -> "Optional[Any]":
        """Try to resolve a form label using the user's pre-entered Q&A answer bank."""
        if not self._qa_answers:
            return None
        label_l = label.lower().strip()
        for qa in self._qa_answers:
            pattern = str(qa.get("question_pattern", "")).lower().strip()
            if not pattern:
                continue
            # Forward match: pattern substring of label
            if pattern in label_l:
                return qa.get("answer", "")
            # Reverse match: label substring of pattern
            if label_l in pattern:
                return qa.get("answer", "")
        return None

    def resolve(self, label: str, field_type: str = "text") -> Optional[Any]:
        """
        Return the value to fill for a given form label.
        field_type: 'text' | 'file' | 'select' | 'radio' | 'checkbox'
        Returns None if no mapping found.

        Priority order:
          1. Structured profile fields (_get_value) — explicit user data wins.
          2. Q&A answer bank (resolve_qa) — fallback for fields not in the profile.
        """
        key_label = label.lower().strip()
        cache_key = f"{key_label}::{field_type}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        profile_key = self._match_key(key_label)
        # Try structured profile first; fall back to Q&A only when profile has no value
        value = self._get_value(profile_key, field_type) if profile_key else None
        if value is None:
            # Try QA with the raw label first, then with every synonym for the matched key
            # so that e.g. a QA pattern "current company" fires for label "current employer"
            value = self.resolve_qa(key_label)
            if value is None and profile_key:
                for syn in FIELD_SYNONYMS.get(profile_key, []):
                    value = self.resolve_qa(syn)
                    if value is not None:
                        break
        self._cache[cache_key] = value
        return value

    def resolve_select(self, label: str, available_options: list[str]) -> "str | None":
        """
        Resolve the best available select option for *label* given a list of
        *available_options* scraped from the live dropdown.

        Priority:
          1. Use resolve() to get the raw profile value for this label.
          2. Case-insensitive exact check against available_options.
          3. Fuzzy + fallback chain via dropdown_matcher.resolve_dropdown_value().
        """
        raw = self.resolve(label, field_type="select")
        if not raw:
            return None
        raw_lower = str(raw).lower().strip()
        # Case-insensitive exact check first
        for opt in available_options:
            if opt.lower().strip() == raw_lower:
                return opt
        # Fuzzy + fallback chain
        try:
            from careerfit.apply_agent.core.dropdown_matcher import resolve_dropdown_value
            return resolve_dropdown_value(str(raw), available_options)
        except ImportError:
            return None

    def cover_letter_for_job(self, job_title: str, company: str) -> str:
        """
        Generate a tailored cover letter using runtime profile skills + applicant config.
        Falls back to a generic template if custom text is provided in config.
        """
        custom = self._cfg.get("cover_letter_template", "")
        if custom:
            return (custom
                    .replace("{job_title}", job_title)
                    .replace("{company}", company)
                    .replace("{first_name}", self._cfg.get("first_name", ""))
                    .replace("{skills}", ", ".join(self._top_skills(6))))

        # Auto-generate from profile
        name = f"{self._cfg.get('first_name', '')} {self._cfg.get('last_name', '')}".strip()
        skills = self._top_skills(5)
        skills_str = ", ".join(skills) if skills else "software engineering and data analysis"
        return (
            f"Dear Hiring Team at {company},\n\n"
            f"I am excited to apply for the {job_title} role. My background in "
            f"{skills_str} aligns closely with what your team is building. "
            f"I have a strong track record of delivering impactful results and am eager "
            f"to contribute to {company}'s mission.\n\n"
            f"Thank you for considering my application.\n\nBest regards,\n{name}"
        )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _top_skills(self, n: int) -> list[str]:
        if self._profile and self._profile.skills:
            return self._profile.skills[:n]
        return self._cfg.get("skills", [])[:n]

    def _match_key(self, label: str) -> Optional[str]:
        # 1. Exact synonym match
        for key, synonyms in FIELD_SYNONYMS.items():
            if label in synonyms or label == key:
                return key

        # 2. Substring containment
        for key, synonyms in FIELD_SYNONYMS.items():
            for syn in synonyms:
                if syn in label or label in syn:
                    return key

        # 3. Fuzzy ratio ≥ 0.75
        best_key, best_ratio = None, 0.0
        for key, synonyms in FIELD_SYNONYMS.items():
            for syn in synonyms:
                ratio = SequenceMatcher(None, label, syn).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_key = key
        if best_ratio >= 0.75:
            return best_key

        return None

    def _get_value(self, key: str, field_type: str) -> Optional[Any]:
        p = self._cfg
        profile = self._profile

        # Derive full name if components exist
        full = p.get("full_name") or (
            f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or None
        )

        # Pull most-recent education entry (index 0) for richer field data
        _edu_entries = p.get("education_entries") or []
        _first_edu = _edu_entries[0] if _edu_entries else {}

        # Pull most-recent experience entry (index 0) for company / title
        _exp_entries = p.get("experience_entries") or []
        _first_exp = _exp_entries[0] if _exp_entries else {}

        mapping: dict[str, Any] = {
            "first_name":         p.get("first_name"),
            "last_name":          p.get("last_name"),
            "full_name":          full,
            "email":              p.get("email"),
            "phone":              p.get("phone"),
            "linkedin":           p.get("linkedin_url"),
            "github":             p.get("github_url"),
            "website":            p.get("portfolio_url") or p.get("website"),
            "location":           p.get("location") or _join(p.get("city"), p.get("state")),
            "city":               p.get("city"),
            "state":              p.get("state"),
            "country":            p.get("country", "United States"),
            "zip_code":           p.get("zip_code"),
            "address":            p.get("address"),
            "resume":             self._resolve_resume_path(p.get("resume_path")),  # resolved path
            "cover_letter":       p.get("cover_letter_text") or "",
            "years_experience":   str(p.get("years_experience", "")),
            "current_company":    _first_exp.get("company") or p.get("current_company"),
            "current_title":      _first_exp.get("title") or p.get("current_title"),
            "start_date":         p.get("available_start_date", "Immediately"),
            "salary_expectation": p.get("salary_expectation", ""),
            "work_authorization": p.get("work_authorization", "Yes"),
            "sponsorship":        p.get("requires_sponsorship", "No"),
            "gender":             p.get("gender", "Prefer not to say"),
            "ethnicity":          p.get("ethnicity", "Prefer not to say"),
            "veteran_status":     p.get("veteran_status", "I am not a protected veteran"),
            "disability":         p.get("disability_status", "No, I don't have a disability"),
            "referral":           p.get("referral_source", "LinkedIn"),
            "university":         _first_edu.get("school") or p.get("university"),
            "degree":             (
                _first_edu.get("degree") or _first_edu.get("major") or p.get("degree")
            ),
            "major":              _first_edu.get("major") or p.get("major"),
            "graduation_year":    str(_first_edu.get("end_year") or p.get("graduation_year", "")),
            "gpa":                str(_first_edu.get("gpa") or p.get("gpa", "")),
            "additional_info":    p.get("additional_info", ""),
        }

        val = mapping.get(key)

        # For select/radio fields return the value; the adapter handles matching to options
        return val

    def _resolve_resume_path(self, filename: "str | None") -> "str | None":
        """Resolve a stored resume filename (possibly bare or absolute) to a real path.

        Storing only the filename in the DB means the path must be reconstructed
        at runtime.  If the stored value is already an absolute path that exists,
        it is returned as-is.  Otherwise the method tries common locations relative
        to this file's package root.
        """
        if not filename:
            return None
        from pathlib import Path as _Path
        p = _Path(filename)
        if p.is_absolute() and p.exists():
            return str(p)
        # Try data/ directory at several parent levels to handle different layouts
        candidates = [
            _Path(__file__).resolve().parent.parent.parent.parent / "data" / p.name,
            _Path(__file__).resolve().parent.parent.parent.parent.parent / "data" / p.name,
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return str(p)  # return as-is; let the caller handle missing file


def _join(*parts: Optional[str], sep: str = ", ") -> Optional[str]:
    filled = [p for p in parts if p]
    return sep.join(filled) if filled else None
