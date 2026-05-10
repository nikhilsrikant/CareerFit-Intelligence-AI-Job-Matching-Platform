from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field

from careerfit.fetchers import NormalizedJob
from careerfit.location import is_us_or_remote_location
from careerfit.source_intelligence import RuntimeProfile, tokens, ROLE_WORDS


NEGATIVE_SIGNALS = {
    "scam", "commission only", "unpaid", "door to door", "pyramid", "multi-level marketing",
}

EARLY_CAREER_SIGNALS = {
    "intern", "internship", "new grad", "new college grad", "graduate", "entry level", "junior", "associate", "trainee", "apprentice", "fellow",
}

SENIOR_SIGNALS = {
    "senior", "staff", "principal", "director", "vp", "head of", "manager", "lead", "architect"
}

# Tokens that should count as equivalent across job titles and resume role phrases.
TOKEN_ALIASES = {
    "engineering": "engineer",
    "engineered": "engineer",
    "engineers": "engineer",
    "developer": "develop",
    "development": "develop",
    "developing": "develop",
    "scientist": "science",
    "scientific": "science",
    "analytics": "analysis",
    "analyst": "analysis",
    "analyzing": "analysis",
    "researcher": "research",
    "researching": "research",
    "designed": "design",
    "designer": "design",
    "systems": "system",
    "applications": "application",
    "models": "model",
    "modeling": "model",
    "modelling": "model",
    "embedded": "embed",
    "firmware": "firmware",
    "hardware": "hardware",
    "software": "software",
    "electrical": "electrical",
    "electronics": "electrical",
    "machine": "machine",
    "learning": "learning",
    "artificial": "ai",
    "intelligence": "ai",
}

GENERIC_QUERY_TERMS = {
    "intern", "internship", "job", "jobs", "role", "roles", "entry", "level", "new", "grad", "graduate", "summer", "fall", "spring", "2024", "2025", "2026", "2027"
}


# -------------------- helpers --------------------
def clamp(value: float, low: float = 0.0, high: float = 0.97) -> float:
    return max(low, min(high, value))


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def percent(value: float) -> int:
    return int(round(value * 100))


@dataclass
class JobMatch:
    company: str
    title: str
    location: str | None
    canonical_url: str
    source: str
    score: float
    decision: str
    role_family: str
    best_document: str | None
    matched_strengths: list[str]
    gaps_or_concerns: list[str]
    reason_summary: str
    should_alert: bool
    raw_job: dict
    profile_ready: bool = False
    feature_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["score_percent"] = percent(self.score)
        return payload


def _canonical_token(token: str) -> str:
    token = token.lower().strip("-/.#")
    return TOKEN_ALIASES.get(token, token)


def tokenize_set(text: str) -> set[str]:
    return {_canonical_token(t) for t in tokens(text) if len(t) > 2}


def _term_tokens(term: str) -> set[str]:
    return {_canonical_token(t) for t in tokens(term) if len(t) > 2}


def _term_matches(term: str, lowered_text: str, token_set: set[str]) -> bool:
    term_l = str(term).lower().strip()
    if len(term_l) < 3:
        return False
    if " " in term_l:
        # Exact phrase first, then token coverage for phrases like Software Engineer vs Software Engineering.
        if term_l in lowered_text:
            return True
        tks = _term_tokens(term_l)
        return bool(tks) and len(tks & token_set) / max(1, len(tks)) >= 0.70
    return _canonical_token(term_l) in token_set or f" {term_l} " in lowered_text


