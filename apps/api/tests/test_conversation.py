"""FR-1 to FR-5: conversation state machine (pure; no I/O)."""

import pytest

from app import i18n
from app.schemas.profile import WorkerProfileData
from app.services.conversation import Action, Ctx, Event, State, after_extraction, handle

URL = "https://kaamsetu.test/privacy"
VOICE = frozenset({"hi", "en"})


def _profile(**overrides: object) -> WorkerProfileData:
    data: dict[str, object] = {
        "primary_trade": "plumber",
        "skills": [{"name": "pipe fitting", "level": "skilled"}],
        "years_experience": 4,
        "location": {"city": "Pune"},
        "confidence": 0.8,
    }
    data.update(overrides)
    return WorkerProfileData.model_validate(data)


# --- FR-1 onboarding ----------------------------------------------------------


def test_fr1_first_message_sends_welcome_and_asks_language() -> None:
    out = handle(Ctx(State.NEW), None, Event("hi"), URL, VOICE)
    assert out.state is State.AWAITING_LANGUAGE
    assert out.action is Action.SAVE_PRECONSENT
    assert out.reply == i18n.welcome(URL)


def test_fr1_first_message_can_be_media() -> None:
    out = handle(Ctx(State.NEW), None, Event(None, has_media=True, is_audio=True), URL, VOICE)
    assert out.state is State.AWAITING_LANGUAGE


@pytest.mark.parametrize("code", i18n.LANGUAGE_CODES)
def test_fr1_choosing_language_asks_consent_in_that_language(code: str) -> None:
    number = str(i18n.LANGUAGE_CODES.index(code) + 1)
    out = handle(Ctx(State.AWAITING_LANGUAGE), None, Event(number), URL, VOICE)
    assert out.state is State.AWAITING_CONSENT
    assert out.language == code
    assert out.reply == i18n.t("consent_request", code, privacy_url=URL)


def test_fr1_invalid_language_repeats_menu() -> None:
    out = handle(Ctx(State.AWAITING_LANGUAGE), None, Event("what?"), URL, VOICE)
    assert out.state is State.AWAITING_LANGUAGE
    assert out.reply == i18n.language_retry()


@pytest.mark.parametrize(("code", "voice_ok"), [("hi", True), ("en", True), ("ta", False)])
def test_fr1_fr2_agree_grants_consent_and_asks_for_work(code: str, voice_ok: bool) -> None:
    out = handle(Ctx(State.AWAITING_CONSENT), code, Event("1"), URL, VOICE)
    assert out.state is State.AWAITING_DESCRIPTION
    assert out.action is Action.GRANT_CONSENT
    assert out.reply == i18n.consent_granted(code, voice_ok)


@pytest.mark.parametrize("answer", ["2", "नहीं", "no"])
def test_fr1_decline_forgets_everything(answer: str) -> None:
    out = handle(Ctx(State.AWAITING_CONSENT), "hi", Event(answer), URL, VOICE)
    assert out.state is State.NEW
    assert out.action is Action.FORGET
    assert out.reply == i18n.t("consent_declined", "hi")


def test_fr1_unclear_consent_asks_again() -> None:
    out = handle(Ctx(State.AWAITING_CONSENT), "ta", Event("maybe"), URL, VOICE)
    assert out.state is State.AWAITING_CONSENT
    assert out.reply == i18n.t("consent_invalid", "ta")


# --- FR-2 input -------------------------------------------------------------------

INPUT_CTXS = [
    Ctx(State.AWAITING_DESCRIPTION),
    Ctx(State.FOLLOW_UP, "location", 1),
    Ctx(State.AWAITING_CHANGE),
]


@pytest.mark.parametrize("ctx", INPUT_CTXS)
def test_fr2_text_and_supported_voice_are_processed(ctx: Ctx) -> None:
    assert handle(ctx, "hi", Event("bijli ka kaam"), URL, VOICE).action is Action.PROCESS_INPUT
    voice = handle(ctx, "hi", Event(None, has_media=True, is_audio=True), URL, VOICE)
    assert voice.action is Action.PROCESS_INPUT
    assert voice.reply is None


@pytest.mark.parametrize("ctx", INPUT_CTXS)
def test_fr2_voice_in_unsupported_language_asks_to_type(ctx: Ctx) -> None:
    out = handle(ctx, "ta", Event(None, has_media=True, is_audio=True), URL, VOICE)
    assert out.action is Action.NONE
    assert out.ctx == ctx
    assert out.reply == i18n.t("voice_type_instead", "ta")


def test_fr2_text_works_in_every_language() -> None:
    for code in i18n.LANGUAGE_CODES:
        out = handle(Ctx(State.AWAITING_DESCRIPTION), code, Event("work"), URL, VOICE)
        assert out.action is Action.PROCESS_INPUT


def test_fr2_photo_is_not_accepted() -> None:
    out = handle(Ctx(State.AWAITING_DESCRIPTION), "en", Event(None, has_media=True), URL, VOICE)
    assert out.action is Action.NONE
    assert out.reply == i18n.t("unsupported_media", "en")


# --- FR-4 follow-ups and FR-5 confirmation ------------------------------------


