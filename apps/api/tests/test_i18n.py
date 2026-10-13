"""FR-1 / PRD Accessibility: worker text exists in every language and stays short."""

import re
import string
from typing import TYPE_CHECKING

import pytest

from app import i18n

if TYPE_CHECKING:
    from app.schemas.profile import WorkerProfileData

PRIVACY_URL = "https://kaamsetu.test/privacy"
MAX_CHARS = 300  # PRD: worker messages under 300 characters


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


@pytest.mark.parametrize("code", i18n.LANGUAGE_CODES)
def test_fr1_every_language_has_all_keys(code: str) -> None:
    loc = i18n._locale(code)
    required = {"name", "english_name", "yes_words", "no_words", *i18n.PER_LANGUAGE_KEYS}
    if code in i18n.WELCOME_LANGUAGES:
        required |= set(i18n.PRE_LANGUAGE_KEYS)
    assert required <= set(loc), f"{code} missing {required - set(loc)}"
    assert loc["yes_words"] and loc["no_words"]
    assert not set(map(i18n._normalize, loc["yes_words"])) & set(
        map(i18n._normalize, loc["no_words"])
    )


@pytest.mark.parametrize("code", i18n.LANGUAGE_CODES)
def test_fr1_placeholders_match_english(code: str) -> None:
    for key in i18n.PER_LANGUAGE_KEYS:
        assert _fields(i18n._locale(code)[key]) == _fields(i18n._locale("en")[key]), (code, key)


@pytest.mark.parametrize("code", i18n.LANGUAGE_CODES)
@pytest.mark.parametrize("key", i18n.PER_LANGUAGE_KEYS)
def test_fr1_messages_under_300_chars(code: str, key: str) -> None:
    text = i18n.t(key, code, privacy_url=PRIVACY_URL, years="12.5")
    assert len(text) < MAX_CHARS, (code, key, len(text))


def test_fr1_welcome_is_bilingual_links_privacy_and_lists_all_languages() -> None:
    msg = i18n.welcome(PRIVACY_URL)
    assert len(msg) < MAX_CHARS, len(msg)
    assert PRIVACY_URL in msg
    assert "नमस्ते" in msg and "Welcome" in msg
    for i, code in enumerate(i18n.LANGUAGE_CODES, 1):
        assert f"{i} {i18n.language_name(code)}" in msg
    assert len(i18n.language_retry()) < MAX_CHARS


def test_fr1_menu_has_13_languages_with_unique_names() -> None:
    assert len(i18n.LANGUAGE_CODES) == 13
    names = [i18n.language_name(c) for c in i18n.LANGUAGE_CODES]
    assert len(set(names)) == len(names)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("1", "hi"),
        (" 2 ", "en"),
        ("१", "hi"),  # Devanagari digit one
        ("৩", "bn"),  # Bengali digit three
        ("13", "ur"),
        ("हिन्दी", "hi"),
        ("tamil", "ta"),
        ("TAMIL", "ta"),
        ("தமிழ்", "ta"),
        ("mr", "mr"),
        ("ଓଡ଼ିଆ", "or"),
    ],
)
def test_fr1_language_choice_by_number_name_or_code(answer: str, expected: str) -> None:
    assert i18n.match_language(answer) == expected


@pytest.mark.parametrize("answer", [None, "", "0", "14", "french", "hello there"])
def test_fr1_unknown_language_choice(answer: str | None) -> None:
    assert i18n.match_language(answer) is None


@pytest.mark.parametrize(
    ("answer", "language", "expected"),
    [
        ("1", "ta", True),
        ("2", "ta", False),
        ("हाँ", "hi", True),
        ("नहीं", "hi", False),
        ("Yes", "hi", True),  # English words work in every language
        ("no", "bn", False),
        ("ஆம்", "ta", True),
        ("maybe", "en", None),
        ("", "en", None),
        (None, "en", None),
    ],
)
def test_fr1_parse_consent(answer: str | None, language: str, expected: bool | None) -> None:
    assert i18n.parse_consent(answer, language) is expected


@pytest.mark.parametrize("code", i18n.LANGUAGE_CODES)
def test_fr5_every_language_names_every_trade(code: str) -> None:
    from app.schemas.profile import TRADES

    trades = i18n._locale(code)["trades"]
    assert set(trades) == set(TRADES)
    assert all(trades.values())


def _profile(**overrides: object) -> "WorkerProfileData":
    from app.schemas.profile import WorkerProfileData

    data: dict[str, object] = {
        "primary_trade": "electrician",
        "skills": [{"name": "house wiring", "level": "expert"}],
        "years_experience": 8,
        "location": {"city": "Delhi", "area": "Laxmi Nagar", "pincode": "110092"},
        "confidence": 0.9,
    }
    data.update(overrides)
    return WorkerProfileData.model_validate(data)


@pytest.mark.parametrize("code", i18n.LANGUAGE_CODES)
def test_fr5_summary_lists_fields_and_options_in_language(code: str) -> None:
    msg = i18n.profile_summary(_profile(), code)
    assert i18n.trade_name("electrician", code) in msg
    assert "house wiring" in msg
    assert "Laxmi Nagar, Delhi 110092" in msg
    assert i18n.t("years_value", code, years="8") in msg
    assert msg.endswith(i18n.t("confirm_options", code))
    assert len(msg) < MAX_CHARS


@pytest.mark.parametrize("code", i18n.LANGUAGE_CODES)
def test_fr5_summary_stays_short_with_many_long_skills(code: str) -> None:
    skills = [
        {"name": f"very long specialised skill number {i}", "level": "skilled"} for i in range(15)
    ]
    msg = i18n.profile_summary(_profile(skills=skills), code)
    assert len(msg) < MAX_CHARS
    assert "skill number 0" in msg


def test_fr5_summary_shows_not_given_for_missing_fields() -> None:
    msg = i18n.profile_summary(_profile(skills=[], years_experience=None, location={}), "en")
    assert msg.count("not given") == 3


def test_fr5_years_are_formatted_compactly() -> None:
    assert "0.5 years" in i18n.profile_summary(_profile(years_experience=0.5), "en")
    assert "8 years" in i18n.profile_summary(_profile(years_experience=8.0), "en")


def test_fr2_consent_reply_matches_voice_capability() -> None:
    assert i18n.t("describe_work_voice", "hi") in i18n.consent_granted("hi", voice_ok=True)
    assert i18n.t("describe_work_text", "ta") in i18n.consent_granted("ta", voice_ok=False)
    for code in i18n.LANGUAGE_CODES:
        for voice_ok in (True, False):
            assert len(i18n.consent_granted(code, voice_ok)) < MAX_CHARS, code


def test_fr1_unknown_language_falls_back_to_english() -> None:
    assert i18n.t("consent_invalid", "xx") == i18n.t("consent_invalid", "en")


def test_fr1_no_worker_text_hardcoded_outside_i18n() -> None:
    """PRD rule: worker-facing text lives in app/i18n only."""
    from pathlib import Path

    app_dir = Path(i18n.__file__).resolve().parents[1]
    sources = [
        p for p in app_dir.rglob("*.py") if "i18n" not in p.parts and "migrations" not in p.parts
    ]
    non_ascii = re.compile(r"[^\x00-\x7f…]")
    offenders = [str(p) for p in sources if non_ascii.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
