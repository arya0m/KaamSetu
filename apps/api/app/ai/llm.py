"""LLM clients behind one interface (PRD: AI pipeline, step 2).

- OpenAIClient: Responses API with strict JSON-schema structured output (default).
- WatsonxClient: IBM Granite on watsonx.ai text chat. Built to the PRD default but
  not yet verified against the live API (no credits); covered by the smoke suite.

Every call is logged with provider, model, prompt id, sizes, latency and token
counts, never the prompt or response text (they contain personal data).
"""

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import httpx

from app.ai.retry import with_retries
from app.config import get_settings

logger = logging.getLogger(__name__)

Message = dict[str, str]


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMError(Exception):
    """The LLM call failed and should not be retried."""


class TransientLLMError(LLMError):
    """Rate limit, timeout, provider 5xx: worth retrying."""


class LLMClient(Protocol):
    provider: str
    model: str

    def complete_json(
        self, messages: list[Message], schema: dict[str, Any], schema_name: str
    ) -> LLMResponse: ...


def _raise_for_status(resp: httpx.Response, provider: str) -> None:
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TransientLLMError(f"{provider} HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise LLMError(f"{provider} HTTP {resp.status_code}")


def _post(http: httpx.Client, provider: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        resp = http.post(url, **kwargs)
    except httpx.TransportError as exc:
        raise TransientLLMError(f"{provider} transport error: {type(exc).__name__}") from exc
    _raise_for_status(resp, provider)
    return resp


class OpenAIClient:
    provider = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        reasoning_effort: str = "",
        max_output_tokens: int = 1500,
        timeout_s: float = 30.0,
        http: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._effort = reasoning_effort
        self._max_tokens = max_output_tokens
        self._http = http or httpx.Client(timeout=timeout_s)

    def complete_json(
        self, messages: list[Message], schema: dict[str, Any], schema_name: str
    ) -> LLMResponse:
        if not self._key:
            raise LLMError("openai_api_key is not set")
        body: dict[str, Any] = {
            "model": self.model,
            "input": messages,
            "max_output_tokens": self._max_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                }
            },
        }
        if self._effort:
            body["reasoning"] = {"effort": self._effort}
        resp = _post(
            self._http,
            "openai",
            f"{self._base}/responses",
            json=body,
            headers={"Authorization": f"Bearer {self._key}"},
        )
        data = resp.json()
        if data.get("status") == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason")
            raise LLMError(f"openai response incomplete: {reason}")
        text = ""
        for item in data.get("output", []):
            for part in item.get("content") or []:
                if part.get("type") == "refusal":
                    raise LLMError("openai refused the request")
                if part.get("type") == "output_text":
                    text += part.get("text", "")
        usage = data.get("usage") or {}
        return LLMResponse(text, usage.get("input_tokens"), usage.get("output_tokens"))


class WatsonxClient:
    provider = "watsonx"

    def __init__(
        self,
        api_key: str,
        project_id: str,
        model: str,
        url: str = "https://us-south.ml.cloud.ibm.com",
        api_version: str = "2024-03-14",
        iam_url: str = "https://iam.cloud.ibm.com/identity/token",
        max_output_tokens: int = 1500,
        timeout_s: float = 30.0,
        http: httpx.Client | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.model = model
        self._key = api_key
        self._project = project_id
        self._url = url.rstrip("/")
        self._version = api_version
        self._iam_url = iam_url
        self._max_tokens = max_output_tokens
        self._http = http or httpx.Client(timeout=timeout_s)
        self._clock = clock
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock = threading.Lock()

    def _bearer(self) -> str:
        """IAM tokens live about 60 minutes; refresh 5 minutes early."""
        with self._lock:
            if self._token and self._clock() < self._token_expires:
                return self._token
            resp = _post(
                self._http,
                "ibm-iam",
                self._iam_url,
                data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": self._key},
                headers={"Accept": "application/json"},
            )
            data = resp.json()
            self._token = str(data["access_token"])
            self._token_expires = self._clock() + max(60, int(data.get("expires_in", 3600)) - 300)
            return self._token

    def complete_json(
        self, messages: list[Message], schema: dict[str, Any], schema_name: str
    ) -> LLMResponse:
        if not (self._key and self._project):
            raise LLMError("watsonx_api_key and watsonx_project_id must be set")
        resp = _post(
            self._http,
            "watsonx",
            f"{self._url}/ml/v1/text/chat",
            params={"version": self._version},
            json={
                "model_id": self.model,
                "project_id": self._project,
                "messages": messages,
                "max_tokens": self._max_tokens,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            headers={"Authorization": f"Bearer {self._bearer()}"},
        )
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("watsonx returned no choices")
        usage = data.get("usage") or {}
        return LLMResponse(
            choices[0].get("message", {}).get("content") or "",
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )


@lru_cache
def get_llm() -> LLMClient:
    s = get_settings()
    if s.llm_provider == "watsonx":
        return WatsonxClient(
            api_key=s.watsonx_api_key,
            project_id=s.watsonx_project_id,
            model=s.watsonx_model_id,
            url=s.watsonx_url,
            api_version=s.watsonx_api_version,
            iam_url=s.ibm_iam_url,
            max_output_tokens=s.llm_max_output_tokens,
            timeout_s=s.llm_timeout_s,
        )
    return OpenAIClient(
        api_key=s.openai_api_key,
        model=s.openai_model,
        base_url=s.openai_base_url,
        reasoning_effort=s.openai_reasoning_effort,
        max_output_tokens=s.llm_max_output_tokens,
        timeout_s=s.llm_timeout_s,
    )


def logged_call(
    client: LLMClient,
    prompt_id: str,
    messages: list[Message],
    schema: dict[str, Any],
    schema_name: str,
) -> LLMResponse:
    """Call the LLM with retries; log metadata only (PRD: no personal data in logs)."""
    prompt_chars = sum(len(m["content"]) for m in messages)
    started = time.perf_counter()
    outcome = "ok"
    resp: LLMResponse | None = None
    try:
        resp = with_retries(
            lambda: client.complete_json(messages, schema, schema_name),
            retry_on=(TransientLLMError,),
            label=f"llm:{client.provider}",
        )
        return resp
    except LLMError as exc:
        outcome = type(exc).__name__
        raise
    finally:
        logger.info(
            "llm_call",
            extra={
                "provider": client.provider,
                "model": client.model,
                "prompt_id": prompt_id,
                "prompt_sha256": hashlib.sha256(messages[0]["content"].encode()).hexdigest()[:16],
                "prompt_chars": prompt_chars,
                "response_chars": len(resp.text) if resp else 0,
                "input_tokens": resp.input_tokens if resp else None,
                "output_tokens": resp.output_tokens if resp else None,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "outcome": outcome,
            },
        )
