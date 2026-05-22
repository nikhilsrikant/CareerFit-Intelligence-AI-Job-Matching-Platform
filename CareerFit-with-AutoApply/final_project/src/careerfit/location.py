from __future__ import annotations

import re

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
    "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia",
}

US_STATE_ABBRS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

US_CITY_HINTS = {
    "san jose", "santa clara", "sunnyvale", "mountain view", "palo alto", "san francisco", "los angeles",
    "seattle", "redmond", "bellevue", "austin", "dallas", "houston", "phoenix", "tempe", "chandler",
    "new york", "boston", "cambridge", "chicago", "denver", "atlanta", "raleigh", "durham", "miami",
    "orlando", "portland", "san diego", "irvine", "cupertino", "fremont", "remote us", "remote united states",
}

NON_US_HINTS = {
    "india", "china", "taiwan", "japan", "korea", "singapore", "vietnam", "thailand", "indonesia", "malaysia",
    "canada", "mexico", "brazil", "argentina", "chile", "colombia", "germany", "france", "spain", "italy",
    "netherlands", "sweden", "norway", "finland", "denmark", "poland", "romania", "ireland", "uk", "united kingdom",
    "england", "london", "israel", "australia", "new zealand", "dubai", "uae", "saudi", "remote europe",
    "emea", "apac", "asia pacific",
}

REMOTE_HINTS = {"remote", "hybrid", "telecommute", "work from home"}


def normalize_location(location: str | None) -> str:
    return re.sub(r"\s+", " ", location or "").strip()


def is_location_unknown(location: str | None) -> bool:
    return not normalize_location(location)


def is_us_or_remote_location(location: str | None, include_remote: bool = True, include_unknown: bool = False) -> bool:
    """Best-effort US/remote location filter for public job-board text.

    This intentionally errs on simple, readable rules instead of relying on any one ATS.
    It includes US states/cities and remote roles, and excludes obvious non-US locations.
    """
    loc = normalize_location(location)
    if not loc:
        return include_unknown

    lower = loc.lower()
    # If the posting explicitly says remote, include it unless it is clearly tied to a non-US region.
    if include_remote and any(h in lower for h in REMOTE_HINTS):
        if not any(h in lower for h in NON_US_HINTS):
            return True
        # "Remote - US" should still pass even if a generic region string is present.
        if "united states" in lower or re.search(r"\busa?\b", lower) or "remote us" in lower:
            return True

    if "united states" in lower or re.search(r"\busa?\b", lower):
        return True
    if any(city in lower for city in US_CITY_HINTS):
        return True
    if any(state in lower for state in US_STATE_NAMES):
        return True

    # Match uppercase state abbreviation after comma/space/slash, e.g. "Santa Clara, CA".
    abbrs = set(re.findall(r"(?:^|[\s,;/()\-])([A-Z]{2})(?:$|[\s,;/()\-])", loc))
    if abbrs & US_STATE_ABBRS:
        return True

    if any(h in lower for h in NON_US_HINTS):
        return False

    return include_unknown
