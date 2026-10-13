"""FR-2 / FR-3: provider clients (HTTP mocked), redaction, retries, ffmpeg, media download."""

import json
import math
import shutil
import struct
import subprocess
import wave
from io import BytesIO
from typing import Any

import httpx
import pytest

from app.ai.llm import LLMError, OpenAIClient, TransientLLMError, WatsonxClient, logged_call
from app.ai.redact import redact
from app.ai.retry import with_retries
from app.ai.stt import (
    AudioTooLong,
    STTError,
    TransientSTTError,
    WatsonSTT,
    transcode_to_wav,
)
from app.schemas.profile import LLM_JSON_SCHEMA
from app.services.whatsapp import MediaError, TransientMediaError, TwilioProvider

MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "data"}]


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- redaction -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("call 98765 43210", "call [PHONE]"),
        ("+91-9876543210 pe", "[PHONE] pe"),
        ("09876543210", "[PHONE]"),
        ("नंबर ९८७६५४३२१० है", "नंबर [PHONE] है"),
        ("aadhaar 1234 5678 9012", "aadhaar [ID]"),
        ("PAN ABCDE1234F", "PAN [ID]"),
        ("voter id ABC1234567", "voter id [ID]"),
        ("account 123456789012345", "account [NUMBER]"),
    ],
)
def test_fr3_redacts_phone_and_id_numbers(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text", ["pin 110092", "500 - 700 rupees", "10 saal", "since 2015", "8 ghante, 26 din"]
)
def test_fr3_keeps_numbers_matching_needs(text: str) -> None:
    assert redact(text) == text


# --- retries ---------------------------------------------------------------------


def test_p3_retries_transient_errors_up_to_three_attempts() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise TransientLLMError("503")
        return "ok"

    assert with_retries(flaky, retry_on=(TransientLLMError,), sleep=sleeps.append) == "ok"
    assert len(calls) == 3 and len(sleeps) == 2
    assert sleeps[1] > sleeps[0] * 0.9  # backoff grows (with jitter)


def test_p3_gives_up_after_three_attempts() -> None:
    calls: list[int] = []

    def down() -> str:
        calls.append(1)
        raise TransientLLMError("503")

    with pytest.raises(TransientLLMError):
        with_retries(down, retry_on=(TransientLLMError,), sleep=lambda _: None)
    assert len(calls) == 3


def test_p3_permanent_errors_are_not_retried() -> None:
    calls: list[int] = []

    def bad() -> str:
        calls.append(1)
        raise LLMError("400")

    with pytest.raises(LLMError):
        with_retries(bad, retry_on=(TransientLLMError,), sleep=lambda _: None)
    assert len(calls) == 1


# --- OpenAI (Responses API, strict JSON schema) -----------------------------------


def _openai_ok(text: str = '{"a": 1}') -> dict[str, Any]:
    return {
        "status": "completed",
        "output": [
            {"type": "reasoning", "content": []},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ],
        "usage": {"input_tokens": 321, "output_tokens": 45},
    }


def test_fr3_openai_request_uses_responses_api_with_strict_schema() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_openai_ok())

    client = OpenAIClient("sk-test", "gpt-6-luna", reasoning_effort="none", http=_client(handler))
    resp = client.complete_json(MESSAGES, LLM_JSON_SCHEMA, "worker_profile")
    assert (resp.text, resp.input_tokens, resp.output_tokens) == ('{"a": 1}', 321, 45)
    (req,) = seen
    assert str(req.url) == "https://api.openai.com/v1/responses"
    assert req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["model"] == "gpt-6-luna"
    assert body["input"] == MESSAGES
    assert body["reasoning"] == {"effort": "none"}
    fmt = body["text"]["format"]
    assert fmt == {
        "type": "json_schema",
        "name": "worker_profile",
        "schema": LLM_JSON_SCHEMA,
        "strict": True,
    }


def test_fr3_openai_omits_reasoning_when_unset() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert "reasoning" not in json.loads(req.content)
        return httpx.Response(200, json=_openai_ok())

    OpenAIClient("k", "m", http=_client(handler)).complete_json(MESSAGES, {}, "x")


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, LLMError),
        ({"output": [{"content": [{"type": "refusal", "refusal": "no"}]}]}, LLMError),
    ],
)
def test_fr3_openai_incomplete_or_refused(payload: dict[str, Any], error: type[Exception]) -> None:
    client = OpenAIClient("k", "m", http=_client(lambda _: httpx.Response(200, json=payload)))
    with pytest.raises(error):
        client.complete_json(MESSAGES, {}, "x")


