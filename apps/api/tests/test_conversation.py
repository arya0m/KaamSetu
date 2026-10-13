"""FR-1: onboarding and consent state machine (pure; no I/O)."""

import pytest

from app import i18n
from app.services.conversation import Action, Event, State, handle

URL = "https://kaamsetu.test/privacy"


def test_fr1_first_message_sends_welcome_and_asks_language() -> None:
    out = handle(State.NEW, None, Event("hi"), URL)
    assert out.state is State.AWAITING_LANGUAGE
    assert out.action is Action.SAVE_PRECONSENT
    assert out.reply == i18n.welcome(URL)
    assert URL in out.reply


def test_fr1_first_message_can_be_media() -> None:
    out = handle(State.NEW, None, Event(None, has_media=True), URL)
    assert out.state is State.AWAITING_LANGUAGE


@pytest.mark.parametrize("code", i18n.LANGUAGE_CODES)
def test_fr1_choosing_language_asks_consent_in_that_language(code: str) -> None:
    number = str(i18n.LANGUAGE_CODES.index(code) + 1)
    out = handle(State.AWAITING_LANGUAGE, None, Event(number), URL)
    assert out.state is State.AWAITING_CONSENT
    assert out.language == code
    assert out.action is Action.SAVE_PRECONSENT
    assert out.reply == i18n.t("consent_request", code, privacy_url=URL)


def test_fr1_invalid_language_repeats_menu() -> None:
    out = handle(State.AWAITING_LANGUAGE, None, Event("what?"), URL)
    assert out.state is State.AWAITING_LANGUAGE
    assert out.action is Action.SAVE_PRECONSENT
    assert out.reply == i18n.language_retry()


@pytest.mark.parametrize("answer", ["1", "हाँ", "yes"])
def test_fr1_agree_grants_consent(answer: str) -> None:
    out = handle(State.AWAITING_CONSENT, "hi", Event(answer), URL)
    assert out.state is State.ACTIVE
    assert out.action is Action.GRANT_CONSENT
    assert out.language == "hi"
    assert out.reply == i18n.t("consent_thanks", "hi")


@pytest.mark.parametrize("answer", ["2", "नहीं", "no"])
def test_fr1_decline_forgets_everything(answer: str) -> None:
    out = handle(State.AWAITING_CONSENT, "hi", Event(answer), URL)
    assert out.state is State.NEW
    assert out.action is Action.FORGET
    assert out.language is None
    assert out.reply == i18n.t("consent_declined", "hi")


@pytest.mark.parametrize("event", [Event("maybe"), Event(None, has_media=True)])
def test_fr1_unclear_consent_asks_again_without_storing(event: Event) -> None:
    out = handle(State.AWAITING_CONSENT, "ta", event, URL)
    assert out.state is State.AWAITING_CONSENT
    assert out.action is Action.SAVE_PRECONSENT
    assert out.reply == i18n.t("consent_invalid", "ta")


def test_p2_active_worker_text_is_echoed_in_their_language() -> None:
    out = handle(State.ACTIVE, "bn", Event("কাজ চাই"), URL)
    assert out.state is State.ACTIVE
    assert out.action is Action.NONE
    assert out.reply == i18n.echo("কাজ চাই", "bn")


def test_p2_active_worker_media_gets_coming_soon() -> None:
    out = handle(State.ACTIVE, "en", Event(None, has_media=True), URL)
    assert out.reply == i18n.t("unsupported_media", "en")
