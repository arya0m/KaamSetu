"""Conversation state machine (FR-1 onboarding and consent; Phase 2 text echo).

Pure: no I/O. Given the current state and one inbound message, it returns the
next state, what to persist, and the reply text. `app.services.inbound` applies it.

    new --any--> awaiting_language --valid choice--> awaiting_consent
                   |  ^ invalid                       | yes     -> active (worker created)
                   +--+                               | no      -> new (pre-consent data forgotten)
                                                      + unclear -> ask again
"""

import enum
from dataclasses import dataclass

from app import i18n


class State(enum.StrEnum):
    NEW = "new"
    AWAITING_LANGUAGE = "awaiting_language"
    AWAITING_CONSENT = "awaiting_consent"
    ACTIVE = "active"


class Action(enum.StrEnum):
    SAVE_PRECONSENT = "save_preconsent"  # keep step + language in Redis (TTL), not Postgres
    GRANT_CONSENT = "grant_consent"  # create the worker; first moment anything is stored
    FORGET = "forget"  # drop all pre-consent data
    NONE = "none"


@dataclass(frozen=True)
class Event:
    text: str | None
    has_media: bool = False


@dataclass(frozen=True)
class Outcome:
    state: State
    language: str | None
    action: Action
    reply: str


def handle(state: State, language: str | None, event: Event, privacy_url: str) -> Outcome:
    if state is State.ACTIVE:
        lang = language or "en"
        if event.text:
            return Outcome(state, lang, Action.NONE, i18n.echo(event.text, lang))
        return Outcome(state, lang, Action.NONE, i18n.t("unsupported_media", lang))

    if state is State.NEW:
        return Outcome(
            State.AWAITING_LANGUAGE, None, Action.SAVE_PRECONSENT, i18n.welcome(privacy_url)
        )

    if state is State.AWAITING_LANGUAGE:
        chosen = i18n.match_language(event.text)
        if chosen is None:
            return Outcome(state, None, Action.SAVE_PRECONSENT, i18n.language_retry())
        reply = i18n.t("consent_request", chosen, privacy_url=privacy_url)
        return Outcome(State.AWAITING_CONSENT, chosen, Action.SAVE_PRECONSENT, reply)

    # AWAITING_CONSENT
    lang = language or "en"
    answer = i18n.parse_consent(event.text, lang)
    if answer is True:
        return Outcome(State.ACTIVE, lang, Action.GRANT_CONSENT, i18n.t("consent_thanks", lang))
    if answer is False:
        return Outcome(State.NEW, None, Action.FORGET, i18n.t("consent_declined", lang))
    return Outcome(state, lang, Action.SAVE_PRECONSENT, i18n.t("consent_invalid", lang))
