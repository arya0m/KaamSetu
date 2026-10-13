"""Worker-facing text. Every string a worker sees lives in `locales/<code>.json`.

Languages are offered as a numbered menu because the Twilio WhatsApp sandbox
cannot send interactive buttons. Answers are matched by menu number (in any
script's digits), native name, English name or ISO code.
"""

import json
import unicodedata
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.schemas.profile import WorkerProfileData

LOCALES_DIR = Path(__file__).parent / "locales"

# Menu order. ISO 639-1 codes; stored in workers.language.
LANGUAGE_CODES: tuple[str, ...] = (
    "hi", "en", "bn", "mr", "te", "ta", "gu", "kn", "ml", "or", "pa", "as", "ur",
)  # fmt: skip

# Keys every locale must define. Pre-language keys are shown before the worker
# picks a language, so only Hindi and English need them (the welcome is bilingual).
PER_LANGUAGE_KEYS = (
    "consent_request",
    "consent_invalid",
    "consent_thanks",
    "consent_declined",
    "unsupported_media",
    "describe_work_voice",
    "describe_work_text",
    "voice_listening",
    "voice_too_long",
    "voice_failed",
    "voice_type_instead",
    "followup_primary_trade",
    "followup_years_experience",
    "followup_location",
    "summary_title",
    "label_trade",
    "label_skills",
    "label_experience",
    "label_location",
    "years_value",
    "not_given",
    "confirm_options",
    "confirm_invalid",
    "change_prompt",
    "profile_confirmed",
    "try_again_later",
)
PRE_LANGUAGE_KEYS = ("welcome", "choose_language", "language_invalid")
WELCOME_LANGUAGES = ("hi", "en")

MAX_MESSAGE_CHARS = 300  # PRD Accessibility: worker messages under 300 characters
SUMMARY_MAX_SKILLS = 5


@cache
def _locale(code: str) -> dict[str, Any]:
    with (LOCALES_DIR / f"{code}.json").open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


def t(key: str, language: str, **params: str) -> str:
    """Translated text for `key`. Unknown languages fall back to English."""
    code = language if language in LANGUAGE_CODES else "en"
    template: str = _locale(code)[key]
    return template.format(**params)


def language_name(code: str) -> str:
    name: str = _locale(code)["name"]
    return name


def _menu() -> str:
    return "\n".join(f"{i} {language_name(code)}" for i, code in enumerate(LANGUAGE_CODES, 1))


def welcome(privacy_url: str) -> str:
    """First message: bilingual greeting, privacy notice link (PRD: Privacy), menu."""
    lines = [t("welcome", code) for code in WELCOME_LANGUAGES]
    lines.append(t("privacy_line", "en", privacy_url=privacy_url))
    lines.append(" / ".join(t("choose_language", code) for code in WELCOME_LANGUAGES))
    return "\n".join([*lines, _menu()])


def language_retry() -> str:
    header = " / ".join(t("language_invalid", code) for code in WELCOME_LANGUAGES)
    return f"{header}\n{_menu()}"


def describe_work(language: str, voice_ok: bool) -> str:
    return t("describe_work_voice" if voice_ok else "describe_work_text", language)


def consent_granted(language: str, voice_ok: bool) -> str:
    """One message after "I agree": thanks plus what to send next (sandbox: 1 msg/step)."""
    return f"{t('consent_thanks', language)}\n{describe_work(language, voice_ok)}"


def follow_up(field: str, language: str) -> str:
    return t(f"followup_{field}", language)


def trade_name(trade: str, language: str) -> str:
    code = language if language in LANGUAGE_CODES else "en"
    name: str = _locale(code)["trades"][trade]
    return name


def _years(value: float) -> str:
    return f"{value:g}"


def profile_summary(profile: "WorkerProfileData", language: str) -> str:
    """FR-5: short profile summary with the Correct / Change options."""
    not_given = t("not_given", language)
    loc = profile.location
    place = ", ".join(p for p in (loc.area, loc.city) if p)
    if loc.pincode:
        place = f"{place} {loc.pincode}".strip()
    years = (
        t("years_value", language, years=_years(profile.years_experience))
        if profile.years_experience is not None
        else not_given
    )

    def render(skill_count: int) -> str:
        skills = ", ".join(s.name for s in profile.skills[:skill_count]) or not_given
        return "\n".join(
            [
                t("summary_title", language),
                f"{t('label_trade', language)}: {trade_name(profile.primary_trade, language)}",
                f"{t('label_skills', language)}: {skills}",
                f"{t('label_experience', language)}: {years}",
                f"{t('label_location', language)}: {place or not_given}",
                "",
                t("confirm_options", language),
            ]
        )

    count = min(SUMMARY_MAX_SKILLS, len(profile.skills))
    text = render(count)
    while len(text) >= MAX_MESSAGE_CHARS and count > 1:
        count -= 1
        text = render(count)
    return text


def _normalize(text: str) -> str:
    """Casefold, trim and convert digits of any script (१, ৩, ௫ …) to ASCII."""
    out = []
    for ch in unicodedata.normalize("NFC", text.strip()).casefold():
        digit = unicodedata.digit(ch, None)
        out.append(str(digit) if digit is not None else ch)
    return " ".join("".join(out).split())


@cache
def _language_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for i, code in enumerate(LANGUAGE_CODES, 1):
        loc = _locale(code)
        for alias in (str(i), code, loc["name"], loc["english_name"]):
            aliases[_normalize(alias)] = code
    return aliases


def match_language(text: str | None) -> str | None:
    if not text:
        return None
    return _language_aliases().get(_normalize(text))


def parse_consent(text: str | None, language: str) -> bool | None:
    """True for yes/agree/correct, False for no/change, None if unclear.

    "1" and "2" work in every language; used for consent and for confirmation.
    """
    if not text:
        return None
    answer = _normalize(text)
    locales = [_locale(code) for code in {language, "en"} if code in LANGUAGE_CODES]
    if answer == "1" or any(answer in map(_normalize, loc["yes_words"]) for loc in locales):
        return True
    if answer == "2" or any(answer in map(_normalize, loc["no_words"]) for loc in locales):
        return False
    return None
