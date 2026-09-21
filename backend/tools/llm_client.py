# backend/tools/llm_client.py
#
# LLM API client — the ONLY place that calls OpenAI or Anthropic directly.
#
# WHY A WRAPPER?
# The same reason redis_client.py exists. Without this:
#   - SecurityAgent imports openai directly -> tightly coupled to OpenAI
#   - Swapping to Anthropic means touching every agent file
#   - Rate limit handling is duplicated across 4 agents
#   - Token counting happens inconsistently
#
# With this wrapper:
#   - Agents call: await llm_client.call(model="gpt-4o-mini", messages=[...])
#   - They never import openai or anthropic directly
#   - Rate limit + retry logic lives in ONE place
#   - Token counting is automatic and centralized
#
# PROVIDERS SUPPORTED:
#   OpenAI  -> gpt-4o, gpt-4o-mini, gpt-3.5-turbo
#   Anthropic -> claude-3-5-sonnet-20241022, claude-3-haiku-20240307
#
# STRUCTURED OUTPUT:
# Both providers support structured output (JSON mode).
# OpenAI:    response_format={"type": "json_object"} in the API call
# Anthropic: system prompt instruction + xml tags in response
# This client normalizes both into: returns parsed dict | raises LLMOutputError.
#
# RETRY STRATEGY (from Stability Patterns wiki):
# "Every external call is a potential stab-in-the-back."
# We retry on:
#   - 429 RateLimitError  -> wait for retry-after header, then retry (up to 3x)
#   - 500/502/503          -> exponential backoff, up to 3 retries
# We do NOT retry on:
#   - 400 BadRequest (prompt too long) -> caller needs to truncate
#   - 401 Unauthorized (bad key)       -> config error, no point retrying
#   - Content policy violation          -> log and return empty findings
#
# TOKEN COUNTING:
# Every call returns a LLMResponse with input_tokens and output_tokens.
# The model router uses this for cost attribution per agent (Phase 10 full tracing).

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.core.exceptions import AgentError
from backend.security.masking import mask_sensitive
logger = logging.getLogger(__name__)


class MistralRateLimitError(AgentError):
    """Raised when Mistral returns a 429 Too Many Requests / Rate Limit error."""
    pass


