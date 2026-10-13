"""Profile extraction (FR-3), missing fields (FR-4) and the combined transcript (FR-5)."""

import json
import logging
import unicodedata
from functools import cache
from pathlib import Path

from pydantic import ValidationError

from app.ai.llm import LLMClient, LLMError, Message, logged_call
from app.ai.redact import ascii_digits, redact
from app.schemas.profile import (
    LLM_JSON_SCHEMA,
    REQUIRED_FIELDS,
    LLMExtraction,
    RequiredField,
    WorkerProfileData,
)

logger = logging.getLogger(__name__)

PROMPT_ID = "extract_profile.v1"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "ai" / "prompts" / "extract_profile.md"
MAX_TRANSCRIPT_CHARS = 6000
MIN_EVIDENCE_CHARS = 2


@cache
def system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def combined_transcript(transcripts: list[str]) -> str:
    """All of a worker's inputs in order, newest last, capped for the prompt."""
    joined = "\n".join(
        f"Message {i}: {t.strip()}" for i, t in enumerate(transcripts, 1) if t.strip()
    )
    return joined[-MAX_TRANSCRIPT_CHARS:]


def _normalize(text: str) -> str:
    return " ".join(ascii_digits(unicodedata.normalize("NFC", text)).casefold().split())


def _grounded(extraction: LLMExtraction, transcript: str) -> WorkerProfileData:
    """Drop skills whose evidence is not a verbatim quote (guardrail: never invent skills)."""
    haystack = _normalize(transcript)
    kept = []
    for skill in extraction.skills:
        quote = _normalize(skill.evidence)
        if len(quote) >= MIN_EVIDENCE_CHARS and quote in haystack:
            kept.append({"name": skill.name.strip().lower(), "level": skill.level})
        else:
            logger.info("dropped ungrounded skill", extra={"skill": skill.name})
    data = extraction.model_dump()
    data["skills"] = kept
    return WorkerProfileData.model_validate(data)


def build_messages(
    transcript: str, previous: WorkerProfileData | None, focus_field: str | None
) -> list[Message]:
    payload = {
        "transcript": transcript,
        "previous_profile": previous.model_dump(mode="json") if previous else None,
        "latest_message_answers": focus_field,
    }
    return [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def extract_profile(
    llm: LLMClient,
    transcripts: list[str],
    previous: WorkerProfileData | None = None,
    focus_field: str | None = None,
) -> WorkerProfileData | None:
    """Run extraction. On invalid output retry once with the error; None if still invalid.

    None means the caller falls back to asking the worker a follow-up (FR-3).
    """
    transcript = redact(combined_transcript(transcripts))
    if not transcript:
        return None
    messages = build_messages(transcript, previous, focus_field)
    for attempt in (1, 2):
        try:
            resp = logged_call(llm, PROMPT_ID, messages, LLM_JSON_SCHEMA, "worker_profile")
        except LLMError:
            logger.warning("profile extraction call failed", exc_info=True)
            return None
        try:
            extraction = LLMExtraction.model_validate_json(resp.text)
        except ValidationError as exc:
            logger.info("invalid extraction output", extra={"attempt": attempt})
            if attempt == 2:
                return None
            error = exc.json(include_input=False, include_url=False)
            messages = [
                *messages,
                {"role": "assistant", "content": resp.text},
                {
                    "role": "user",
                    "content": "That JSON was invalid: "
                    + error
                    + "\nReturn only the corrected JSON.",
                },
            ]
            continue
        return _grounded(extraction, transcript)
    return None  # pragma: no cover


def missing_required(profile: WorkerProfileData | None) -> list[RequiredField]:
    """FR-4 fields still missing, in the order they are asked about."""
    if profile is None:
        return list(REQUIRED_FIELDS)
    missing: list[RequiredField] = []
    if profile.primary_trade == "other" and "primary_trade" in profile.missing_fields:
        missing.append("primary_trade")
    if profile.years_experience is None:
        missing.append("years_experience")
    if profile.location.is_empty:
        missing.append("location")
    return missing
