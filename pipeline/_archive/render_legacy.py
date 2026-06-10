"""
Archived chunks of pipeline.render — 2026-06-10.

Extracted from `pipeline/render.py` once they had zero live callers:

  load_rubric(path)                       — YAML loader for the legacy
                                            5-dimension rubric files now
                                            under `_archive/rubric/`.
  render_transcript(transcript)           — only used by render_score_prompt
  render_visual(observations)             — only used by render_score_prompt
  render_score_prompt(dimension, ...)     — old per-dimension scoring prompt
                                            template renderer; replaced by
                                            pipeline.rubric.render_prompt()

If we ever revive the old YAML rubric path, import these from here
instead of pipeline.render. They depend on the archived
`pipeline/_archive/legacy_rubric_types.py` for type definitions.
"""
from pathlib import Path

import yaml
from jinja2 import Environment

from pipeline._archive.legacy_rubric_types import (
    LegacyRubric,
    LegacyRubricDimension,
)
from pipeline.render import _jinja_env, load_prompt  # still in the live module
from pipeline.types import SessionMeta, Transcript, VisualObservations

_ROOT = Path(__file__).resolve().parent.parent.parent
_RUBRIC_DIR = _ROOT / "_archive" / "rubric"


def load_rubric(path: Path | None = None) -> LegacyRubric:
    """Legacy 5-dimension rubric loader (YAML). Pre-Q&A architecture."""
    path = path or (_RUBRIC_DIR / "rubric_v0_1.yaml")
    return LegacyRubric.model_validate(yaml.safe_load(path.read_text()))


def render_transcript(transcript: Transcript) -> str:
    return "\n".join(
        f"[{seg.ts_start}] {seg.speaker}: {seg.text}"
        for seg in transcript.segments
    )


def render_visual(observations: VisualObservations) -> str:
    return "\n".join(
        f"[{o.ts_start}-{o.ts_end}] {o.description}"
        for o in observations.observations
    )


def render_score_prompt(
    dimension: LegacyRubricDimension,
    rubric: LegacyRubric,
    session: SessionMeta,
    transcript: Transcript,
    observations: VisualObservations,
    few_shot_examples: list | None = None,
) -> str:
    """Legacy 5-dimension prompt renderer."""
    template = _jinja_env().from_string(load_prompt("score_dimension"))
    return template.render(
        dimension=dimension.model_dump(),
        rubric_version=rubric.version,
        rubric_scoring_scale=rubric.scoring_scale,
        anti_bias_rules=rubric.anti_bias_rules,
        session=session.model_dump(mode="json"),
        transcript_rendered=render_transcript(transcript),
        visual_observations_rendered=render_visual(observations),
        few_shot_examples=few_shot_examples or [],
    )
