"""Reading an LLM's JSON answer: plain JSON, or JSON wrapped in a Markdown code fence."""

import json
from typing import Any

from ai_data_engineer.reasoning.llm import LLMError


def parse_json_reply(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        cleaned = cleaned.rsplit("```", 1)[0]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMError(f"the LLM's answer is not valid JSON ({exc.msg})") from exc


def items_of(data: Any, key: str) -> list[dict[str, Any]]:
    """The objects listed under ``key`` (or a bare list); anything else is ignored."""
    if isinstance(data, dict):
        data = data.get(key, [])
    if not isinstance(data, list):
        raise LLMError(f"the LLM's answer has no list of {key}")
    return [item for item in data if isinstance(item, dict)]


def confidence_of(value: object) -> float | None:
    """A 0..1 confidence from an LLM answer, or None if missing or out of range."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 1 else None