def test_fr4_complete_profile_goes_straight_to_summary() -> None:
    profile = _profile()
    out = after_extraction(Ctx(State.AWAITING_DESCRIPTION), "en", profile, 3)
    assert out.ctx == Ctx(State.CONFIRMING)
    assert out.action is Action.SAVE_PROFILE
    assert out.reply == i18n.profile_summary(profile, "en")


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"primary_trade": "other", "missing_fields": ["primary_trade"]}, "primary_trade"),
        ({"years_experience": None}, "years_experience"),
        ({"location": {}}, "location"),
    ],
)
def test_fr4_asks_one_missing_field(overrides: dict[str, object], field: str) -> None:
    out = after_extraction(Ctx(State.AWAITING_DESCRIPTION), "hi", _profile(**overrides), 3)
    assert out.ctx == Ctx(State.FOLLOW_UP, field, 1)
    assert out.action is Action.SAVE_PROFILE
    assert out.reply == i18n.follow_up(field, "hi")


def test_fr4_asks_in_fixed_order_one_at_a_time() -> None:
    profile = _profile(years_experience=None, location={})
    first = after_extraction(Ctx(State.AWAITING_DESCRIPTION), "en", profile, 3)
    assert first.ctx.field == "years_experience"
    second = after_extraction(first.ctx, "en", _profile(location={}), 3)
    assert second.ctx == Ctx(State.FOLLOW_UP, "location", 2)


def test_fr4_other_trade_is_fine_when_the_worker_said_it() -> None:
    out = after_extraction(
        Ctx(State.AWAITING_DESCRIPTION), "en", _profile(primary_trade="other"), 3
    )
    assert out.ctx.state is State.CONFIRMING


def test_fr4_at_most_three_follow_ups_then_summary() -> None:
    profile = _profile(years_experience=None)
    ctx = Ctx(State.AWAITING_DESCRIPTION)
    for n in (1, 2, 3):
        out = after_extraction(ctx, "en", profile, 3)
        assert out.ctx == Ctx(State.FOLLOW_UP, "years_experience", n)
        ctx = out.ctx
    final = after_extraction(ctx, "en", profile, 3)
    assert final.ctx.state is State.CONFIRMING
    assert "not given" in (final.reply or "")


def test_fr3_failed_extraction_falls_back_to_a_follow_up() -> None:
    out = after_extraction(Ctx(State.AWAITING_DESCRIPTION), "hi", None, 3)
    assert out.ctx == Ctx(State.FOLLOW_UP, "primary_trade", 1)
    assert out.action is Action.NONE  # nothing to save


def test_fr3_failed_extraction_after_all_follow_ups_restarts() -> None:
    out = after_extraction(Ctx(State.FOLLOW_UP, "location", 3), "hi", None, 3)
    assert out.ctx == Ctx(State.AWAITING_DESCRIPTION)
    assert out.reply == i18n.t("try_again_later", "hi")


def test_fr5_change_restarts_the_follow_up_count() -> None:
    out = after_extraction(Ctx(State.AWAITING_CHANGE), "en", _profile(location={}), 3)
    assert out.ctx == Ctx(State.FOLLOW_UP, "location", 1)


@pytest.mark.parametrize("answer", ["1", "yes", "हाँ"])
def test_fr5_correct_confirms(answer: str) -> None:
    out = handle(Ctx(State.CONFIRMING), "hi", Event(answer), URL, VOICE)
    assert out.ctx == Ctx(State.CONFIRMED)
    assert out.action is Action.CONFIRM_PROFILE
    assert out.reply == i18n.t("profile_confirmed", "hi")


@pytest.mark.parametrize("answer", ["2", "no"])
def test_fr5_change_asks_what_to_change(answer: str) -> None:
    out = handle(Ctx(State.CONFIRMING), "en", Event(answer), URL, VOICE)
    assert out.ctx == Ctx(State.AWAITING_CHANGE)
    assert out.reply == i18n.t("change_prompt", "en")


def test_fr5_unclear_confirmation_asks_again() -> None:
    out = handle(Ctx(State.CONFIRMING), "en", Event("hmm"), URL, VOICE)
    assert out.ctx == Ctx(State.CONFIRMING)
    assert out.reply == i18n.t("confirm_invalid", "en")


def test_fr5_message_after_confirming_shows_summary_again() -> None:
    out = handle(Ctx(State.CONFIRMED), "en", Event("hello"), URL, VOICE)
    assert out.ctx == Ctx(State.CONFIRMING)
    assert out.action is Action.SHOW_SUMMARY


# --- context encoding ------------------------------------------------------------


@pytest.mark.parametrize(
    "ctx",
    [
        Ctx(State.AWAITING_DESCRIPTION),
        Ctx(State.FOLLOW_UP, "years_experience", 3),
        Ctx(State.CONFIRMED),
    ],
)
def test_p3_context_round_trips_through_workers_state(ctx: Ctx) -> None:
    encoded = ctx.encode()
    assert len(encoded) <= 32  # workers.state is String(32)
    assert Ctx.decode(encoded) == ctx


def test_p3_phase2_active_state_maps_to_description() -> None:
    assert Ctx.decode("active") == Ctx(State.AWAITING_DESCRIPTION)


@pytest.mark.parametrize("raw", ["follow_up:salary:1", "follow_up:location:x", "bogus"])
def test_p3_bad_state_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        Ctx.decode(raw)