@pytest.mark.parametrize(
    ("status", "error"), [(429, TransientLLMError), (503, TransientLLMError), (401, LLMError)]
)
def test_fr3_openai_http_errors_classified(status: int, error: type[Exception]) -> None:
    client = OpenAIClient("k", "m", http=_client(lambda _: httpx.Response(status)))
    with pytest.raises(error) as exc:
        client.complete_json(MESSAGES, {}, "x")
    assert type(exc.value) is error


def test_fr3_openai_without_key_fails_fast() -> None:
    with pytest.raises(LLMError, match="openai_api_key"):
        OpenAIClient("", "m", http=_client(lambda _: httpx.Response(200))).complete_json(
            MESSAGES, {}, "x"
        )


def test_fr3_strict_schema_lists_every_property_as_required() -> None:
    def check(schema: dict[str, Any]) -> None:
        if schema.get("type") == "object" or "object" in schema.get("type", []):
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for sub in schema["properties"].values():
                check(sub)
        if "items" in schema:
            check(schema["items"])
        assert not {"minimum", "maximum", "minLength", "pattern"} & set(schema)

    check(LLM_JSON_SCHEMA)


# --- watsonx.ai (Granite) -------------------------------------------------------------


def test_fr3_watsonx_gets_iam_token_once_and_calls_text_chat() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.host == "iam.cloud.ibm.com":
            return httpx.Response(200, json={"access_token": "iam-tok", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": '{"b": 2}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    client = WatsonxClient("ibm-key", "proj-1", "ibm/granite-4-h-small", http=_client(handler))
    for _ in range(2):
        resp = client.complete_json(MESSAGES, {}, "x")
    assert (resp.text, resp.input_tokens, resp.output_tokens) == ('{"b": 2}', 10, 5)
    iam = [r for r in seen if r.url.host == "iam.cloud.ibm.com"]
    assert len(iam) == 1  # token cached
    assert b"grant_type=urn%3Aibm%3Aparams%3Aoauth%3Agrant-type%3Aapikey" in iam[0].content
    chat = seen[-1]
    assert chat.url.path == "/ml/v1/text/chat"
    assert chat.url.params["version"] == "2024-03-14"
    assert chat.headers["authorization"] == "Bearer iam-tok"
    body = json.loads(chat.content)
    assert body["model_id"] == "ibm/granite-4-h-small"
    assert body["project_id"] == "proj-1"
    assert body["messages"] == MESSAGES
    assert body["response_format"] == {"type": "json_object"}


def test_fr3_watsonx_refreshes_expired_token() -> None:
    now = [0.0]
    tokens: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "iam.cloud.ibm.com":
            tokens.append("t")
            return httpx.Response(200, json={"access_token": f"t{len(tokens)}", "expires_in": 3600})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = WatsonxClient("k", "p", "m", http=_client(handler), clock=lambda: now[0])
    client.complete_json(MESSAGES, {}, "x")
    now[0] = 3400.0  # past expiry minus 5-minute margin
    client.complete_json(MESSAGES, {}, "x")
    assert len(tokens) == 2


def test_fr3_watsonx_requires_credentials() -> None:
    with pytest.raises(LLMError, match="watsonx"):
        WatsonxClient("", "", "m", http=_client(lambda _: httpx.Response(200))).complete_json(
            MESSAGES, {}, "x"
        )


def test_fr3_logged_call_retries_transient_failures() -> None:
    class Flaky:
        provider, model, n = "flaky", "m", 0

        def complete_json(self, *a: Any) -> Any:
            from app.ai.llm import LLMResponse

            self.n += 1
            if self.n < 2:
                raise TransientLLMError("503")
            return LLMResponse("{}")

    flaky = Flaky()
    assert logged_call(flaky, "p", MESSAGES, {}, "x").text == "{}"
    assert flaky.n == 2


# --- Watson STT ---------------------------------------------------------------------


def test_fr2_watson_supports_only_hindi_and_indian_english() -> None:
    stt = WatsonSTT("k", "https://stt.example")
    assert [c for c in ("hi", "en", "ta", "bn", "mr") if stt.supports(c)] == ["hi", "en"]


def test_fr2_watson_request_and_transcript_parsing() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "alternatives": [{"transcript": "मैं बिजली का ", "confidence": 0.8}],
                        "final": True,
                    },
                    {
                        "alternatives": [{"transcript": "काम करता हूँ ", "confidence": 0.6}],
                        "final": True,
                    },
                ]
            },
        )

    stt = WatsonSTT(
        "watson-key",
        "https://api.eu-gb.speech-to-text.watson.cloud.ibm.com/instances/x/",
        http=_client(handler),
    )
    text, confidence = stt.transcribe(b"RIFF...", "hi")
    assert text == "मैं बिजली का काम करता हूँ"
    assert confidence == pytest.approx(0.7)
    (req,) = seen
    assert req.url.path == "/instances/x/v1/recognize"
    assert req.url.params["model"] == "hi-IN_Telephony"
    assert req.headers["content-type"] == "audio/wav"
    assert req.headers["authorization"].startswith("Basic ")


