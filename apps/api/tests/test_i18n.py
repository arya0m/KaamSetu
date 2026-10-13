"""FR-1 / PRD Accessibility: worker text exists in every language and stays short."""

import re
import string

import pytest

from app import i18n

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
    text = i18n.t(key, code, privacy_url=PRIVACY_URL, text="x" * i18n.ECHO_MAX_CHARS)
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


def test_fr1_echo_is_clipped_to_keep_messages_short() -> None:
    msg = i18n.echo("a" * 1000, "en")
    assert msg.endswith("…")
    assert len(msg) < MAX_CHARS


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
