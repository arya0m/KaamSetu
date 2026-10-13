"""FR-2 to FR-5 end to end: voice/text -> transcript -> profile -> follow-ups -> confirm.

Real PostgreSQL; WhatsApp, STT and LLM are fakes; Celery runs eagerly.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import i18n
from app.models import VoiceNote, Worker, WorkerProfile
from app.schemas.profile import WorkerProfileData
from tests.conftest import Loop, profile_json

pytestmark = pytest.mark.db

HI = "+919800000001"
EN = "+919800000002"
TA = "+919800000003"
VOICE_URL = "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages/MM1/Media/ME1"


def onboard(loop: Loop, phone: str, language_choice: str) -> None:
    for body in ("hi", language_choice, "1"):
        loop.send(phone, body)


def profile_row(loop: Loop, phone: str) -> WorkerProfile | None:
    with Session(loop.engine) as s:
        worker = loop.worker(phone)
        assert worker is not None
        return s.scalar(select(WorkerProfile).where(WorkerProfile.worker_id == worker.id))


def notes(loop: Loop, phone: str) -> list[VoiceNote]:
    with Session(loop.engine) as s:
        worker = loop.worker(phone)
        assert worker is not None
        return list(
            s.scalars(
                select(VoiceNote)
                .where(VoiceNote.worker_id == worker.id)
                .order_by(VoiceNote.created_at)
            )
        )


def state(loop: Loop, phone: str) -> str:
    worker = loop.worker(phone)
    assert worker is not None
    return worker.state


def summary_for(reply_json: str, language: str) -> str:
    import json

    data = json.loads(reply_json)
    data["skills"] = [{"name": s["name"], "level": s["level"]} for s in data["skills"]]
    return i18n.profile_summary(WorkerProfileData.model_validate(data), language)


def test_fr2_fr5_hindi_voice_note_to_confirmed_profile(loop: Loop) -> None:
    onboard(loop, HI, "1")
    loop.wa.media[VOICE_URL] = b"OGG"
    loop.stt.transcripts = ["मैं आठ साल से wiring का काम करता हूँ, लक्ष्मी नगर दिल्ली"]
    reply = profile_json()
    loop.llm.replies = [reply]

    loop.send(HI, media_url=VOICE_URL)
    replies = loop.wa.bodies_to(HI)
    assert replies[-2] == i18n.t("voice_listening", "hi")  # FR-2: immediate acknowledgement
    assert replies[-1] == summary_for(reply, "hi")  # FR-5 summary in Hindi
    assert state(loop, HI) == "confirming"

    (note,) = notes(loop, HI)
    assert note.media_url == VOICE_URL
    assert note.transcript == loop.stt.transcripts[0]
    assert note.language == "hi" and note.duration_s == 12.0
    row = profile_row(loop, HI)
    assert row is not None and row.confirmed is False
    assert row.profile_json["primary_trade"] == "electrician"
    assert "evidence" not in row.profile_json["skills"][0]

    loop.send(HI, "1")
    assert loop.last_reply(HI) == i18n.t("profile_confirmed", "hi")
    assert state(loop, HI) == "confirmed"
    row = profile_row(loop, HI)
    assert row is not None and row.confirmed is True


def test_fr4_follow_ups_fill_missing_fields_one_at_a_time(loop: Loop) -> None:
    onboard(loop, EN, "2")
    loop.llm.replies = [
        profile_json(years_experience=None, location={"city": None, "area": None, "pincode": None}),
        profile_json(location={"city": None, "area": None, "pincode": None}),
        profile_json(),
    ]
    loop.send(EN, "I do house wiring")
    assert loop.last_reply(EN) == i18n.follow_up("years_experience", "en")
    assert state(loop, EN) == "follow_up:years_experience:1"

    loop.send(EN, "8 years")
    assert loop.last_reply(EN) == i18n.follow_up("location", "en")
    assert state(loop, EN) == "follow_up:location:2"
    assert loop.llm.user_payload()["latest_message_answers"] == "years_experience"

    loop.send(EN, "Laxmi Nagar, Delhi")
    assert loop.last_reply(EN).startswith(i18n.t("summary_title", "en"))
    assert state(loop, EN) == "confirming"
    payload = loop.llm.user_payload()
    assert payload["latest_message_answers"] == "location"
    assert payload["transcript"] == (
        "Message 1: I do house wiring\nMessage 2: 8 years\nMessage 3: Laxmi Nagar, Delhi"
    )
    assert payload["previous_profile"] is not None


def test_fr4_stops_after_three_follow_ups(loop: Loop) -> None:
    onboard(loop, EN, "2")
    loop.llm.replies = [profile_json(years_experience=None)]
    loop.send(EN, "I do wiring")
    for _ in range(3):
        loop.send(EN, "not sure")
    assert loop.last_reply(EN).startswith(i18n.t("summary_title", "en"))
    assert "Experience: not given" in loop.last_reply(EN)
    follow_ups = loop.wa.bodies_to(EN).count(i18n.follow_up("years_experience", "en"))
    assert follow_ups == 3


def test_fr5_change_reextracts_on_the_combined_transcript(loop: Loop) -> None:
    onboard(loop, EN, "2")
    loop.llm.replies = [profile_json(), profile_json(primary_trade="plumber", skills=[])]
    loop.send(EN, "I do wiring in Laxmi Nagar Delhi for 8 years")
    loop.send(EN, "2")
    assert loop.last_reply(EN) == i18n.t("change_prompt", "en")
    loop.send(EN, "actually I am a plumber")
    assert "Plumber" in loop.last_reply(EN)
    assert loop.llm.user_payload()["transcript"] == (
        "Message 1: I do wiring in Laxmi Nagar Delhi for 8 years\n"
        "Message 2: actually I am a plumber"
    )
    row = profile_row(loop, EN)
    assert row is not None and row.profile_json["primary_trade"] == "plumber"
    assert row.confirmed is False


def test_fr5_confirmed_worker_can_see_and_change_profile_later(loop: Loop) -> None:
    onboard(loop, EN, "2")
    loop.send(EN, "I do wiring")
    loop.send(EN, "1")
    loop.send(EN, "hello again")
    assert loop.last_reply(EN).startswith(i18n.t("summary_title", "en"))
    assert state(loop, EN) == "confirming"


def test_fr3_invalid_llm_output_twice_falls_back_to_a_follow_up(loop: Loop) -> None:
    onboard(loop, EN, "2")
    loop.llm.replies = ["garbage", "still garbage"]
    loop.send(EN, "I do some work")
    assert len(loop.llm.calls) == 2  # one retry with the validation error
    assert loop.last_reply(EN) == i18n.follow_up("primary_trade", "en")
    assert profile_row(loop, EN) is None


def test_fr2_text_only_language_asks_to_type_and_accepts_text(loop: Loop) -> None:
    onboard(loop, TA, "6")  # Tamil: Watson has no model
    assert loop.last_reply(TA) == i18n.consent_granted("ta", voice_ok=False)
    loop.wa.media[VOICE_URL] = b"OGG"
    loop.send(TA, media_url=VOICE_URL)
    assert loop.last_reply(TA) == i18n.t("voice_type_instead", "ta")
    assert loop.stt.calls == 0 and loop.llm.calls == []

    loop.send(TA, "நான் 8 ஆண்டுகளாக wiring வேலை செய்கிறேன், சென்னை")
    assert loop.last_reply(TA).startswith(i18n.t("summary_title", "ta"))
    (note,) = notes(loop, TA)
    assert note.media_url is None and note.language == "ta"


def test_fr2_voice_note_over_two_minutes_is_rejected(loop: Loop) -> None:
    onboard(loop, HI, "1")
    loop.wa.media[VOICE_URL] = b"TOO-LONG"
    loop.send(HI, media_url=VOICE_URL)
    assert loop.last_reply(HI) == i18n.t("voice_too_long", "hi")
    assert state(loop, HI) == "awaiting_description"
    assert notes(loop, HI) == [] and loop.llm.calls == []


def test_fr2_unreadable_voice_note_asks_again(loop: Loop) -> None:
    onboard(loop, HI, "1")
    loop.send(HI, media_url=VOICE_URL)  # not in the fake provider: download fails
    assert loop.last_reply(HI) == i18n.t("voice_failed", "hi")
    loop.wa.media[VOICE_URL] = b"OGG"
    loop.stt.transcripts = ["   "]
    loop.send(HI, media_url=VOICE_URL)
    assert loop.last_reply(HI) == i18n.t("voice_failed", "hi")
    assert notes(loop, HI) == []


def test_fr2_photo_after_consent_is_not_processed(loop: Loop) -> None:
    onboard(loop, EN, "2")
    loop.send(EN, num_media=1)
    assert loop.last_reply(EN) == i18n.t("unsupported_media", "en")


def test_fr2_every_reply_is_logged_for_registered_workers(loop: Loop) -> None:
    from app.models import Message, MessageDirection

    onboard(loop, EN, "2")
    loop.send(EN, "I do wiring")
    with Session(loop.engine) as s:
        directions = s.scalars(select(Message.direction).order_by(Message.created_at)).all()
    assert directions.count(MessageDirection.OUTBOUND) == 2  # consent reply + summary


def test_fr11_prep_deleting_worker_removes_profile_and_transcripts(loop: Loop) -> None:
    """Cascade groundwork for FR-11: transcripts and profile go with the worker."""
    from sqlalchemy import delete

    onboard(loop, EN, "2")
    loop.send(EN, "I do wiring")
    with Session(loop.engine) as s:
        s.execute(delete(Worker))
        s.commit()
        assert s.scalars(select(VoiceNote)).all() == []
        assert s.scalars(select(WorkerProfile)).all() == []
