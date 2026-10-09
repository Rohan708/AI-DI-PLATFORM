"""LLM providers behind one small interface, chosen by ``AIDE_LLM_PROVIDER``.

Each provider takes a system prompt and a user prompt and returns the model's JSON text.
Adding a provider (Claude, OpenAI, a local model) means one class here plus one entry in
``llm_from_settings``. Tests use a fake client: no real LLM calls in tests.

Gemini is called through its REST API with the standard library (no SDK dependency).
The API key travels in a header, never in the URL, so it can't leak into logs.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from ai_data_engineer.config import Settings

GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Low temperature: proposals should be the model's best reading of the schema, not creative.
PROPOSAL_TEMPERATURE = 0.2


class LLMError(RuntimeError):
    """The LLM could not be reached or gave no usable answer."""


class LLMNotConfiguredError(LookupError):
    """AI features are off or missing a key (shown to the user as one line)."""


@dataclass(frozen=True)
class LLMReply:
    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMClient(Protocol):
    provider: str
    model: str

    def complete_json(self, system: str, prompt: str) -> LLMReply: ...


class GeminiClient:
    provider = "gemini"

    def __init__(self, api_key: str, model: str = GEMINI_DEFAULT_MODEL, timeout: float = 120.0):
        if not api_key:
            raise LLMNotConfiguredError("AIDE_GEMINI_API_KEY is empty")
        self._key = api_key
        self.model = model
        self._timeout = timeout

    def complete_json(self, system: str, prompt: str) -> LLMReply:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": PROPOSAL_TEMPERATURE,
            },
        }
        request = urllib.request.Request(  # noqa: S310 - fixed https endpoint
            GEMINI_ENDPOINT.format(model=self.model),
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": self._key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise LLMError(f"Gemini returned HTTP {exc.code}: {_error_message(exc)}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Gemini could not be reached: {exc}") from exc
        return parse_gemini_reply(payload, self.model)


def parse_gemini_reply(payload: dict[str, Any], model: str) -> LLMReply:
    candidates = payload.get("candidates") or []
    if not candidates:
        reason = (payload.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise LLMError(f"Gemini gave no answer ({reason})")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(str(p.get("text", "")) for p in parts)
    if not text.strip():
        reason = candidates[0].get("finishReason", "empty")
        raise LLMError(f"Gemini gave an empty answer ({reason})")
    usage = payload.get("usageMetadata") or {}
    return LLMReply(
        text=text,
        provider="gemini",
        model=str(payload.get("modelVersion") or model),
        input_tokens=usage.get("promptTokenCount"),
        output_tokens=usage.get("candidatesTokenCount"),
    )


def _error_message(exc: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(exc.read())
        return str(detail.get("error", {}).get("message", exc.reason))[:300]
    except (ValueError, OSError):
        return str(exc.reason)


def llm_from_settings(settings: Settings) -> LLMClient:
    model = settings.llm_model or None
    if settings.llm_provider == "gemini":
        if settings.gemini_api_key is None:
            raise LLMNotConfiguredError(
                "AIDE_LLM_PROVIDER is gemini but AIDE_GEMINI_API_KEY is not set (add it to .env)"
            )
        return GeminiClient(
            settings.gemini_api_key.get_secret_value(),
            model or GEMINI_DEFAULT_MODEL,
            settings.llm_timeout_seconds,
        )
    raise LLMNotConfiguredError(
        "AI features are off: set AIDE_LLM_PROVIDER (e.g. gemini) and its API key in .env"
    )
