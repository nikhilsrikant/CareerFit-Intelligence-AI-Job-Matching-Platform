"""
Adapter registry — maps ATS name strings to adapter classes.
The source field from NormalizedJob.source already matches these keys,
so no ATS re-detection is needed for jobs fetched by CareerFit.
"""
from __future__ import annotations

from careerfit.apply_agent.adapters.greenhouse import GreenhouseAdapter
from careerfit.apply_agent.adapters.workday import WorkdayAdapter
from careerfit.apply_agent.adapters.lever import LeverAdapter
from careerfit.apply_agent.adapters.ashby_smartrecruiters import AshbyAdapter, SmartRecruitersAdapter
from careerfit.apply_agent.adapters.generic import GenericAdapter, AmazonAdapter, MicrosoftAdapter
from careerfit.apply_agent.adapters.base import BaseAdapter

_REGISTRY: dict[str, type[BaseAdapter]] = {
    "greenhouse":       GreenhouseAdapter,
    "workday":          WorkdayAdapter,
    "lever":            LeverAdapter,
    "ashby":            AshbyAdapter,
    "smartrecruiters":  SmartRecruitersAdapter,
    "amazon":           AmazonAdapter,
    "microsoft":        MicrosoftAdapter,
    "generic":          GenericAdapter,
    "auto":             GenericAdapter,   # CareerFit's fallback ATS type
}


def get_adapter(ats_name: str) -> BaseAdapter:
    """Return an instantiated adapter for the given ATS name."""
    cls = _REGISTRY.get((ats_name or "").lower(), GenericAdapter)
    return cls()


__all__ = ["get_adapter", "_REGISTRY"]
