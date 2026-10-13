"""Worker profile: the PRD's extraction output schema, and the LLM's wire format.

The LLM additionally returns `evidence` (a verbatim quote) for each skill so the
"never invent skills" guardrail can be checked in code. Evidence is stripped
before the profile is stored.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Trade = Literal[
    "electrician", "plumber", "tailor", "driver", "beautician", "carpenter", "delivery", "other"
]  # fmt: skip
TRADES: tuple[str, ...] = Trade.__args__  # type: ignore[attr-defined]
RequiredField = Literal["primary_trade", "years_experience", "location"]
REQUIRED_FIELDS: tuple[RequiredField, ...] = ("primary_trade", "years_experience", "location")


class Skill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    level: Literal["basic", "skilled", "expert"]


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: str | None = Field(default=None, max_length=80)
    area: str | None = Field(default=None, max_length=120)
    pincode: str | None = None

    @field_validator("pincode", mode="before")
    @classmethod
    def _pincode(cls, v: Any) -> str | None:
        """Keep only a valid 6-digit Indian pin code; anything else becomes null."""
        if v is None:
            return None
        digits = re.sub(r"\D", "", str(v))
        return digits if re.fullmatch(r"[1-9]\d{5}", digits) else None

    @property
    def is_empty(self) -> bool:
        return not (self.city or self.area or self.pincode)


class WorkerProfileData(BaseModel):
    """PRD 'Extraction output schema'. Stored in worker_profiles.profile_json."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=100)
    primary_trade: Trade
    skills: list[Skill] = Field(default_factory=list, max_length=15)
    years_experience: float | None = Field(default=None, ge=0, le=60)
    location: Location = Field(default_factory=Location)
    languages_spoken: list[str] = Field(default_factory=list, max_length=10)
    availability: Literal["full_time", "part_time", "gig"] | None = None
    expected_daily_pay_inr: float | None = Field(default=None, ge=0, le=100_000)
    missing_fields: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class EvidencedSkill(Skill):
    evidence: str = Field(max_length=200)


class LLMExtraction(WorkerProfileData):
    skills: list[EvidencedSkill] = Field(default_factory=list, max_length=15)  # type: ignore[assignment]


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {**schema, "type": [schema["type"], "null"]}


_STR = {"type": "string"}
_NUM = {"type": "number"}

# Hand-written for OpenAI strict structured outputs: every property required,
# additionalProperties false, nullability via type arrays, no numeric keywords.
LLM_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name", "primary_trade", "skills", "years_experience", "location",
        "languages_spoken", "availability", "expected_daily_pay_inr",
        "missing_fields", "confidence",
    ],
    "properties": {
        "name": _nullable(_STR),
        "primary_trade": {"type": "string", "enum": list(TRADES)},
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "level", "evidence"],
                "properties": {
                    "name": _STR,
                    "level": {"type": "string", "enum": ["basic", "skilled", "expert"]},
                    "evidence": _STR,
                },
            },
        },
        "years_experience": _nullable(_NUM),
        "location": {
            "type": "object",
            "additionalProperties": False,
            "required": ["city", "area", "pincode"],
            "properties": {
                "city": _nullable(_STR),
                "area": _nullable(_STR),
                "pincode": _nullable(_STR),
            },
        },
        "languages_spoken": {"type": "array", "items": _STR},
        "availability": {
            "type": ["string", "null"], "enum": ["full_time", "part_time", "gig", None]
        },
        "expected_daily_pay_inr": _nullable(_NUM),
        "missing_fields": {
            "type": "array", "items": {"type": "string", "enum": list(REQUIRED_FIELDS)}
        },
        "confidence": _NUM,
    },
}  # fmt: skip
