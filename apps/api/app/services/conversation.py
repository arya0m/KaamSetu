"""Conversation state machine: onboarding (FR-1) and voice-to-profile (FR-2 to FR-5).

Pure: no I/O. `handle` maps (context, inbound message) to an Outcome; when the
message is work information it returns PROCESS_INPUT and `app.services.inbound`
transcribes and extracts, then calls `after_extraction` to pick the next step.

    new --any--> awaiting_language --choice--> awaiting_consent
        awaiting_consent --yes--> awaiting_description   (worker created)
                         --no---> new                    (pre-consent data forgotten)
    awaiting_description --voice/text--> extract --+--> follow_up:<field>:<n>  (n <= 3)
    follow_up --answer--> extract -----------------+--> confirming (summary)
    confirming --1--> confirmed      --2--> awaiting_change --voice/text--> extract
    confirmed --any--> confirming (summary shown again)

The context is stored in workers.state, e.g. "follow_up:location:2".
"""

import enum
from dataclasses import dataclass

from app import i18n
from app.schemas.profile import REQUIRED_FIELDS, WorkerProfileData
from app.services.profile import missing_required


class State(enum.StrEnum):
    NEW = "new"
    AWAITING_LANGUAGE = "awaiting_language"
    AWAITING_CONSENT = "awaiting_consent"
    AWAITING_DESCRIPTION = "awaiting_description"
    FOLLOW_UP = "follow_up"
    CONFIRMING = "confirming"
    AWAITING_CHANGE = "awaiting_change"
    CONFIRMED = "confirmed"


INPUT_STATES = frozenset({State.AWAITING_DESCRIPTION, State.FOLLOW_UP, State.AWAITING_CHANGE})
LEGACY_STATES = {"active": State.AWAITING_DESCRIPTION}  # Phase 2 workers


@dataclass(frozen=True)
class Ctx:
    state: State
    field: str | None = None  # FOLLOW_UP: which field was asked
    asked: int = 0  # FOLLOW_UP: follow-ups asked so far in this round

    def encode(self) -> str:
        if self.state is State.FOLLOW_UP:
            return f"{self.state.value}:{self.field}:{self.asked}"
        return self.state.value

    @classmethod
    def decode(cls, raw: str) -> "Ctx":
        if raw in LEGACY_STATES:
            return cls(LEGACY_STATES[raw])
        head, _, rest = raw.partition(":")
        state = State(head)
        if state is State.FOLLOW_UP:
            field, _, asked = rest.partition(":")
            if field not in REQUIRED_FIELDS or not asked.isdigit():
                raise ValueError(f"bad follow-up state {raw!r}")
            return cls(state, field, int(asked))
        return cls(state)


class Action(enum.StrEnum):
    SAVE_PRECONSENT = "save_preconsent"  # step + language in Redis (TTL), not Postgres
    GRANT_CONSENT = "grant_consent"  # create the worker: first moment anything is stored
    FORGET = "forget"  # drop all pre-consent data
    PROCESS_INPUT = "process_input"  # transcribe/store/extract, then after_extraction()
    CONFIRM_PROFILE = "confirm_profile"  # mark worker_profiles.confirmed
    SHOW_SUMMARY = "show_summary"  # render the stored profile with Correct/Change
    SAVE_PROFILE = "save_profile"  # store the extracted (unconfirmed) profile
    NONE = "none"


@dataclass(frozen=True)
class Event:
    text: str | None
    has_media: bool = False
    is_audio: bool = False


@dataclass(frozen=True)
class Outcome:
    ctx: Ctx
    language: str | None
    action: Action
    reply: str | None  # None when the follow-up step decides the reply

    @property
    def state(self) -> State:
        return self.ctx.state


def handle(
    ctx: Ctx,
    language: str | None,
    event: Event,
    privacy_url: str,
    voice_languages: frozenset[str] = frozenset(),
) -> Outcome:
    state = ctx.state
    lang = language or "en"

    if state is State.NEW:
        return Outcome(
            Ctx(State.AWAITING_LANGUAGE), None, Action.SAVE_PRECONSENT, i18n.welcome(privacy_url)
        )

    if state is State.AWAITING_LANGUAGE:
        chosen = i18n.match_language(event.text)
        if chosen is None:
            return Outcome(ctx, None, Action.SAVE_PRECONSENT, i18n.language_retry())
        reply = i18n.t("consent_request", chosen, privacy_url=privacy_url)
        return Outcome(Ctx(State.AWAITING_CONSENT), chosen, Action.SAVE_PRECONSENT, reply)

    if state is State.AWAITING_CONSENT:
        answer = i18n.parse_consent(event.text, lang)
        if answer is True:
            reply = i18n.consent_granted(lang, lang in voice_languages)
            return Outcome(Ctx(State.AWAITING_DESCRIPTION), lang, Action.GRANT_CONSENT, reply)
        if answer is False:
            return Outcome(Ctx(State.NEW), None, Action.FORGET, i18n.t("consent_declined", lang))
        return Outcome(ctx, lang, Action.SAVE_PRECONSENT, i18n.t("consent_invalid", lang))

    if state in INPUT_STATES:
        if event.is_audio:
            if lang not in voice_languages:
                return Outcome(ctx, lang, Action.NONE, i18n.t("voice_type_instead", lang))
            return Outcome(ctx, lang, Action.PROCESS_INPUT, None)
        if event.text:
            return Outcome(ctx, lang, Action.PROCESS_INPUT, None)
        return Outcome(ctx, lang, Action.NONE, i18n.t("unsupported_media", lang))

    if state is State.CONFIRMING:
        answer = i18n.parse_consent(event.text, lang)
        if answer is True:
            return Outcome(
                Ctx(State.CONFIRMED),
                lang,
                Action.CONFIRM_PROFILE,
                i18n.t("profile_confirmed", lang),
            )
        if answer is False:
            return Outcome(
                Ctx(State.AWAITING_CHANGE), lang, Action.NONE, i18n.t("change_prompt", lang)
            )
        return Outcome(ctx, lang, Action.NONE, i18n.t("confirm_invalid", lang))

    # CONFIRMED: any message shows the profile again so it can be changed.
    return Outcome(Ctx(State.CONFIRMING), lang, Action.SHOW_SUMMARY, None)


def after_extraction(
    ctx: Ctx, language: str, profile: WorkerProfileData | None, max_follow_ups: int
) -> Outcome:
    """FR-4: ask for one missing field at a time (max `max_follow_ups`), then FR-5 summary.

    A failed extraction (profile None) counts as everything missing (FR-3 fallback).
    """
    asked = ctx.asked if ctx.state is State.FOLLOW_UP else 0
    missing = missing_required(profile)
    if missing and asked < max_follow_ups:
        field = missing[0]
        return Outcome(
            Ctx(State.FOLLOW_UP, field, asked + 1),
            language,
            Action.SAVE_PROFILE if profile else Action.NONE,
            i18n.follow_up(field, language),
        )
    if profile is None:
        return Outcome(
            Ctx(State.AWAITING_DESCRIPTION),
            language,
            Action.NONE,
            i18n.t("try_again_later", language),
        )
    return Outcome(
        Ctx(State.CONFIRMING),
        language,
        Action.SAVE_PROFILE,
        i18n.profile_summary(profile, language),
    )
