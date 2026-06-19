import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Type, TypeVar

import anthropic
import openai
from dotenv import load_dotenv
from google import genai
from google.genai import types as gtypes
from json_repair import repair_json
from pydantic import BaseModel, ValidationError

from adapters.retry import retry_external

# Load environment variables from .env at the repo root, if present.
# `override=True` means .env wins over shell-exported values — this is what we
# want for project secrets, because shell exports in `.zshrc` etc. can go stale
# (key rotated, revoked, copied wrong) without the user remembering to update
# them. The .env file is the project's source of truth; if the user has no
# .env, the shell environment still applies as a fallback.
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env", override=True)

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

DEFAULT_SCORING_MODEL = "claude-sonnet-4-6"
DEFAULT_VISION_MODEL = "gemini-2.5-flash"


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def extract_json(text: str) -> str:
    """Pull a JSON object out of a model response, stripping markdown fences and surrounding prose."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s)
        s = s.strip()
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        return s
    obj_match = re.search(r"\{[\s\S]*\}", s)
    if obj_match:
        return obj_match.group(0).strip()
    return s


def parse_json_lenient(text: str) -> dict | list:
    """Parse JSON; on failure, try json_repair to recover partial/truncated output.

    Useful when an LLM response is truncated mid-output — json_repair will
    close unbalanced brackets/quotes and return whatever valid prefix it can.
    """
    cleaned = extract_json(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning(f"json.loads failed ({e}); falling back to json_repair")
        repaired = repair_json(cleaned, return_objects=True)
        if repaired in ({}, [], "", None):
            raise ValueError(f"json_repair recovered nothing from {len(cleaned)}-char response") from e
        return repaired


class LLMAdapter:
    def __init__(self, scoring_model: str | None = None, vision_model: str | None = None):
        self.scoring_model = scoring_model or os.getenv("SCORING_MODEL", DEFAULT_SCORING_MODEL)
        self.vision_model = vision_model or os.getenv("VISION_MODEL", DEFAULT_VISION_MODEL)

        # Anthropic client picks up ANTHROPIC_API_KEY from env automatically
        self._anthropic = anthropic.Anthropic()

        # Gemini client (new google-genai SDK); defer error until call time
        google_key = os.getenv("GOOGLE_API_KEY")
        self._gemini: genai.Client | None = genai.Client(api_key=google_key) if google_key else None

        # OpenAI client (used by model-comparison runner for GPT-4o reasoning);
        # defer error until call time so the adapter is usable without an
        # OpenAI key for callers that don't need it.
        openai_key = os.getenv("OPENAI_API_KEY")
        self._openai: openai.OpenAI | None = openai.OpenAI(api_key=openai_key) if openai_key else None

    @retry_external(max_attempts=3)
    def call_claude_text(
        self,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float | None = 0.0,
        model_name: str | None = None,
    ) -> str:
        # Some newer Claude models (e.g. Opus 4.7) reject `temperature`
        # outright. Pass `temperature=None` to omit it from the request.
        kwargs: dict = dict(
            model=model_name or self.scoring_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = self._anthropic.messages.create(**kwargs)
        return resp.content[0].text

    # call_claude_json was here; archived to
    # adapters/_archive/llm_legacy_methods.py on 2026-06-10 (zero live callers).

    @retry_external(max_attempts=4, min_wait=3.0, max_wait=60.0)
    def upload_video(self, path: Path) -> object:
        """Upload a local video to Gemini Files API; block until processing completes.

        Retries on transient upload failures (HTTP 503, "Upload has already been
        terminated", network drops). Each retry starts a fresh resumable upload
        session — Google orphans the half-uploaded files but they expire in 48h.
        Necessary at any concurrency > 1: the Files API gateway can return 503
        mid-upload when multiple large videos upload simultaneously, and the
        SDK does not auto-retry resumable upload chunks.
        """
        if self._gemini is None:
            raise RuntimeError("GOOGLE_API_KEY not set in environment")
        log.info(f"uploading video to Gemini Files API: {path}")
        file = self._gemini.files.upload(file=str(path))
        while file.state.name == "PROCESSING":
            time.sleep(3)
            file = self._gemini.files.get(name=file.name)
        if file.state.name == "FAILED":
            raise RuntimeError(f"Gemini file processing failed for {path}")
        log.info(f"video uploaded: {file.name} state={file.state.name}")
        return file

    @retry_external(max_attempts=3)
    def call_gemini_video(
        self,
        prompt: str,
        video_file: object,
        temperature: float = 0.0,
        max_output_tokens: int = 65536,
        force_json: bool = True,
        thinking_budget: int | None = 0,
        start_seconds: int | None = None,
        end_seconds: int | None = None,
        fps: float | None = None,
        media_resolution: str | None = None,
        model_name: str | None = None,
    ) -> str:
        """Call Gemini with a video. Defaults:
          - temperature=0.0 for deterministic output. Caller can override
            (e.g. set to 0.3 if creative variation is wanted) but the default
            is 0 because the typical use here is structured extraction
            (timestamps, observations, transcript) where determinism matters
            more than variation. Setting to 0 fixed an issue where the same
            Balloon dance video returned `last_child=00:10:00` on one run and
            `last_child=00:31:59` on another.
          - thinking_budget=0 disables Gemini 2.5 thinking tokens (which otherwise eat the output budget).
            Set to None to use the model default, or to a positive int to allocate a specific budget.
          - force_json sets response_mime_type=application/json for stricter output.
          - start_seconds/end_seconds: if either is set, the call uses video_metadata offsets to
            analyse only a portion of the uploaded video. The model emits timestamps relative
            to the clip start (00:00:00 of the chunk); the caller is responsible for shifting
            them back to absolute time.
          - fps: if set, tells Gemini to sample the video at this rate (frames/sec) when
            tokenizing. Use this to fit long videos under the 1M input-token limit. Note:
            Gemini DOES NOT honour the source video's encoded fps — only this parameter
            controls Gemini's sampling rate.
        """
        if self._gemini is None:
            raise RuntimeError("GOOGLE_API_KEY not set in environment")
        config_kwargs: dict = {
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        }
        if force_json:
            config_kwargs["response_mime_type"] = "application/json"
        if thinking_budget is not None:
            config_kwargs["thinking_config"] = gtypes.ThinkingConfig(thinking_budget=thinking_budget)
        if media_resolution is not None:
            mr_key = f"MEDIA_RESOLUTION_{media_resolution.upper()}"
            config_kwargs["media_resolution"] = gtypes.MediaResolution[mr_key]
            log.info(f"call_gemini_video: media_resolution={mr_key} (~66/258/516 tok/frame for LOW/MED/HIGH)")

        # The video_metadata path is triggered if ANY of start/end/fps is set.
        needs_video_metadata = (
            start_seconds is not None
            or end_seconds is not None
            or fps is not None
        )
        if needs_video_metadata:
            file_data = gtypes.FileData(
                file_uri=video_file.uri,
                mime_type=getattr(video_file, "mime_type", "video/mp4"),
            )
            vm_kwargs: dict = {
                "start_offset": f"{start_seconds or 0}s",
                "end_offset": (f"{end_seconds}s" if end_seconds is not None else None),
            }
            if fps is not None:
                vm_kwargs["fps"] = fps
            video_metadata = gtypes.VideoMetadata(**vm_kwargs)
            log.info(
                f"call_gemini_video: video_metadata path "
                f"(start={vm_kwargs['start_offset']} end={vm_kwargs['end_offset']} fps={fps})"
            )
            contents = [
                gtypes.Part(file_data=file_data, video_metadata=video_metadata),
                prompt,
            ]
        else:
            log.info("call_gemini_video: default path (no video_metadata, Gemini picks fps)")
            contents = [video_file, prompt]

        resp = self._gemini.models.generate_content(
            model=model_name or self.vision_model,
            contents=contents,
            config=gtypes.GenerateContentConfig(**config_kwargs),
        )

        # Log token usage so we can see where the output budget went
        try:
            u = resp.usage_metadata
            log.info(
                f"Gemini usage: prompt={u.prompt_token_count} "
                f"thoughts={getattr(u, 'thoughts_token_count', 0)} "
                f"output={u.candidates_token_count} "
                f"total={u.total_token_count}"
            )
        except Exception:
            pass

        finish_reason = None
        try:
            finish_reason = resp.candidates[0].finish_reason
        except Exception:
            pass

        text = resp.text if hasattr(resp, "text") else None
        if finish_reason and "MAX_TOKENS" in str(finish_reason).upper():
            log.warning(
                f"Gemini finish_reason={finish_reason}; "
                f"max_output_tokens={max_output_tokens} was exhausted — response is truncated"
            )
        if not text or not text.strip():
            raise ValueError(f"empty response from Gemini (finish_reason={finish_reason})")
        return text.strip()

    # call_openai_text, call_openai_json, call_gemini_text,
    # call_gemini_text_json archived to adapters/_archive/llm_legacy_methods.py
    # on 2026-06-10 (zero live callers in the new architecture).
