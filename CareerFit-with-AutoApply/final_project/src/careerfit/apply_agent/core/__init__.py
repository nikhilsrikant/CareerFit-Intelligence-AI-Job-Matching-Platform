"""
FieldMapper — maps form field labels to values from the applicant profile.
Works with both st.secrets (Streamlit Cloud) and a local YAML dict.
"""
from __future__ import annotations
from difflib import SequenceMatcher
from typing import Any, Optional

FIELD_SYNONYMS: dict[str, list[str]] = {
    "first_name":          ["first name", "given name", "fname", "first", "prénom"],
    "last_name":           ["last name", "family name", "surname", "lname", "last"],
    "full_name":           ["full name", "name", "your name", "legal name"],
    "email":               ["email", "e-mail", "email address", "work email"],
    "phone":               ["phone", "phone number", "mobile", "cell", "telephone", "contact number"],
    "linkedin":            ["linkedin", "linkedin url", "linkedin profile", "linkedin profile url"],
    "github":              ["github", "github url", "github profile", "portfolio"],
    "website":             ["website", "personal website", "portfolio url", "personal site"],
    "location":            ["location", "city, state", "current location", "where are you located"],
    "city":                ["city", "city name"],
    "state":               ["state", "province", "region"],
    "country":             ["country", "country of residence"],
    "zip_code":            ["zip", "zip code", "postal code"],
    "address":             ["address", "street address", "mailing address"],
    "resume":              ["resume", "cv", "upload resume", "attach resume", "upload cv", "resume/cv"],
    "cover_letter":        ["cover letter", "cover letter text", "why do you want to work here",
                            "why this company", "tell us about yourself", "additional message",
                            "motivation letter", "why are you interested"],
    "years_experience":    ["years of experience", "years experience", "how many years",
                            "years of relevant experience"],
    "current_company":     ["current employer", "current company", "where do you work", "employer"],
    "current_title":       ["current title", "current job title", "current position", "job title"],
    "start_date":          ["start date", "available start", "when can you start",
                            "availability", "available from", "desired start date"],
    "salary_expectation":  ["salary", "desired salary", "expected salary", "compensation",
                            "salary expectation", "expected compensation"],
    "work_authorization":  ["work authorization", "authorized to work", "are you legally authorized",
                            "right to work", "legally authorized", "us work authorization"],
    "sponsorship":         ["sponsorship", "require sponsorship", "visa sponsorship",
                            "do you require sponsorship", "will you now or in the future require sponsorship"],
    "gender":              ["gender", "gender identity"],
    "ethnicity":           ["ethnicity", "race", "race/ethnicity"],
    "veteran_status":      ["veteran", "veteran status", "are you a veteran", "protected veteran"],
    "disability":          ["disability", "disability status", "do you have a disability"],
    "referral":            ["how did you hear", "referral", "source",
                            "how did you find this job", "where did you hear about us"],
    "university":          ["university", "school", "college", "institution"],
    "degree":              ["degree", "degree type", "highest education", "major", "field of study"],
    "graduation_year":     ["graduation year", "grad year", "expected graduation"],
    "gpa":                 ["gpa", "grade point average", "cumulative gpa"],
    "additional_info":     ["additional information", "anything else", "other information",
                            "tell us more", "comments"],
}


class FieldMapper:
    def __init__(self, profile_cfg: dict, runtime_profile=None):
        self._cfg = profile_cfg
        self._profile = runtime_profile
        self._cache: dict[str, Optional[Any]] = {}

    def resolve(self, label: str, field_type: str = "text") -> Optional[Any]:
        key_label = label.lower().strip()
        cache_key = f"{key_label}::{field_type}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        profile_key = self._match_key(key_label)
        value = self._get_value(profile_key, field_type) if profile_key else None
        self._cache[cache_key] = value
        return value

    def cover_letter_for_job(self, job_title: str, company: str) -> str:
        template = self._cfg.get("cover_letter_template", "")
        if template:
            skills = ", ".join(self._top_skills(5))
            return (template
                    .replace("{job_title}", job_title)
                    .replace("{company}", company)
                    .replace("{first_name}", self._cfg.get("first_name", ""))
                    .replace("{skills}", skills))
        name = f"{self._cfg.get('first_name','')} {self._cfg.get('last_name','')}".strip()
        skills = ", ".join(self._top_skills(4)) or "software engineering"
        return (
            f"Dear Hiring Team at {company},\n\n"
            f"I am excited to apply for the {job_title} role. My background in {skills} "
            f"aligns closely with what your team is building, and I am eager to contribute "
            f"to {company}'s mission.\n\nThank you for considering my application.\n\n"
            f"Best regards,\n{name}"
        )

    def _top_skills(self, n: int) -> list[str]:
        if self._profile and getattr(self._profile, "skills", None):
            return self._profile.skills[:n]
        skills = self._cfg.get("skills", [])
        return (skills if isinstance(skills, list) else []) [:n]

    def _match_key(self, label: str) -> Optional[str]:
        for key, synonyms in FIELD_SYNONYMS.items():
            if label in synonyms or label == key:
                return key
        for key, synonyms in FIELD_SYNONYMS.items():
            for syn in synonyms:
                if syn in label or label in syn:
                    return key
        best_key, best_ratio = None, 0.0
        for key, synonyms in FIELD_SYNONYMS.items():
            for syn in synonyms:
                ratio = SequenceMatcher(None, label, syn).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_key = key
        return best_key if best_ratio >= 0.75 else None

    def _get_value(self, key: str, field_type: str) -> Optional[Any]:
        p = self._cfg
        full = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip() or None
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
            "resume":             p.get("resume_path"),
            "cover_letter":       p.get("cover_letter_text", ""),
            "years_experience":   str(p.get("years_experience", "")),
            "current_company":    p.get("current_company"),
            "current_title":      p.get("current_title"),
            "start_date":         p.get("available_start_date", "Immediately"),
            "salary_expectation": p.get("salary_expectation", ""),
            "work_authorization": p.get("work_authorization", "Yes"),
            "sponsorship":        p.get("requires_sponsorship", "No"),
            "gender":             p.get("gender", "Prefer not to say"),
            "ethnicity":          p.get("ethnicity", "Prefer not to say"),
            "veteran_status":     p.get("veteran_status", "I am not a protected veteran"),
            "disability":         p.get("disability_status", "No, I don't have a disability"),
            "referral":           p.get("referral_source", "LinkedIn"),
            "university":         p.get("university"),
            "degree":             p.get("degree"),
            "graduation_year":    str(p.get("graduation_year", "")),
            "gpa":                str(p.get("gpa", "")),
            "additional_info":    p.get("additional_info", ""),
        }
        return mapping.get(key)


def _join(*parts: Optional[str], sep: str = ", ") -> Optional[str]:
    filled = [p for p in parts if p]
    return sep.join(filled) if filled else None
