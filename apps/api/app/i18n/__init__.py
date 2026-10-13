"""Worker-facing text. Every string a worker sees lives in `locales/<code>.json`.

Languages are offered as a numbered menu because the Twilio WhatsApp sandbox
cannot send interactive buttons. Answers are matched by menu number (in any
script's digits), native name, English name or ISO code.
"""

import json
import unicodedata
from functools import cache
from pathlib import Path
from typing import Any

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
    "echo",
    "unsupported_media",
)
PRE_LANGUAGE_KEYS = ("welcome", "choose_language", "language_invalid")
WELCOME_LANGUAGES = ("hi", "en")

ECHO_MAX_CHARS = 200


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


def echo(text: str, language: str) -> str:
    clipped = text if len(text) <= ECHO_MAX_CHARS else text[: ECHO_MAX_CHARS - 1] + "…"
    return t("echo", language, text=clipped)


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
    """True for agree, False for decline, None if unclear. "1"/"2" work everywhere."""
    if not text:
        return None
    answer = _normalize(text)
    locales = [_locale(code) for code in {language, "en"} if code in LANGUAGE_CODES]
    if answer == "1" or any(answer in map(_normalize, loc["yes_words"]) for loc in locales):
        return True
    if answer == "2" or any(answer in map(_normalize, loc["no_words"]) for loc in locales):
        return False
    return None
