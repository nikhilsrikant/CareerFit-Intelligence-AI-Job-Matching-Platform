"""
dropdown_matcher.py
───────────────────
Smart dropdown fallback engine for ATS form select fields.

When the user's degree/major does not exactly match an available option,
this module resolves the best available alternative using:
  1. Fuzzy string matching (SequenceMatcher + Jaccard token overlap + prefix bonus)
  2. Domain-specific fallback chains (DEGREE_FALLBACK_CHAINS) covering common
     education fields and degree types

No external dependencies — only stdlib (difflib, re, string).
"""
from __future__ import annotations

import re
import string
from difflib import SequenceMatcher
from typing import Optional


# ── Fallback chains ──────────────────────────────────────────────────────────
# Each key is a "canonical" degree/major name; the list is an ordered sequence
# of progressively broader fallbacks to try when the canonical form is not
# available in a dropdown.

DEGREE_FALLBACK_CHAINS: dict[str, list[str]] = {
    # User-reported cases (primary motivation for this module)
    "Data Science, Analytics and Engineering": [
        "Data Science",
        "Data Analytics",
        "Data Processing",
        "Computer Science",
        "Information Systems",
        "Engineering",
    ],
    "Electronics and Communication Engineering": [
        "Electronics Engineering",
        "Electronics",
        "Electrical and Electronics",
        "Electrical Engineering",
        "Electrical",
        "Computer Engineering",
        "Engineering",
    ],
    # Degree type chains
    "Master of Science": [
        "M.S.",
        "MS",
        "Masters",
        "Master",
        "Graduate",
        "Post-Graduate",
        "Postgraduate",
    ],
    "Bachelor of Engineering": [
        "B.E.",
        "BE",
        "B.Tech",
        "Bachelor of Technology",
        "Bachelors",
        "Undergraduate",
    ],
    "Bachelor of Science": [
        "B.S.",
        "BS",
        "Bachelors of Science",
        "Bachelors",
        "Undergraduate",
    ],
    "Bachelor of Technology": [
        "B.Tech",
        "BTech",
        "Bachelor of Engineering",
        "B.E.",
        "Bachelors",
        "Undergraduate",
    ],
    "Master of Business Administration": [
        "MBA",
        "M.B.A.",
        "Masters of Business",
        "Graduate Business",
    ],
    # Field of study chains
    "Computer Science": [
        "Computer Science and Engineering",
        "CS",
        "Software Engineering",
        "Information Technology",
        "Information Systems",
        "Computing",
    ],
    "Information Technology": [
        "Computer Science",
        "Information Systems",
        "Software Engineering",
        "Computing",
    ],
    "Electrical Engineering": [
        "Electrical and Electronics",
        "Electronics Engineering",
        "EE",
        "Electrical",
        "Electronics",
        "Engineering",
    ],
    "Mechanical Engineering": [
        "ME",
        "Mechanical",
        "Engineering",
        "Manufacturing Engineering",
    ],
    "Civil Engineering": [
        "CE",
        "Civil",
        "Engineering",
        "Structural Engineering",
    ],
    "Chemical Engineering": [
        "ChemE",
        "Chemical",
        "Engineering",
        "Chemistry",
    ],
    "Biomedical Engineering": [
        "Biomedical",
        "Bioengineering",
        "Engineering",
        "Biology",
    ],
    "Mathematics": [
        "Math",
        "Applied Mathematics",
        "Statistics",
        "Pure Mathematics",
        "Mathematical Sciences",
    ],
    "Statistics": [
        "Applied Statistics",
        "Math",
        "Mathematics",
        "Data Science",
        "Quantitative Methods",
    ],
    "Physics": [
        "Applied Physics",
        "Physical Sciences",
        "Engineering Physics",
        "Science",
    ],
    "Biology": [
        "Biological Sciences",
        "Life Sciences",
        "Biochemistry",
        "Biotechnology",
        "Science",
    ],
    "Chemistry": [
        "Biochemistry",
        "Chemical Sciences",
        "Science",
        "Chemical Engineering",
    ],
    "Finance": [
        "Financial Engineering",
        "Economics",
        "Accounting",
        "Business",
        "Financial Mathematics",
    ],
    "Economics": [
        "Finance",
        "Business Economics",
        "Applied Economics",
        "Business",
        "Quantitative Economics",
    ],
    "Marketing": [
        "Business",
        "Communications",
        "Marketing and Communications",
        "Digital Marketing",
    ],
    "Business Administration": [
        "Business",
        "Management",
        "Commerce",
        "Business Management",
        "MBA",
    ],
    "Psychology": [
        "Behavioral Sciences",
        "Social Sciences",
        "Cognitive Science",
        "Neuroscience",
    ],
    "Data Science": [
        "Data Analytics",
        "Machine Learning",
        "Artificial Intelligence",
        "Computer Science",
        "Statistics",
        "Data Engineering",
    ],
}