# ---------------------------------------------------------------------------
# Phase 16 — fire-and-forget cost log writer.
#
# Called after each successful LLM call. Reads the active workflow context
# (set by base_agent.analyze) so we never have to thread a workflow_id arg
# through every retry path. Failures here are swallowed inside
# record_llm_call so a DB hiccup cannot break the review pipeline.
# ---------------------------------------------------------------------------
async def _persist_call_log(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_seconds: float,
    is_valid_json: bool,
) -> None:
    try:
        # Lazy imports to avoid circular import at module load time
        # (economics imports models, models import Base, Base depends on settings).
        from backend.economics import record_llm_call
        from backend.observability.workflow_context import get_workflow_context
        ctx = get_workflow_context()
        await record_llm_call(
            workflow_id=ctx.workflow_id,
            agent_type=ctx.agent_type,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_seconds * 1000.0,
            is_valid_json=is_valid_json,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("llm_call_log_helper_failed | error=%s", exc)


# ---------------------------------------------------------------------------
# Token cost table (USD per 1000 tokens)
# ---------------------------------------------------------------------------
_TOKEN_COSTS: dict[str, dict[str, float]] = {
    # Mistral models (primary)
    "mistral-small-latest":  {"input": 0.0001,  "output": 0.0003},
    "mistral-medium-latest": {"input": 0.0004,  "output": 0.0012},
    "mistral-large-latest":  {"input": 0.002,   "output": 0.006},
    # Google GenAI models (fallback)
    "gemini-2.5-flash":      {"input": 0.00015, "output": 0.0006},
    "gemini-1.5-flash":      {"input": 0.000075,"output": 0.0003},
    "gemini-1.5-pro":        {"input": 0.00125, "output": 0.005},
    # Legacy models (backward compatibility)
    "gpt-4o":                {"input": 0.005,   "output": 0.015},
    "gpt-4o-mini":           {"input": 0.00015, "output": 0.0006},
    "gpt-3.5-turbo":         {"input": 0.0005,  "output": 0.0015},
    "claude-3-5-sonnet-20241022": {"input": 0.003,  "output": 0.015},
    "claude-3-haiku-20240307":    {"input": 0.00025,"output": 0.00125},
}


@dataclass
class LLMResponse:
    """
    The structured response from one LLM API call.

    WHY A DATACLASS AND NOT PYDANTIC?
    LLMResponse is internal to the tools layer — it never leaves the codebase
    (not stored, not serialized to JSON, not returned via API).
    Dataclass is lighter than Pydantic for pure in-process data.

    FIELDS:
    """
    # The parsed content from the LLM.
    # For JSON mode calls: a parsed dict.
    # For text mode calls: a string.
    content: dict | str

    # How many tokens the prompt used (input side).
    # Billed by the provider. Used for cost tracking.
    input_tokens: int

    # How many tokens the response used (output side).
    # Usually much smaller than input for structured output calls.
    output_tokens: int

    # Which model actually served this request.
    # May differ from requested model if provider falls back.
    model_used: str

    # Wall-clock seconds for this API call.
    # Used by Phase 10 (Observability) to identify slow models.
    latency_seconds: float

    # Estimated cost in USD for this call.
    # Computed from token counts + cost table above.
    estimated_cost_usd: float = 0.0

    # Whether the response content was valid JSON (for JSON mode calls).
    # False means output guardrail caught malformed output.
    is_valid_json: bool = True


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Estimates the cost of one LLM API call in USD.

    Returns 0.0 if the model is not in our cost table (unknown models).
    We never crash because of missing cost data — we just log it.

    HOW THE MATH WORKS:
    Cost table stores: price per 1000 tokens.
    Actual tokens used: input_tokens + output_tokens.
    Cost = (input_tokens / 1000) * input_price + (output_tokens / 1000) * output_price

    EXAMPLE:
    gpt-4o-mini, 1000 input tokens, 200 output tokens:
    = (1000/1000) * 0.00015 + (200/1000) * 0.0006
    = 0.00015 + 0.00012
    = $0.00027 per call
    """
    costs = _TOKEN_COSTS.get(model, {})
    if not costs:
        return 0.0
    input_cost  = (input_tokens  / 1000) * costs.get("input",  0.0)
    output_cost = (output_tokens / 1000) * costs.get("output", 0.0)
    return round(input_cost + output_cost, 8)


class LLMClient:
    """
    Async LLM client supporting Mistral (primary) and Google GenAI Gemini (fallback).

    LIFECYCLE:
    This client is stateless — it creates async HTTP client connections per call.
    Thread-safe and safe to use concurrently across multiple agent tasks.
    API keys are read from Settings at call time.

    ORCHESTRATION:
    Primary model: mistral-small-latest via call_mistral().
    Fallback model: gemini-2.5-flash via call_google() when Mistral returns HTTP 429 RateLimit.
    Agents call call_with_fallback() which orchestrates this transparently.
    """

    # Maximum number of retries on transient failures (5xx, network drops)
    MAX_RETRIES = 3

    # Base delay for exponential backoff in seconds.
    BASE_RETRY_DELAY = 1.0

    async def call_mistral(
        self,
        model: str = "mistral-small-latest",
        messages: list[dict[str, str]] | None = None,
        system_prompt: str = "",
        json_mode: bool = True,
        max_tokens: int = 2048,
        api_key: str | None = None,
    ) -> LLMResponse:
        """
        Makes an API call to Mistral AI.
        If rate limited (HTTP 429), raises MistralRateLimitError to trigger Gemini fallback.
        """
        from backend.config import get_settings
        cfg = get_settings()
        key = api_key or cfg.mistral_api_key

        messages = messages or []
        masked_system = mask_sensitive(system_prompt) if system_prompt else ""
        masked_messages = [
            {**m, "content": mask_sensitive(m.get("content", ""))}
            for m in messages
        ]
        full_messages = [{"role": "system", "content": masked_system}] + masked_messages

        payload: dict[str, Any] = {
            "model": model,
            "messages": full_messages,
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            start = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        "https://api.mistral.ai/v1/chat/completions",
                        json=payload,
                        headers=headers,
                    )

                if resp.status_code == 429:
                    raise MistralRateLimitError(
                        f"Mistral rate limit (429): {resp.text}"
                    )

                if resp.status_code and 400 <= resp.status_code < 500:
                    raise AgentError(
                        f"Mistral API client error {resp.status_code}: {resp.text}",
                        agent_name=model,
                    )

                resp.raise_for_status()
                data = resp.json()

                latency = time.monotonic() - start
                raw_content = data["choices"][0]["message"]["content"] or "{}"
                usage = data.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)

                is_valid_json = True
                try:
                    parsed = json.loads(raw_content)
                except json.JSONDecodeError:
                    parsed = _try_extract_json(raw_content)
                    is_valid_json = bool(parsed)

                cost = _compute_cost(model, input_tokens, output_tokens)
                logger.info(
                    "mistral_call | model=%s input_tokens=%d output_tokens=%d "
                    "latency=%.2fs cost=$%.6f",
                    model, input_tokens, output_tokens, latency, cost,
                )

                await _persist_call_log(
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    latency_seconds=latency,
                    is_valid_json=is_valid_json,
                )

                return LLMResponse(
                    content=parsed,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    model_used=model,
                    latency_seconds=round(latency, 3),
                    estimated_cost_usd=cost,
                    is_valid_json=is_valid_json,
                )

            except MistralRateLimitError:
                raise

            except Exception as e:
                last_error = e
                delay = self.BASE_RETRY_DELAY * (2 ** attempt)
                logger.warning(
                    "mistral_call_error | model=%s attempt=%d/%d error=%s",
                    model, attempt + 1, self.MAX_RETRIES, str(e),
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(delay)

        raise AgentError(
            f"Mistral call failed after {self.MAX_RETRIES + 1} attempts: {last_error}",
            agent_name=model,
        ) from last_error

    async def call_google(
        self,
        model: str = "gemini-2.5-flash",
        messages: list[dict[str, str]] | None = None,
        system_prompt: str = "",
        json_mode: bool = True,
        max_tokens: int = 2048,
        api_key: str | None = None,
    ) -> LLMResponse:
        """
        Makes an API call to Google GenAI (Gemini).
        Used as primary fallback when Mistral is rate limited.
        """
        from backend.config import get_settings
        cfg = get_settings()
        key = api_key or cfg.google_api_key

        messages = messages or []
        masked_system = mask_sensitive(system_prompt) if system_prompt else ""
        contents = []
        for m in messages:
            role = "user" if m.get("role") in ("user", "system") else "model"
            content_text = mask_sensitive(m.get("content", ""))
            contents.append({"role": role, "parts": [{"text": content_text}]})

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": max_tokens,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        if masked_system:
            payload["systemInstruction"] = {"parts": [{"text": masked_system}]}
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        headers = {"Content-Type": "application/json"}

        last_error: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            start = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(url, json=payload, headers=headers)

                if resp.status_code and 400 <= resp.status_code < 500:
                    raise AgentError(
                        f"Google GenAI client error {resp.status_code}: {resp.text}",
                        agent_name=model,
                    )

                resp.raise_for_status()
                data = resp.json()

                latency = time.monotonic() - start
                raw_content = "{}"
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    non_thought = [p.get("text", "") for p in parts if not p.get("thought", False) and "text" in p]
                    if non_thought:
                        raw_content = non_thought[-1]
                    elif parts:
                        raw_content = parts[-1].get("text", "{}")

                usage = data.get("usageMetadata", {})
                input_tokens = usage.get("promptTokenCount", 0)
                output_tokens = usage.get("candidatesTokenCount", 0)

                is_valid_json = True
                try:
                    parsed = json.loads(raw_content)
                except json.JSONDecodeError:
                    parsed = _try_extract_json(raw_content)
                    is_valid_json = bool(parsed)

                cost = _compute_cost(model, input_tokens, output_tokens)
                logger.info(
                    "google_gemini_call | model=%s input_tokens=%d output_tokens=%d "
                    "latency=%.2fs cost=$%.6f",
                    model, input_tokens, output_tokens, latency, cost,
                )

                await _persist_call_log(
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    latency_seconds=latency,
                    is_valid_json=is_valid_json,
                )

                return LLMResponse(
                    content=parsed,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    model_used=model,
                    latency_seconds=round(latency, 3),
                    estimated_cost_usd=cost,
                    is_valid_json=is_valid_json,
                )

            except Exception as e:
                last_error = e
                delay = self.BASE_RETRY_DELAY * (2 ** attempt)
                logger.warning(
                    "google_call_error | model=%s attempt=%d/%d error=%s",
                    model, attempt + 1, self.MAX_RETRIES, str(e),
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(delay)

        raise AgentError(
            f"Google Gemini call failed after {self.MAX_RETRIES + 1} attempts: {last_error}",
            agent_name=model,
        ) from last_error

    async def call_with_fallback(
        self,
        model: str = "mistral-small-latest",
        messages: list[dict[str, str]] | None = None,
        system_prompt: str = "",
        json_mode: bool = True,
        max_tokens: int = 2048,
        mistral_api_key: str | None = None,
        google_api_key: str | None = None,
        fallback_model: str = "gemini-2.5-flash",
    ) -> LLMResponse:
        """
        Primary LLM caller:
        Tries Mistral (mistral-small-latest) as primary.
        If Mistral returns a rate limit (HTTP 429), automatically retries with
        Google Gen AI (gemini-2.5-flash).
        """
        messages = messages or []
        from backend.config import get_settings
        cfg = get_settings()
        m_key = mistral_api_key or cfg.mistral_api_key
        g_key = google_api_key or cfg.google_api_key

        if not m_key and g_key:
            return await self.call_google(
                model=fallback_model,
                messages=messages,
                system_prompt=system_prompt,
                json_mode=json_mode,
                max_tokens=max_tokens,
                api_key=g_key,
            )

        try:
            return await self.call_mistral(
                model=model,
                messages=messages,
                system_prompt=system_prompt,
                json_mode=json_mode,
                max_tokens=max_tokens,
                api_key=mistral_api_key,
            )
        except MistralRateLimitError as rle:
            logger.warning(
                "mistral_rate_limited | primary=%s | falling back to Google Gemini fallback=%s | reason=%s",
                model, fallback_model, str(rle),
            )
            return await self.call_google(
                model=fallback_model,
                messages=messages,
                system_prompt=system_prompt,
                json_mode=json_mode,
                max_tokens=max_tokens,
                api_key=google_api_key,
            )

    async def call_openai(self, *args: Any, **kwargs: Any) -> LLMResponse:
        """Legacy compatibility alias routed through call_with_fallback."""
        model = kwargs.get("model", "mistral-small-latest")
        if "gpt" in model or "openai" in model:
            model = "mistral-small-latest"
        kwargs["model"] = model
        return await self.call_with_fallback(**kwargs)

    async def call_anthropic(self, *args: Any, **kwargs: Any) -> LLMResponse:
        """Legacy compatibility alias routed through call_with_fallback."""
        model = kwargs.get("model", "mistral-small-latest")
        if "claude" in model or "anthropic" in model:
            model = "mistral-small-latest"
        kwargs["model"] = model
        return await self.call_with_fallback(**kwargs)


def _try_extract_json(text: str) -> dict | list:
    """
    Output guardrail: tries to extract valid JSON from partially malformed LLM output.

    LLMs sometimes wrap their JSON in markdown code blocks:
      ```json
      { "findings": [...] }
      ```
    Or add a short preamble:
      "Here are the findings:\n{ ... }"

    This function tries three strategies in order:
      1. Strip markdown code fences, parse
      2. Find the first { or [ and parse from there
      3. Give up and return empty dict

    WIKI PRINCIPLE (Production-Hardening.md):
    "Redact before block" — extract partial signal before giving up entirely.
    An empty dict triggers low confidence -> HITL, which is better than crashing.
    """
    if not text:
        return {}

    # Strategy 1: strip markdown code fences
    cleaned = text.strip()
    for fence in ["```json", "```JSON", "```"]:
        if cleaned.startswith(fence):
            cleaned = cleaned[len(fence):]
            break
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Strategy 2: find the first { or [ character and parse from there
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = cleaned.find(start_char)
        if start == -1:
            continue
        # Find the last matching closing character
        end = cleaned.rfind(end_char)
        if end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue

    # Strategy 3: give up
    logger.error("json_extraction_failed | could not extract JSON from LLM output")
    return {}


# Module-level singleton.
# Stateless — safe to share across all concurrent agent calls.
llm_client = LLMClient()
