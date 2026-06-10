"""
Archived LLMAdapter methods — 2026-06-10.

Methods extracted because they had zero live callers in the new
architecture (Shape A uses call_gemini_video; Shape B uses
call_claude_text; nothing else does):

  LLMAdapter.call_claude_json
  LLMAdapter.call_openai_text
  LLMAdapter.call_openai_json
  LLMAdapter.call_gemini_text
  LLMAdapter.call_gemini_text_json

To restore, swap `LLMAdapter()` for `LLMAdapterLegacy()` at the
instantiation site (scripts/run_rubric.py and friends). The subclass
re-adds all 5 methods. The live `adapters.llm` module is otherwise
unchanged.
"""
from typing import Type

from google.genai import types as gtypes
from pydantic import BaseModel, ValidationError

from adapters.llm import (
    LLMAdapter,
    T,
    extract_json,
    log,
)
from adapters.retry import retry_external
import json


class LLMAdapterLegacy(LLMAdapter):
    """LLMAdapter + the 5 methods archived during the 2026-06-10 dead-code
    sweep. Behaves identically to LLMAdapter for all live call paths."""

    def call_claude_json(
        self,
        system: str,
        user: str,
        schema: Type[T],
        max_attempts: int = 2,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        model_name: str | None = None,
    ) -> T:
        """Call Claude, parse JSON, validate against schema. Retry once with the validation error fed back if it fails."""
        attempt = 0
        last_err: Exception | None = None
        last_raw: str = ""
        current_user = user
        while attempt < max_attempts:
            attempt += 1
            raw = self.call_claude_text(
                system=system, user=current_user, max_tokens=max_tokens,
                temperature=temperature, model_name=model_name,
            )
            last_raw = raw
            cleaned = extract_json(raw)
            try:
                parsed = json.loads(cleaned)
                return schema.model_validate(parsed)
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = e
                log.warning(f"schema validation failed (attempt {attempt}/{max_attempts}): {e}")
                current_user = (
                    user
                    + f"\n\n[your previous response failed validation: {e}]\n"
                    + "Return ONLY the JSON object matching the schema. No prose, no markdown fences."
                )
        raise ValueError(
            f"failed to parse/validate after {max_attempts} attempts. "
            f"last error: {last_err}\nlast raw (truncated): {last_raw[:500]}"
        )

    @retry_external(max_attempts=3)
    def call_openai_text(
        self,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        model_name: str = "gpt-4o",
    ) -> str:
        if self._openai is None:
            raise RuntimeError("OPENAI_API_KEY not set in environment")
        resp = self._openai.chat.completions.create(
            model=model_name, max_completion_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content

    def call_openai_json(
        self,
        system: str,
        user: str,
        schema: Type[T],
        max_attempts: int = 2,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        model_name: str = "gpt-4o",
    ) -> T:
        """Call OpenAI chat completion, parse JSON, validate. Mirrors call_claude_json."""
        if self._openai is None:
            raise RuntimeError("OPENAI_API_KEY not set in environment")
        attempt = 0
        last_err: Exception | None = None
        last_raw: str = ""
        current_user = user + "\n\nReturn ONLY a JSON object matching the requested schema."
        while attempt < max_attempts:
            attempt += 1
            resp = self._openai.chat.completions.create(
                model=model_name, max_completion_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": current_user},
                ],
            )
            raw = resp.choices[0].message.content or ""
            last_raw = raw
            cleaned = extract_json(raw)
            try:
                parsed = json.loads(cleaned)
                return schema.model_validate(parsed)
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = e
                log.warning(f"OpenAI schema validation failed (attempt {attempt}/{max_attempts}): {e}")
                current_user = (
                    user
                    + f"\n\n[your previous response failed validation: {e}]\n"
                    + "Return ONLY the JSON object matching the schema. No prose, no markdown fences."
                )
        raise ValueError(
            f"OpenAI: failed to parse/validate after {max_attempts} attempts. "
            f"last error: {last_err}\nlast raw (truncated): {last_raw[:500]}"
        )

    @retry_external(max_attempts=3)
    def call_gemini_text(
        self,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        thinking_budget: int | None = 0,
        force_json: bool = True,
        model_name: str = "gemini-2.5-pro",
    ) -> str:
        """Text-only Gemini call (no video) for using Gemini as a pure reasoner."""
        if self._gemini is None:
            raise RuntimeError("GOOGLE_API_KEY not set in environment")
        config_kwargs: dict = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if force_json:
            config_kwargs["response_mime_type"] = "application/json"
        if thinking_budget is not None:
            config_kwargs["thinking_config"] = gtypes.ThinkingConfig(thinking_budget=thinking_budget)
        full_prompt = f"{system}\n\n{user}"
        resp = self._gemini.models.generate_content(
            model=model_name, contents=[full_prompt],
            config=gtypes.GenerateContentConfig(**config_kwargs),
        )
        text = resp.text if hasattr(resp, "text") else None
        if not text or not text.strip():
            raise ValueError("empty response from Gemini text call")
        return text.strip()

    def call_gemini_text_json(
        self,
        system: str,
        user: str,
        schema: Type[T],
        max_attempts: int = 2,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        model_name: str = "gemini-2.5-pro",
    ) -> T:
        """Gemini text call with schema validation. Mirrors call_claude_json / call_openai_json."""
        attempt = 0
        last_err: Exception | None = None
        last_raw: str = ""
        current_user = user
        while attempt < max_attempts:
            attempt += 1
            raw = self.call_gemini_text(
                system=system, user=current_user, max_tokens=max_tokens,
                temperature=temperature, model_name=model_name,
            )
            last_raw = raw
            cleaned = extract_json(raw)
            try:
                parsed = json.loads(cleaned)
                return schema.model_validate(parsed)
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = e
                log.warning(f"Gemini text schema validation failed (attempt {attempt}/{max_attempts}): {e}")
                current_user = (
                    user
                    + f"\n\n[your previous response failed validation: {e}]\n"
                    + "Return ONLY the JSON object matching the schema. No prose, no markdown fences."
                )
        raise ValueError(
            f"Gemini text: failed to parse/validate after {max_attempts} attempts. "
            f"last error: {last_err}\nlast raw (truncated): {last_raw[:500]}"
        )
