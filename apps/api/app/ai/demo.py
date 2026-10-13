"""Offline demo engines for DEMO_MODE (no API keys needed). Not AI and never in production.

- DemoSTT: Watson's language coverage (Hindi, English); returns a sample work
  description instead of listening, because it cannot really transcribe.
- DemoLLM: a keyword extractor that returns the PRD profile JSON. It quotes the
  words it matched as evidence, so the same "never invent skills" check applies.

They let the whole voice-to-profile flow be shown in the /demo simulator. Set
WATSON_STT_* or OPENAI_API_KEY to use the real providers instead.
"""

import json
import re
from typing import Any

from app.ai.llm import LLMResponse, Message
from app.ai.redact import ascii_digits

DEMO_TRANSCRIPTS = {
    "hi": (
        "मैं आठ साल से बिजली "
        "का काम करता हूँ, घर की "
        "wiring और inverter लगाता हूँ। "
        "लक्ष्मी नगर, Delhi में "
        "रहता हूँ।"
    ),
    "en": (
        "I have been an electrician for 8 years. I do house wiring and inverter installation. "
        "I live in Laxmi Nagar, Delhi."
    ),
}


class DemoSTT:
    provider = "demo-stt"

    def supports(self, language: str) -> bool:
        return language in DEMO_TRANSCRIPTS

    def transcribe(self, wav: bytes, language: str) -> tuple[str, float | None]:
        return DEMO_TRANSCRIPTS[language], 1.0


# (keyword as it may appear in a transcript, normalized value). Keywords are
# matched case-insensitively; non-English keywords are written as escapes so app
# code stays ASCII (worker-facing text lives in app/i18n).
_TRADES: dict[str, tuple[str, ...]] = {
    "electrician": ("electric", "wiring", "बिजली", "इलेक्ट्री"),
    "plumber": ("plumb", "pipe", "tap", "प्लंब", "नल"),
    "tailor": ("tailor", "stitch", "blouse", "सिलाई", "दर्ज़ी"),
    "driver": ("driver", "driving", "taxi", "truck", "ड्राइवर"),
    "beautician": ("beauti", "parlour", "makeup", "facial", "ब्यूटी"),
    "carpenter": ("carpent", "furniture", "बढ़ई", "फर्नीचर"),
    "delivery": ("deliver", "parcel", "डिलीवरी"),
}  # fmt: skip
_SKILLS: dict[str, tuple[str, ...]] = {
    "house wiring": ("wiring", "वायरिंग"),
    "inverter installation": ("inverter", "इन्वर्टर"),
    "motor repair": ("motor",),
    "pipe fitting": ("pipe", "पाइप"),
    "tap fitting": ("tap", "नल"),
    "blouse stitching": ("blouse", "ब्लाउज़"),
    "four-wheeler driving": ("taxi", "car ", "गाड़ी"),
    "heavy vehicle driving": ("truck",),
    "bridal makeup": ("makeup",),
    "facial": ("facial",),
    "furniture making": ("furniture", "फर्नीचर"),
    "parcel delivery": ("parcel",),
}  # fmt: skip
_CITIES: dict[str, tuple[str, ...]] = {
    "Delhi": ("delhi", "दिल्ली"),
    "Mumbai": ("mumbai", "मुंबई"),
    "Bengaluru": ("bengaluru", "bangalore"),
    "Pune": ("pune", "पुणे"),
    "Jaipur": ("jaipur", "जयपुर"),
    "Lucknow": ("lucknow", "लखनऊ"),
    "Kolkata": ("kolkata",),
    "Chennai": ("chennai",),
    "Hyderabad": ("hyderabad",),
    "Ahmedabad": ("ahmedabad",),
    "Noida": ("noida",),
}  # fmt: skip
_YEAR_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20,
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4,
    "पाँच": 5, "छह": 6, "सात": 7, "आठ": 8,
    "नौ": 9, "दस": 10, "बारह": 12,
    "पंद्रह": 15, "बीस": 20,
}  # fmt: skip
_YEAR_UNIT = r"(?:years?|yrs?|saal|साल|वर्ष)"
_YEARS_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*" + _YEAR_UNIT, re.IGNORECASE)
_YEARS_WORD = re.compile(r"(\S+)\s+" + _YEAR_UNIT, re.IGNORECASE)
_PINCODE = re.compile(r"(?<!\d)([1-9]\d{5})(?!\d)")


def _find(text: str, keywords: tuple[str, ...]) -> str | None:
    """The keyword exactly as it appears in the transcript (used as evidence)."""
    lower = text.casefold()
    for kw in keywords:
        i = lower.find(kw.casefold())
        if i >= 0:
            return text[i : i + len(kw)]
    return None


def extract(transcript: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    text = ascii_digits(transcript)
    out: dict[str, Any] = dict(previous or {})
    out.setdefault("name", None)
    out.setdefault("languages_spoken", [])
    out.setdefault("availability", None)
    out.setdefault("expected_daily_pay_inr", None)

    trade = next((t for t, kws in _TRADES.items() if _find(text, kws)), None)
    out["primary_trade"] = trade or out.get("primary_trade") or "other"

    skills = [
        {"name": name, "level": "skilled", "evidence": ev}
        for name, kws in _SKILLS.items()
        if (ev := _find(text, kws))
    ]
    out["skills"] = skills

    years: float | None = None
    if m := _YEARS_NUM.search(text):
        years = float(m.group(1))
    else:
        for m in _YEARS_WORD.finditer(text):
            if (word := m.group(1).casefold()) in _YEAR_WORDS:
                years = float(_YEAR_WORDS[word])
                break
    if years is not None:
        out["years_experience"] = years
    out.setdefault("years_experience", None)

    location = dict(out.get("location") or {"city": None, "area": None, "pincode": None})
    if city := next((c for c, kws in _CITIES.items() if _find(text, kws)), None):
        location["city"] = city
    if pin := _PINCODE.search(text):
        location["pincode"] = pin.group(1)
    out["location"] = {k: location.get(k) for k in ("city", "area", "pincode")}

    missing = []
    if out["primary_trade"] == "other":
        missing.append("primary_trade")
    if out["years_experience"] is None:
        missing.append("years_experience")
    if not any(out["location"].values()):
        missing.append("location")
    out["missing_fields"] = missing
    out["confidence"] = 0.5
    return out


class DemoLLM:
    provider = "demo-llm"
    model = "keyword-rules"

    def complete_json(
        self, messages: list[Message], schema: dict[str, Any], schema_name: str
    ) -> LLMResponse:
        payload = json.loads(messages[1]["content"])
        previous = payload.get("previous_profile")
        if previous:
            # Previous skills carry no evidence; keep them only if they are still found.
            previous = {k: v for k, v in previous.items() if k != "skills"}
        result = extract(payload["transcript"], previous)
        return LLMResponse(json.dumps(result, ensure_ascii=False), 0, 0)