def _dedupe(items: list[str], limit: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        label = str(item).strip()
        key = label.lower()
        if label and key not in seen:
            out.append(label)
            seen.add(key)
        if len(out) >= limit:
            break
    return out


def overlap_score_precomputed(
    lowered_text: str,
    token_set: set[str],
    profile_terms: list[str],
    denominator: float = 10.0,
) -> tuple[float, list[str]]:
    if not profile_terms:
        return 0.0, []
    hits: list[str] = []
    for term in profile_terms:
        if _term_matches(str(term), lowered_text, token_set):
            hits.append(str(term))
    unique_hits = _dedupe(sorted(set(hits), key=lambda x: x.lower()), 24)
    return min(1.0, len(unique_hits) / denominator), unique_hits[:12]


def _profile_phrase_terms(profile: RuntimeProfile) -> list[str]:
    terms: list[str] = []
    for item in profile.suggested_roles or []:
        role = str(item.get("role", "")).strip()
        if role:
            terms.append(role)
            terms.extend([t for t in _term_tokens(role) if t not in ROLE_WORDS])
    for doc in profile.document_tracks or []:
        for role in doc.get("suggested_roles", []) or []:
            label = str(role.get("role", "")).strip()
            if label:
                terms.append(label)
    return _dedupe(terms, 80)


def extract_intent_terms(search_text: str | None) -> list[str]:
    """Convert sidebar search text into a useful fallback intent profile.

    This is only used when the user has not built a resume/website profile yet. It prevents
    the old constant 24% fallback score while still making it clear that personalized matching
    requires a profile.
    """
    text = normalize_text(search_text)
    if not text:
        return []
    terms: list[str] = []
    # Preserve useful multi-word phrases.
    if len(text) >= 4:
        terms.append(text)
    for token in re.findall(r"[a-zA-Z][a-zA-Z0-9+.#/-]{1,}", text):
        token_l = token.lower().strip("-/.#")
        if token_l and token_l not in GENERIC_QUERY_TERMS:
            terms.append(token_l)
    return _dedupe(terms, 30)


# -------------------- scoring --------------------
def role_title_score(title: str, suggested_roles: list[dict], profile_keywords: list[str], title_tokens: set[str]) -> tuple[float, str, list[str]]:
    title_l = normalize_text(title)
    best_score = 0.0
    best_role = "General Fit"
    evidence: list[str] = []

    for item in suggested_roles:
        role = str(item.get("role", ""))
        role_tokens = _term_tokens(role)
        if not role_tokens:
            continue
        hits = sorted(title_tokens & role_tokens)
        role_word_hits = sorted(h for h in hits if h in ROLE_WORDS or h in {"engineer", "science", "analysis", "research", "design", "operations", "electrical", "software", "hardware", "machine", "learning", "ai"})
        coverage = len(hits) / max(1, len(role_tokens))
        if hits:
            # Stronger title calibration: two role tokens like software+engineer should be meaningful.
            score = min(1.0, 0.18 + 0.52 * coverage + 0.07 * len(role_word_hits) + 0.08 * float(item.get("score", 0)))
            if score > best_score:
                best_score = score
                best_role = role
                evidence = hits[:8]

    keyword_hits = []
    for kw in profile_keywords[:70]:
        kw_l = str(kw).lower()
        if len(kw_l) >= 4 and _term_matches(kw_l, title_l, title_tokens):
            keyword_hits.append(str(kw))
    if keyword_hits:
        score = min(1.0, 0.32 + 0.10 * len(set(keyword_hits)))
        if score > best_score:
            best_score = score
            best_role = infer_role_from_title(title)
            evidence = keyword_hits[:8]

    return best_score, best_role, evidence


def infer_role_from_title(title: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z+.#-]*", title)
    if not words:
        return "General Fit"
    compact = []
    for w in words[:9]:
        compact.append(w)
        if _canonical_token(w) in ROLE_WORDS and len(compact) >= 2:
            break
    return " ".join(compact).title() if compact else "General Fit"


def document_track_score(lowered_text: str, token_set: set[str], profile: RuntimeProfile) -> str | None:
    best_name = None
    best_hits = -1
    for doc in profile.document_tracks:
        terms = list(doc.get("skills", [])) + list(doc.get("keywords", [])) + [r.get("role", "") for r in doc.get("suggested_roles", [])]
        _, hits = overlap_score_precomputed(lowered_text, token_set, terms, denominator=8.0)
        if len(hits) > best_hits:
            best_hits = len(hits)
            best_name = doc.get("name")
    return best_name


def negative_penalty(job_text: str) -> tuple[float, list[str]]:
    hits = [sig for sig in NEGATIVE_SIGNALS if sig in job_text]
    return min(0.28, 0.08 * len(hits)), sorted(hits)[:6]


def opportunity_adjustment(job_text: str) -> tuple[float, list[str]]:
    early_hits = [sig for sig in EARLY_CAREER_SIGNALS if sig in job_text]
    senior_hits = [sig for sig in SENIOR_SIGNALS if sig in job_text]
    boost = min(0.08, 0.025 * len(early_hits))
    penalty = min(0.14, 0.04 * len(senior_hits))
    return boost - penalty, sorted(set(early_hits + senior_hits))[:6]


# Years-of-experience patterns
# Catches: "5+ years", "5 years of experience", "minimum 3 years", "at least 4 years", "5-7 years"
_YEARS_PATTERNS = [
    re.compile(r"(\d{1,2})\s*\+?\s*(?:to|-|–)\s*\d{1,2}\s*(?:years?|yrs?)", re.IGNORECASE),  # "3-5 years"
    re.compile(r"(?:minimum|at least|min\.?)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience|exp|professional)", re.IGNORECASE),
    re.compile(r"(\d{1,2})\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience|exp|professional|relevant|industry)", re.IGNORECASE),
]

def extract_years_required(job_text: str) -> int | None:
    """Return the MINIMUM years of experience required, or None if not found."""
    if not job_text:
        return None
    found = []
    for pattern in _YEARS_PATTERNS:
        for match in pattern.finditer(job_text):
            try:
                n = int(match.group(1))
                if 0 <= n <= 30:
                    found.append(n)
            except (ValueError, IndexError):
                continue
    if not found:
        return None
    return min(found)


def years_experience_penalty(job_text: str, max_years: int) -> tuple[float, int | None]:
    """Penalize jobs requiring more than max_years of experience.
    Returns (penalty, detected_years). The penalty grows with the gap."""
    detected = extract_years_required(job_text)
    if detected is None or detected <= max_years:
        return 0.0, detected
    gap = detected - max_years
    # 0.08 penalty per year over the limit, capped at 0.30
    penalty = min(0.30, 0.08 * gap)
    return penalty, detected


def profile_density_bonus(profile: RuntimeProfile) -> float:
    if not profile.combined_text:
        return 0.0
    evidence_count = len(profile.skills) + len(profile.keywords) + len(profile.suggested_roles) * 2
    return min(0.05, evidence_count / 2200.0)


def _job_text(job: NormalizedJob) -> str:
    return "\n".join([
        job.title or "",
        job.location or "",
        job.department or "",
        job.employment_type or "",
        job.description_text or "",
    ])


def _has_real_description(job: NormalizedJob) -> bool:
    desc = normalize_text(job.description_text)
    title = normalize_text(job.title)
    return len(desc) > max(90, len(title) + 45)


def _fallback_no_profile_score(
    title: str,
    job_text_l: str,
    token_set: set[str],
    title_tokens: set[str],
    intent_terms: list[str],
) -> tuple[float, str, list[str], dict[str, float]]:
    intent_score, intent_hits = overlap_score_precomputed(job_text_l, token_set, intent_terms, denominator=max(2.0, min(8.0, len(intent_terms) or 2.0)))
    roleish_tokens = title_tokens & ({_canonical_token(x) for x in ROLE_WORDS} | {"engineer", "science", "analysis", "design", "research", "hardware", "software", "electrical", "mechanical", "finance", "marketing", "clinical", "operations", "data", "machine", "learning", "ai"})
    title_specificity = min(1.0, len(roleish_tokens) / 4.0)
    technicality = min(1.0, len([t for t in title_tokens if t not in GENERIC_QUERY_TERMS]) / 7.0)
    score = 0.08 + 0.40 * intent_score + 0.18 * title_specificity + 0.10 * technicality
    # Keep unpersonalized matches visibly capped below high-fit. This avoids implying a true profile fit.
    score = min(score, 0.62)
    evidence = intent_hits or sorted(roleish_tokens)[:5] or ["Title appears potentially relevant"]
    reason = "No profile has been built yet, so this is only a search-intent score. Build a profile to get personalized relevance."
    features = {
        "intent": round(intent_score, 4),
        "title_specificity": round(title_specificity, 4),
        "title_detail": round(technicality, 4),
        "personalized": 0.0,
    }
    return score, reason, evidence, features


def score_job(
    job: NormalizedJob,
    profile: RuntimeProfile,
    threshold: float = 0.85,
    intent_terms: list[str] | None = None,
    max_years_experience: int = 10,
) -> JobMatch:
    title = job.title or "Untitled role"
    text = _job_text(job)
    job_text_l = normalize_text(text)
    token_set = tokenize_set(job_text_l)
    title_tokens = tokenize_set(title)

    profile_ready = bool((profile.combined_text or "").strip())
    profile_skills = profile.skills or []
    profile_keywords = profile.keywords or []
    suggested_roles = profile.suggested_roles or []

    penalty, negative_hits = negative_penalty(job_text_l)
    adjustment, seniority_hits = opportunity_adjustment(job_text_l)
    years_penalty, detected_years = years_experience_penalty(job_text_l, max_years_experience)
    penalty += years_penalty

    if not profile_ready:
        base, reason, evidence, feature_scores = _fallback_no_profile_score(
            title, job_text_l, token_set, title_tokens, intent_terms or []
        )
        role_family = infer_role_from_title(title)
        matched_strengths = [f"Search/title evidence: {item}" for item in evidence]
    else:
        title_score, role_family, title_hits = role_title_score(title, suggested_roles, profile_keywords, title_tokens)
        skill_score, skill_hits = overlap_score_precomputed(job_text_l, token_set, profile_skills, denominator=8.0)
        keyword_score, keyword_hits = overlap_score_precomputed(job_text_l, token_set, profile_keywords[:100], denominator=12.0)
        role_phrase_score, role_phrase_hits = overlap_score_precomputed(job_text_l, token_set, _profile_phrase_terms(profile), denominator=5.0)

        profile_term_set = _dedupe([str(x) for x in profile_skills + profile_keywords[:100] + _profile_phrase_terms(profile)], 220)
        semantic_hits = []
        for term in profile_term_set:
            if len(term) >= 3 and _term_matches(term, job_text_l, token_set):
                semantic_hits.append(term)
        semantic_hits = _dedupe(semantic_hits, 18)
        semantic_score = min(1.0, len(semantic_hits) / 16.0)

        # When fast mode provides only listing data, title/role evidence must carry more weight.
        # This fixes the old behavior where all listing-only jobs clustered at the same low score.
        if _has_real_description(job):
            synergy = 0.08 if title_score >= 0.72 and (skill_score + keyword_score + role_phrase_score) >= 0.45 else 0.0
            base = (
                0.06
                + 0.26 * title_score
                + 0.25 * skill_score
                + 0.21 * keyword_score
                + 0.15 * semantic_score
                + 0.12 * role_phrase_score
                + synergy
            )
        else:
            # Listing-only Workday/Ashby/Lever boards may not include the full JD in fast mode.
            # A strong title + role-target overlap is therefore enough to score high, while
            # unrelated titles remain low. Tuned to spread scores across 0.6-0.95 instead of
            # clamping to 0.99 — gives meaningful differentiation between top matches.
            synergy = 0.10 if title_score >= 0.80 and role_phrase_score >= 0.40 else 0.0
            base = (
                0.04
                + 0.40 * title_score
                + 0.10 * skill_score
                + 0.12 * keyword_score
                + 0.08 * semantic_score
                + 0.16 * role_phrase_score
                + synergy
            )
        base += profile_density_bonus(profile)
        reason = make_reason(role_family, skill_hits, keyword_hits, title_hits, semantic_hits, seniority_hits, role_phrase_hits)
        matched_strengths = build_strengths(skill_hits, keyword_hits, title_hits, semantic_hits, role_phrase_hits)
        feature_scores = {
            "title": round(title_score, 4),
            "skills": round(skill_score, 4),
            "keywords": round(keyword_score, 4),
            "semantic": round(semantic_score, 4),
            "role_phrases": round(role_phrase_score, 4),
            "density_bonus": round(profile_density_bonus(profile), 4),
            "personalized": 1.0,
        }

    score = clamp(base + adjustment - penalty)
    if not profile_ready:
        decision = "profile_needed"
        should_alert = False
    elif score >= threshold:
        decision = "high_fit"
        should_alert = True
    elif score >= max(0.65, threshold - 0.15):
        decision = "possible_fit"
        should_alert = False
    elif score >= 0.45:
        decision = "review"
        should_alert = False
    else:
        decision = "low_fit"
        should_alert = False

    gaps = build_gaps(job_text_l, profile_skills, negative_hits, seniority_hits, profile_ready)
    return JobMatch(
        company=job.company,
        title=title,
        location=job.location,
        canonical_url=job.canonical_url,
        source=job.source,
        score=round(score, 4),
        decision=decision,
        role_family=role_family,
        best_document=document_track_score(job_text_l, token_set, profile) if profile_ready else None,
        matched_strengths=matched_strengths[:8],
        gaps_or_concerns=gaps[:6],
        reason_summary=reason,
        should_alert=should_alert,
        raw_job=job.to_dict(),
        profile_ready=profile_ready,
        feature_scores=feature_scores,
    )


def make_reason(
    role_family: str,
    skill_hits: list[str],
    keyword_hits: list[str],
    title_hits: list[str],
    semantic_hits: list[str],
    seniority_hits: list[str],
    role_phrase_hits: list[str],
) -> str:
    parts: list[str] = []
    if role_family and role_family != "General Fit":
        parts.append(f"Closest role alignment: {role_family}.")
    if title_hits:
        parts.append("Title signals: " + ", ".join(title_hits[:4]) + ".")
    if role_phrase_hits:
        parts.append("Role-target overlap: " + ", ".join(role_phrase_hits[:4]) + ".")
    if skill_hits:
        parts.append("Skill overlap: " + ", ".join(skill_hits[:5]) + ".")
    if keyword_hits:
        parts.append("Profile keyword overlap: " + ", ".join(keyword_hits[:5]) + ".")
    if semantic_hits:
        parts.append("Job-description evidence: " + ", ".join(sorted(set(semantic_hits))[:5]) + ".")
    if seniority_hits:
        parts.append("Seniority/opportunity signals detected: " + ", ".join(seniority_hits[:4]) + ".")
    if not parts:
        return "Limited profile overlap detected. Review manually if the company or team is strategically important."
    return " ".join(parts)


def build_strengths(skill_hits: list[str], keyword_hits: list[str], title_hits: list[str], semantic_hits: list[str], role_phrase_hits: list[str]) -> list[str]:
    strengths = []
    for item in title_hits[:4]:
        strengths.append(f"Title signal: {item}")
    for item in role_phrase_hits[:4]:
        strengths.append(f"Role-target match: {item}")
    for item in skill_hits[:6]:
        strengths.append(f"Skill match: {item}")
    for item in keyword_hits[:5]:
        strengths.append(f"Keyword match: {item}")
    for item in semantic_hits[:5]:
        strengths.append(f"JD evidence: {item}")
    return _dedupe(strengths, 12) or ["Potential fit based on title and description"]


def build_gaps(job_text: str, profile_skills: list[str], negative_hits: list[str], seniority_hits: list[str], profile_ready: bool) -> list[str]:
    gaps = []
    if not profile_ready:
        gaps.append("Profile not built yet; score is not personalized")
    for hit in negative_hits:
        gaps.append(f"Potential mismatch signal: {hit}")
    if any(hit in SENIOR_SIGNALS for hit in seniority_hits):
        gaps.append("Seniority may be higher than the uploaded profile; review requirements manually")
    req_match = re.findall(r"(?i)(?:required|required skills|preferred|qualifications?)[:\- ]+([^\.]{10,160})", job_text)
    if req_match and not profile_skills:
        gaps.append("Job has explicit requirements; build/upload a profile for stronger matching")
    if not gaps:
        gaps.append("No major gap detected by the rules engine")
    return gaps


def rank_jobs(
    jobs: list[NormalizedJob],
    profile: RuntimeProfile,
    threshold: float = 0.85,
    us_only: bool = False,
    include_unknown_locations: bool = False,
    intent_terms: list[str] | None = None,
    max_years_experience: int = 10,
) -> list[JobMatch]:
    seen: set[str] = set()
    matches: list[JobMatch] = []
    for job in jobs:
        if us_only and not is_us_or_remote_location(job.location, include_remote=True, include_unknown=include_unknown_locations):
            continue
        key = job.canonical_url or f"{job.company}-{job.title}-{job.location}"
        if key in seen:
            continue
        seen.add(key)
        matches.append(score_job(job, profile, threshold, intent_terms=intent_terms, max_years_experience=max_years_experience))
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches
