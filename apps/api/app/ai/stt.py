"""Speech to text (PRD: AI pipeline, step 1; FR-2).

Audio from WhatsApp (OGG/Opus) is converted with ffmpeg to 16 kHz mono WAV,
then sent to a `SpeechToText` provider. IBM Watson is the provider: its Indian
models cover Hindi and Indian English only (hi-IN_Telephony, en-IN_Telephony),
so other languages are reported as unsupported and the worker is asked to type.
"""

import io
import logging
import subprocess  # fixed argv, no shell
import wave
from dataclasses import dataclass
from functools import lru_cache
from typing import ClassVar, Protocol

import httpx

from app.ai.retry import with_retries
from app.config import get_settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # mono, 16-bit


class STTError(Exception):
    """Audio could not be transcribed."""


class TransientSTTError(STTError):
    """Provider rate limit, 5xx or network error: worth retrying."""


class AudioTooLong(STTError):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"audio is {seconds:.1f}s")
        self.seconds = seconds


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    duration_s: float
    confidence: float | None = None


class SpeechToText(Protocol):
    provider: str

    def supports(self, language: str) -> bool: ...

    def transcribe(self, wav: bytes, language: str) -> tuple[str, float | None]:
        """Return (transcript, confidence) for 16 kHz mono WAV audio."""
        ...


def transcode_to_wav(
    audio: bytes, max_seconds: int, timeout_s: float = 30.0
) -> tuple[bytes, float]:
    """Decode any audio ffmpeg understands to 16 kHz mono 16-bit WAV.

    Decodes at most max_seconds + 1 so oversized notes are cheap to reject.
    Returns (wav_bytes, duration_seconds); raises AudioTooLong past the limit.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", "pipe:0", "-t", str(max_seconds + 1),
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
    ]  # fmt: skip
    try:
        proc = subprocess.run(  # noqa: S603  # fixed argv, input is bytes not a path
            cmd, input=audio, capture_output=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as exc:
        raise STTError("ffmpeg is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise STTError("ffmpeg timed out") from exc
    if proc.returncode != 0 or not proc.stdout:
        raise STTError("audio could not be decoded")
    duration = len(proc.stdout) / BYTES_PER_SECOND
    if duration > max_seconds:
        raise AudioTooLong(duration)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(proc.stdout)
    return buf.getvalue(), duration


class WatsonSTT:
    provider = "watson"
    MODELS: ClassVar[dict[str, str]] = {"hi": "hi-IN_Telephony", "en": "en-IN_Telephony"}

    def __init__(
        self, api_key: str, url: str, timeout_s: float = 30.0, http: httpx.Client | None = None
    ) -> None:
        self._key = api_key
        self._url = url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout_s)

    def supports(self, language: str) -> bool:
        return language in self.MODELS

    def transcribe(self, wav: bytes, language: str) -> tuple[str, float | None]:
        if not (self._key and self._url):
            raise STTError("watson_stt_api_key and watson_stt_url must be set")
        model = self.MODELS.get(language)
        if model is None:
            raise STTError(f"no Watson model for language {language!r}")
        try:
            resp = self._http.post(
                f"{self._url}/v1/recognize",
                params={"model": model, "smart_formatting": "true"},
                content=wav,
                headers={"Content-Type": "audio/wav"},
                auth=("apikey", self._key),
            )
        except httpx.TransportError as exc:
            raise TransientSTTError(f"watson transport error: {type(exc).__name__}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientSTTError(f"watson HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise STTError(f"watson HTTP {resp.status_code}")
        parts, confidences = [], []
        for result in resp.json().get("results", []):
            alternatives = result.get("alternatives") or []
            if alternatives:
                parts.append(alternatives[0].get("transcript", "").strip())
                if (c := alternatives[0].get("confidence")) is not None:
                    confidences.append(float(c))
        text = " ".join(p for p in parts if p)
        confidence = sum(confidences) / len(confidences) if confidences else None
        return text, confidence


@lru_cache
def get_stt() -> SpeechToText:
    s = get_settings()
    return WatsonSTT(s.watson_stt_api_key, s.watson_stt_url, timeout_s=s.llm_timeout_s)


def transcribe_audio(stt: SpeechToText, audio: bytes, language: str) -> Transcript:
    """ffmpeg -> STT with retries (PRD: max 3 attempts on transient failures)."""
    s = get_settings()
    wav, duration = transcode_to_wav(audio, s.voice_max_seconds, s.ffmpeg_timeout_s)
    text, confidence = with_retries(
        lambda: stt.transcribe(wav, language),
        retry_on=(TransientSTTError,),
        label=f"stt:{stt.provider}",
    )
    logger.info(
        "stt_call",
        extra={"provider": stt.provider, "language": language, "duration_s": round(duration, 1),
               "chars": len(text), "confidence": confidence},
    )  # fmt: skip
    return Transcript(text, language, duration, confidence)
