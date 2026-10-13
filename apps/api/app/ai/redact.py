"""Strip phone and ID numbers from transcripts before they reach an LLM (PRD: Guardrails).

Pin codes (6 digits) are kept: matching needs them. Digits of any script are
first converted to ASCII, so a number written in Devanagari digits is caught too.
"""

import re
import unicodedata

# Placeholders the LLM sees instead of personal numbers (not credentials).
PHONE_TOKEN = "[PHONE]"  # noqa: S105
ID_TOKEN = "[ID]"  # noqa: S105
NUMBER_TOKEN = "[NUMBER]"  # noqa: S105

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Aadhaar: 12 digits, often grouped 4-4-4.
    (re.compile(r"(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\d)"), ID_TOKEN),
    # Indian mobile, optional +91/0 prefix, optional spaces or dashes.
    (re.compile(r"(?<![\d+])(?:\+?91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?!\d)"), PHONE_TOKEN),
    # PAN: ABCDE1234F. Voter ID (EPIC): ABC1234567. Passport: A1234567.
    (re.compile(r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b"), ID_TOKEN),
    (re.compile(r"\b[A-Za-z]{3}\d{7}\b"), ID_TOKEN),
    (re.compile(r"\b[A-Za-z]\d{7}\b"), ID_TOKEN),
)

# Any other number with 9+ digits (other phone formats, account numbers). Counted
# by digits, so a pay range like "500 - 700" or a pin code is left alone.
_LONG_NUMBER = re.compile(r"(?<!\d)\+?\d[\d -]*\d(?!\d)")
_MIN_LONG_DIGITS = 9


def _long_number(match: re.Match[str]) -> str:
    digits = sum(ch.isdigit() for ch in match.group())
    return NUMBER_TOKEN if digits >= _MIN_LONG_DIGITS else match.group()


def ascii_digits(text: str) -> str:
    return "".join(str(d) if (d := unicodedata.digit(ch, None)) is not None else ch for ch in text)


def redact(text: str) -> str:
    out = ascii_digits(text)
    for pattern, token in _PATTERNS:
        out = pattern.sub(token, out)
    return _LONG_NUMBER.sub(_long_number, out)
