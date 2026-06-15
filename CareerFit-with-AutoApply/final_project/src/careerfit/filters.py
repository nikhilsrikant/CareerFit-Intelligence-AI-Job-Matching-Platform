"""
filters.py
F-1 visa eligibility filter and role-type filter (Fall Intern / Co-op / New Grad).
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from careerfit.fetchers import NormalizedJob

# ── F-1 eligibility keyword lists ────────────────────────────────────────────

F1_BLOCK_KEYWORDS: list[str] = [
    "security clearance",
    "secret clearance",
    "top secret",
    "ts/sci",
    "public trust clearance",
    "must be a us citizen",
    "must be us citizen",
    "u.s. citizen only",
    "us citizen only",
    "us citizens only",
    "united states citizen only",
    "permanent resident only",
    "must be a permanent resident",
    "no visa sponsorship",
    "no sponsorship",
    "sponsorship not available",
    "will not sponsor",
    "cannot sponsor",
    "authorized without sponsorship",
    "without need for sponsorship",
    "without sponsorship now or in the future",
    "must be authorized to work in the us without",
    "requires us work authorization",
    "requires u.s. work authorization",
    "defense contractor",
    "dod contractor",
    "department of defense",
    "itar",
    "export control requirements",
    "government security clearance",
    "active clearance",
    "require a security clearance",
    "requires a clearance",
]

F1_FRIENDLY_KEYWORDS: list[str] = [
    "f-1",
    " opt ",
    " cpt ",
    "visa sponsorship available",
    "will sponsor",
    "sponsorship available",
    "open to sponsorship",
    "we sponsor",
    "supports visa",
    "offers visa sponsorship",
]

# ── Role type keyword lists ───────────────────────────────────────────────────

ROLE_TYPE_KEYWORDS: dict[str, list[str]] = {
    "fall_intern": [
        "fall intern",
        "fall internship",
        "fall 2025 intern",
        "fall 2026 intern",
        "fall 2027 intern",
        "intern fall",
        "internship fall",
        "fall semester intern",
        "fall term intern",
    ],
    "fall_coop": [
        "fall co-op",
        "fall coop",
        "co-op fall",
        "coop fall",
        "fall co op",
        "fall cooperative",
        "fall co-op student",
        "fall semester co-op",
    ],
    "new_grad": [
        "new grad",
        "new college grad",
        "new graduate",
        "recent grad",
        "recent graduate",
        "class of 2025",
        "class of 2026",
        "class of 2027",
    ],
    "entry_level": [
        "entry level",
        "entry-level",
        "junior ",
        "associate engineer",
        "associate software",
        "associate data",
        "0-1 year",
        "0-2 year",
        "0 to 2 year",
        "no experience required",
        "less than 1 year",
        "0+ years",
    ],
}

# Titles with these terms are explicitly blocked (senior/summer/spring/govt)
ROLE_TITLE_BLOCKLIST: list[str] = [
    "senior ",
    " senior",
    "staff engineer",
    "staff software",
    "principal ",
    "director ",
    "vice president",
    " vp ",
    "head of ",
    " manager",
    "manager ",
    "lead engineer",
    "lead software",
    "architect ",
    "summer intern",
    "summer internship",
    "summer 2025",
    "summer 2026",
    "spring intern",
    "spring internship",
    "spring 2025",
    "spring 2026",
]


# ── Filter functions ──────────────────────────────────────────────────────────

def check_f1_eligibility(job_text: str) -> dict:
    """
    Check whether a job description is compatible with F-1 (OPT/CPT) visa status.

    Returns:
        {
            "f1_eligible": bool,   # True = job does NOT have F-1 blocking language
            "f1_blocked":  bool,   # True = job explicitly requires clearance or no-sponsor
            "f1_friendly": bool,   # True = job explicitly welcomes F-1/OPT/CPT
            "blocking_keywords": list[str],
        }
    """
    lowered = (job_text or "").lower()
    blocking = [kw for kw in F1_BLOCK_KEYWORDS if kw in lowered]
    friendly = any(kw in lowered for kw in F1_FRIENDLY_KEYWORDS)
    blocked = bool(blocking)
    return {
        "f1_eligible":        not blocked,
        "f1_blocked":         blocked,
        "f1_friendly":        friendly,
        "blocking_keywords":  blocking,
    }


def check_role_type(title: str, job_text: str) -> dict:
    """
    Determine whether a role is a target type (Fall Intern, Fall Co-op, New Grad,
    or Entry Level) and whether the title hits the blocklist (senior/summer/spring).

    Returns:
        {
            "is_target_role": bool,
            "matched_types":  list[str],
            "is_blocked_title": bool,
        }
    """
    title_l = (title or "").lower()
    combined = (title_l + " " + (job_text or "").lower())

    blocked_title = any(kw in title_l for kw in ROLE_TITLE_BLOCKLIST)

    matched_types: list[str] = []
    for category, keywords in ROLE_TYPE_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            matched_types.append(category)

    # "intern" or "internship" in title also counts as entry-level without summer/spring
    if not blocked_title and re.search(r"\bintern(ship)?\b", title_l):
        if "entry_level" not in matched_types:
            matched_types.append("entry_level")
        if "fall_intern" not in matched_types:
            matched_types.append("fall_intern")

    is_target = bool(matched_types) and not blocked_title
    return {
        "is_target_role":   is_target,
        "matched_types":    matched_types,
        "is_blocked_title": blocked_title,
    }


def apply_filters(
    job: "NormalizedJob",
    f1_filter_enabled: bool,
    role_type_filter_enabled: bool,
) -> "tuple[bool, dict]":
    """
    Apply F-1 and role-type filters to a NormalizedJob.

    Returns:
        (passes: bool, metadata: dict)
    The metadata dict contains f1_friendly, f1_blocked, is_target_role, role_types.
    """
    job_text = (job.description_text or "") + " " + (job.title or "")

    f1_result   = check_f1_eligibility(job_text)
    role_result = check_role_type(job.title or "", job_text)

    pass_f1   = (not f1_filter_enabled)   or f1_result["f1_eligible"]
    pass_role = (not role_type_filter_enabled) or role_result["is_target_role"]

    metadata = {
        "f1_friendly":    f1_result["f1_friendly"],
        "f1_blocked":     f1_result["f1_blocked"],
        "is_target_role": role_result["is_target_role"],
        "role_types":     role_result["matched_types"],
    }
    return (pass_f1 and pass_role, metadata)