# ── Normalisation ─────────────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = s.lower()
    s = s.translate(str.maketrans("", "", string.punctuation))
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ── Fuzzy matcher ─────────────────────────────────────────────────────────────

def fuzzy_best_match(
    target: str,
    options: list[str],
    threshold: float = 0.55,
) -> Optional[str]:
    """
    Return the option from *options* that best matches *target*, or None if no
    option scores above *threshold*.

    Scoring combines:
      - SequenceMatcher ratio  (weight 0.45)
      - Jaccard token overlap  (weight 0.40)
      - Prefix bonus           (+0.15 if one string starts with the other)
    """
    if not options:
        return None

    norm_target = _normalize(target)

    # Fast path: exact normalised match
    for option in options:
        if _normalize(option) == norm_target:
            return option

    best_option: Optional[str] = None
    best_score: float = 0.0

    target_tokens = set(norm_target.split())

    for option in options:
        norm_opt = _normalize(option)
        opt_tokens = set(norm_opt.split())

        seq_score = SequenceMatcher(None, norm_target, norm_opt).ratio()

        union = target_tokens | opt_tokens
        jaccard = (
            len(target_tokens & opt_tokens) / len(union)
            if union
            else 0.0
        )

        prefix_bonus = (
            0.15
            if norm_target.startswith(norm_opt) or norm_opt.startswith(norm_target)
            else 0.0
        )

        combined = 0.45 * seq_score + 0.40 * jaccard + prefix_bonus

        if combined > best_score:
            best_score = combined
            best_option = option

    return best_option if best_score > threshold else None


# ── Public resolver ───────────────────────────────────────────────────────────

def resolve_dropdown_value(
    desired: str,
    available_options: list[str],
) -> Optional[str]:
    """
    Resolve *desired* to the best available option in *available_options*.

    Algorithm:
      1. Trivial guard: return None if either argument is empty.
      2. Try fuzzy_best_match directly against all available options.
      3. Look up a fallback chain for *desired* in DEGREE_FALLBACK_CHAINS.
         If no exact key matches, attempt partial key matching (substring both ways).
      4. Walk the chain in order; return the first fallback that fuzzy-matches.
      5. Return None if all attempts fail.
    """
    if not desired or not available_options:
        return None

    # Step 1: direct fuzzy match
    result = fuzzy_best_match(desired, available_options)
    if result:
        return result

    # Step 2: find fallback chain
    chain: Optional[list[str]] = DEGREE_FALLBACK_CHAINS.get(desired)

    if chain is None:
        # Partial key matching (normalised substring).
        # Sort candidates by descending key length to prefer the most-specific
        # match regardless of insertion order in DEGREE_FALLBACK_CHAINS.
        norm_desired = _normalize(desired)
        candidates = [
            (key, key_chain)
            for key, key_chain in DEGREE_FALLBACK_CHAINS.items()
            if _normalize(key) in norm_desired or norm_desired in _normalize(key)
        ]
        candidates.sort(key=lambda kc: len(kc[0]), reverse=True)
        if candidates:
            chain = candidates[0][1]

    if chain is None:
        return None

    # Step 3: walk chain
    for fallback in chain:
        result = fuzzy_best_match(fallback, available_options)
        if result:
            return result

    return None
