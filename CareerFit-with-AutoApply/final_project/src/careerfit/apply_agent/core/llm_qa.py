"""
LLMQAEngine — uses OpenAI to answer unique ATS form questions using resume + JD context.

Gracefully degrades when:
  - openai_api_key is empty or None
  - openai package is not installed
  - API call fails for any reason
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Try to import openai — gracefully degrade if not installed
try:
    from openai import OpenAI as _OpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    _OpenAI = None  # type: ignore[assignment,misc]
    _OPENAI_AVAILABLE = False


_SYSTEM_PROMPT = (
    "You are helping fill a job application form. Using the applicant profile and job "
    "description below, answer the following question concisely. If options are provided, "
    "choose the best matching option exactly as written.\n\n"
    "Profile:\n{profile_summary}\n\n"
    "Job Description:\n{job_description}\n\n"
    "Question: {question_label}\n\n"
    "Options (choose one if provided): {options}\n\n"
    "Answer:"
)


class LLMQAEngine:
    """
    Answers arbitrary ATS form questions using an OpenAI LLM.

    Args:
        openai_api_key: User-supplied OpenAI API key. Pass empty string to disable.
        profile_summary: Structured summary of the applicant's profile / resume.
        job_description: Text of the job description for the position being applied to.
    """

    def __init__(
        self,
        openai_api_key: str,
        profile_summary: str,
        job_description: str,
    ) -> None:
        self._api_key = openai_api_key or ""
        self._profile_summary = profile_summary or ""
        self._job_description = job_description or ""
        self._client: Optional[object] = None

    # ── Public API ───────────────────────────────────────────────────────────

    def answer_question(
        self,
        question_label: str,
        options: "list[str] | None" = None,
    ) -> str:
        """
        Answer a form question using LLM context.

        Returns an empty string when:
          - openai_api_key is empty/None (graceful degradation)
          - openai package is not installed
          - the API call fails for any reason
        """
        if not self._api_key:
            return ""
        if not _OPENAI_AVAILABLE:
            return ""

        try:
            client = self._get_client()
            prompt = _SYSTEM_PROMPT.format(
                profile_summary=self._profile_summary,
                job_description=self._job_description,
                question_label=question_label,
                options=str(options) if options else "None",
            )
            response = client.chat.completions.create(  # type: ignore[union-attr]
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=256,
                temperature=0.2,
            )
            result = response.choices[0].message.content.strip()
            logger.debug("LLM answered %r -> %r", question_label, result)
            return result
        except Exception as e:
            logger.warning("LLM call failed for question %r: %s", question_label, e)
            return ""

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _get_client(self) -> object:
        """Lazily create the OpenAI client (one per engine instance)."""
        if self._client is None:
            self._client = _OpenAI(api_key=self._api_key)  # type: ignore[operator]
        return self._client