@pytest.mark.parametrize(
    ("status", "error"), [(503, TransientSTTError), (429, TransientSTTError), (400, STTError)]
)
def test_fr2_watson_http_errors_classified(status: int, error: type[Exception]) -> None:
    stt = WatsonSTT("k", "https://stt.example", http=_client(lambda _: httpx.Response(status)))
    with pytest.raises(error) as exc:
        stt.transcribe(b"x", "en")
    assert type(exc.value) is error


def test_fr2_watson_rejects_unsupported_language_and_missing_credentials() -> None:
    with pytest.raises(STTError):
        WatsonSTT("k", "https://stt.example").transcribe(b"x", "ta")
    with pytest.raises(STTError):
        WatsonSTT("", "").transcribe(b"x", "hi")


# --- ffmpeg (real) --------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _tone_ogg(seconds: float) -> bytes:
    """A real WhatsApp-style OGG/Opus clip, encoded by ffmpeg from a sine wave."""
    rate = 48_000
    frames = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(int(rate * seconds))
    )
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-f", "ogg", "pipe:1"],  # noqa: S607
        input=buf.getvalue(), capture_output=True, check=True,
    )  # fmt: skip
    return proc.stdout


@needs_ffmpeg
def test_fr2_ogg_opus_is_converted_to_16khz_mono_wav() -> None:
    wav, duration = transcode_to_wav(_tone_ogg(2.0), max_seconds=120)
    assert duration == pytest.approx(2.0, abs=0.1)
    with wave.open(BytesIO(wav)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16_000, 1, 2)


@needs_ffmpeg
def test_fr2_voice_notes_over_the_limit_are_rejected() -> None:
    with pytest.raises(AudioTooLong) as exc:
        transcode_to_wav(_tone_ogg(3.5), max_seconds=2)
    assert exc.value.seconds > 2


@needs_ffmpeg
def test_fr2_garbage_audio_is_an_stt_error() -> None:
    with pytest.raises(STTError):
        transcode_to_wav(b"definitely not audio", max_seconds=120)


def test_fr2_missing_ffmpeg_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_ffmpeg(*a: Any, **k: Any) -> Any:
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(subprocess, "run", no_ffmpeg)
    with pytest.raises(STTError, match="ffmpeg"):
        transcode_to_wav(b"x", 120)


# --- Twilio media download ------------------------------------------------------------

MEDIA_URL = "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages/MM1/Media/ME1"


def _twilio(handler: Any) -> TwilioProvider:
    return TwilioProvider("AC1", "tok", "whatsapp:+1", http=_client(handler))


def test_fr2_media_download_uses_account_auth_and_follows_redirect() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.twilio.com":
            assert req.headers["authorization"].startswith("Basic ")
            return httpx.Response(307, headers={"location": "https://media.twiliocdn.com/x.ogg"})
        return httpx.Response(200, content=b"OGGDATA")

    assert _twilio(handler).fetch_media(MEDIA_URL, 1000) == b"OGGDATA"


@pytest.mark.parametrize(
    "url",
    ["http://api.twilio.com/x", "https://evil.example/x", "https://169.254.169.254/latest"],
)
def test_fr2_media_download_only_from_twilio(url: str) -> None:
    with pytest.raises(MediaError):
        _twilio(lambda _: httpx.Response(200, content=b"x")).fetch_media(url, 1000)


def test_fr2_media_download_size_is_capped() -> None:
    with pytest.raises(MediaError, match="too large"):
        _twilio(lambda _: httpx.Response(200, content=b"x" * 5000)).fetch_media(MEDIA_URL, 1000)


@pytest.mark.parametrize(("status", "error"), [(503, TransientMediaError), (404, MediaError)])
def test_fr2_media_download_errors(status: int, error: type[Exception]) -> None:
    with pytest.raises(error) as exc:
        _twilio(lambda _: httpx.Response(status)).fetch_media(MEDIA_URL, 1000)
    assert type(exc.value) is error


def test_fr2_inbound_voice_note_is_parsed() -> None:
    msg = _twilio(lambda _: httpx.Response(200)).parse_inbound(
        [
            ("MessageSid", "SM1"),
            ("From", "whatsapp:+919876543210"),
            ("NumMedia", "1"),
            ("MediaUrl0", MEDIA_URL),
            ("MediaContentType0", "audio/ogg"),
        ]
    )
    assert msg.is_audio and msg.kind == "audio" and msg.media_url == MEDIA_URL
