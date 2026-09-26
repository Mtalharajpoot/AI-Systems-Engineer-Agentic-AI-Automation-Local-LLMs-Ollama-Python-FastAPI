"""Local LLM (Ollama) used ONLY for messages the rules could not classify.

Reliability pattern:
  1. Ask for JSON that matches a schema (Ollama structured output).
  2. Validate with Pydantic.
  3. If invalid: one retry, showing the model its own validation error.
  4. Still invalid, or Ollama down/slow: raise -> the agent falls back to a HUMAN.
The model never sends messages or dispatches vendors. It only returns a classification.
"""
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from .config import settings


class LLMUnavailable(Exception):
    """Ollama unreachable, timed out, or returned an HTTP error."""


class LLMDisabled(LLMUnavailable):
    """LLM_MODE=off"""


class LLMInvalid(Exception):
    """Model answered, but never produced valid JSON for our schema."""


class LLMResult(BaseModel):
    category: Literal["plumbing", "electrical", "hvac", "locksmith", "general",
                      "fire", "gas", "office", "unknown"]
    urgency: Literal["emergency", "urgent", "routine"]
    confidence: float = Field(ge=0, le=1)
    needs_human: bool
    reason: str = Field(max_length=300)


SYSTEM_PROMPT = """You triage tenant messages for a property manager.
Return ONLY JSON that matches the schema.
urgency: emergency = danger to people or serious property damage right now (flooding, fire, gas, electrical hazard).
urgent = loss of an essential service or security today (no heat, no power, lockout, sewage).
routine = can wait for business hours.
category: plumbing, electrical, hvac, locksmith, general, fire, gas, office (rent/lease/questions, not maintenance), unknown.
If you are unsure, set needs_human=true and give a low confidence. Never invent facts."""

_post = httpx.post   # indirection so tests can replace the network call


def classify(message: str, summary: str, facts: str) -> LLMResult:
    mode = settings.llm_mode
    if mode == "off":
        raise LLMDisabled("LLM_MODE=off")
    if mode == "mock":
        return _mock(message)
    return _ollama(message, summary, facts)


def _ollama(message: str, summary: str, facts: str) -> LLMResult:
    # Token-efficient prompt: unit facts + short rolling summary + the ONE new message.
    user = f"Unit: {facts}\nTicket summary so far: {summary or 'none'}\nNew message: {message}"
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "format": LLMResult.model_json_schema(),
        "options": {"temperature": 0, "num_predict": 200},
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user}],
    }
    for _attempt in range(2):
        try:
            resp = _post(f"{settings.ollama_url}/api/chat", json=payload, timeout=settings.llm_timeout)
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc
        try:
            return LLMResult.model_validate_json(content)
        except ValidationError as exc:
            # feed the error back once so the model can fix its own output
            payload["messages"] += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"That JSON was invalid: {str(exc)[:300]}. Return corrected JSON only."},
            ]
    raise LLMInvalid("model did not return valid JSON after retry")


def _mock(message: str) -> LLMResult:
    """Deterministic fake model for tests and for running the demo without Ollama."""
    text = message.lower()
    if "smell" in text or "hot" in text or "warm" in text:
        return LLMResult(category="electrical", urgency="urgent", confidence=0.8,
                         needs_human=False, reason="mock: possible electrical/heat issue")
    return LLMResult(category="general", urgency="routine", confidence=0.7,
                     needs_human=False, reason="mock: nothing alarming")
