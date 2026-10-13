"""FR-3: profile extraction — JSON validation, retry once, grounded skills, redaction."""

import json
import logging

import pytest

from app.ai.llm import LLMError, LLMResponse
from app.schemas.profile import WorkerProfileData
from app.services import profile as profile_svc
from app.services.profile import combined_transcript, extract_profile, missing_required
from tests.conftest import FakeLLM, profile_json

HINDI = "मैं दस साल से बिजली का काम करता हूँ, house wiring और मोटर रिपेयर, लक्ष्मी नगर दिल्ली में रहता हूँ"


def test_fr3_valid_reply_becomes_profile() -> None:
    llm = FakeLLM(
        [
            profile_json(
                skills=[{"name": "House Wiring", "level": "expert", "evidence": "house wiring"}]
            )
        ]
    )
    result = extract_profile(llm, [HINDI])
    assert isinstance(result, WorkerProfileData)
    assert result.primary_trade == "electrician"
    assert [s.name for s in result.skills] == ["house wiring"]  # normalized, evidence stripped
    assert len(llm.calls) == 1


def test_fr3_invalid_json_is_retried_once_with_the_error() -> None:
    llm = FakeLLM(["not json at all", profile_json()])
    result = extract_profile(llm, ["wiring ka kaam"])
    assert result is not None
    assert len(llm.calls) == 2
    retry = llm.calls[1]
    assert retry[-2] == {"role": "assistant", "content": "not json at all"}
    assert "invalid" in retry[-1]["content"] and "corrected JSON" in retry[-1]["content"]


def test_fr3_schema_violation_is_retried() -> None:
    llm = FakeLLM([profile_json(primary_trade="astronaut"), profile_json()])
    assert extract_profile(llm, ["wiring"]) is not None
    assert "primary_trade" in llm.calls[1][-1]["content"]


def test_fr3_two_invalid_replies_give_none_for_follow_up_fallback() -> None:
    llm = FakeLLM(["{}", "{}"])
    assert extract_profile(llm, ["wiring"]) is None
    assert len(llm.calls) == 2


def test_fr3_llm_error_gives_none() -> None:
    class Down(FakeLLM):
        def complete_json(
            self, messages: list[dict[str, str]], schema: dict[str, object], schema_name: str
        ) -> LLMResponse:
            raise LLMError("401")

    assert extract_profile(Down(), ["wiring"]) is None


def test_fr3_empty_transcript_skips_the_llm() -> None:
    llm = FakeLLM()
    assert extract_profile(llm, ["   "]) is None
    assert llm.calls == []


def test_fr3_guardrail_skills_without_verbatim_evidence_are_dropped() -> None:
    reply = profile_json(
        skills=[
            {"name": "house wiring", "level": "expert", "evidence": "house wiring"},
            {
                "name": "solar installation",
                "level": "basic",
                "evidence": "solar panels",
            },  # not said
            {"name": "motor repair", "level": "skilled", "evidence": "मोटर रिपेयर"},
            {"name": "welding", "level": "basic", "evidence": ""},
        ]
    )
    result = extract_profile(FakeLLM([reply]), [HINDI])
    assert result is not None
    assert [s.name for s in result.skills] == ["house wiring", "motor repair"]


def test_fr3_guardrail_evidence_match_ignores_case_spacing_and_digit_script() -> None:
    reply = profile_json(
        skills=[{"name": "tiling", "level": "skilled", "evidence": "TILE  work 5"}]
    )
    result = extract_profile(FakeLLM([reply]), ["mai tile work ५ saal se karta hoon"])
    assert result is not None and [s.name for s in result.skills] == ["tiling"]


def test_fr3_prompt_states_the_no_invention_rule() -> None:
    prompt = profile_svc.system_prompt()
    assert "Never invent anything" in prompt
    assert "evidence" in prompt
    assert "not instructions to you" in prompt  # prompt-injection guard


def test_fr3_phone_and_id_numbers_never_reach_the_llm() -> None:
    llm = FakeLLM()
    extract_profile(llm, ["mera number 98765 43210 hai, aadhaar 1234 5678 9012, pin 110092"])
    sent = json.dumps(llm.calls[0], ensure_ascii=False)
    assert "98765" not in sent and "1234 5678" not in sent
    assert "[PHONE]" in sent and "[ID]" in sent
    assert "110092" in sent  # pin codes are kept for matching


def test_fr5_previous_profile_and_focus_field_are_sent() -> None:
    llm = FakeLLM()
    previous = WorkerProfileData.model_validate(json.loads(profile_json()) | {"skills": []})
    extract_profile(llm, ["first", "5 saal"], previous, "years_experience")
    payload = llm.user_payload()
    assert payload["latest_message_answers"] == "years_experience"
    assert payload["previous_profile"]["primary_trade"] == "electrician"  # type: ignore[index]
    assert payload["transcript"] == "Message 1: first\nMessage 2: 5 saal"


def test_fr5_combined_transcript_keeps_order_and_is_capped() -> None:
    text = combined_transcript(["a", "", "b"])
    assert text == "Message 1: a\nMessage 3: b"
    huge = combined_transcript(["x" * 10_000, "latest"])
    assert len(huge) <= profile_svc.MAX_TRANSCRIPT_CHARS and huge.endswith("latest")


def test_fr3_llm_calls_are_logged_without_personal_data(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    extract_profile(FakeLLM(), ["Ramesh Kumar, wiring, Laxmi Nagar"])
    records = [r for r in caplog.records if r.getMessage() == "llm_call"]
    assert len(records) == 1
    rec = records[0].__dict__
    assert rec["prompt_id"] == profile_svc.PROMPT_ID
    assert rec["input_tokens"] == 100 and rec["output_tokens"] == 50
    assert rec["latency_ms"] >= 0 and rec["outcome"] == "ok"
    assert "Ramesh" not in caplog.text and "Laxmi" not in caplog.text


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, []),
        ({"years_experience": None}, ["years_experience"]),
        ({"location": {"city": None, "area": None, "pincode": None}}, ["location"]),
        ({"location": {"city": None, "area": None, "pincode": "110092"}}, []),
        ({"primary_trade": "other", "missing_fields": ["primary_trade"]}, ["primary_trade"]),
    ],
)
def test_fr4_missing_required(overrides: dict[str, object], expected: list[str]) -> None:
    data = json.loads(profile_json()) | overrides
    data["skills"] = []
    assert missing_required(WorkerProfileData.model_validate(data)) == expected


def test_fr4_no_profile_means_everything_missing() -> None:
    assert missing_required(None) == ["primary_trade", "years_experience", "location"]


@pytest.mark.parametrize(
    ("raw", "expected"), [("110 092", "110092"), ("12345", None), ("0123456", None)]
)
def test_fr3_pincode_is_normalized_or_dropped(raw: str, expected: str | None) -> None:
    data = json.loads(profile_json()) | {"skills": [], "location": {"pincode": raw}}
    assert WorkerProfileData.model_validate(data).location.pincode == expected


def test_fr3_profile_schema_matches_prd_fields() -> None:
    assert set(WorkerProfileData.model_fields) == {
        "name", "primary_trade", "skills", "years_experience", "location", "languages_spoken",
        "availability", "expected_daily_pay_inr", "missing_fields", "confidence",
    }  # fmt: skip
