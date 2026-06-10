import re
from pathlib import Path

import yaml
from jinja2 import Environment

from pipeline.types import (
    LegacyRubric,
    LegacyRubricDimension,
    SessionMeta,
    Transcript,
    VisualObservations,
)

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT / "prompts"
RUBRIC_DIR = ROOT / "rubric"


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    m = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
    return text[m.end():] if m else text


def load_prompt(prompt_id: str) -> str:
    """Load a prompt template by id from prompts/<id>.md, with frontmatter stripped."""
    return strip_frontmatter((PROMPT_DIR / f"{prompt_id}.md").read_text())


def load_rubric(path: Path | None = None) -> LegacyRubric:
    """Legacy 5-dimension rubric loader (YAML). Stays around for the archived
    scoring path. New Q&A rubric loader lives in pipeline.rubric."""
    path = path or (RUBRIC_DIR / "rubric_v0_1.yaml")
    return LegacyRubric.model_validate(yaml.safe_load(path.read_text()))


def render_transcript(transcript: Transcript) -> str:
    return "\n".join(f"[{seg.ts_start}] {seg.speaker}: {seg.text}" for seg in transcript.segments)


def render_visual(observations: VisualObservations) -> str:
    return "\n".join(
        f"[{o.ts_start}-{o.ts_end}] {o.description}" for o in observations.observations
    )


def _jinja_env() -> Environment:
    return Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)


def render_score_prompt(
    dimension: LegacyRubricDimension,
    rubric: LegacyRubric,
    session: SessionMeta,
    transcript: Transcript,
    observations: VisualObservations,
    few_shot_examples: list | None = None,
) -> str:
    """Legacy 5-dimension prompt renderer. Stays around for the archived
    scoring path; new Q&A path uses pipeline.rubric.render_prompt()."""
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


def split_system_user(rendered: str) -> tuple[str, str]:
    """Split a rendered prompt at the `# USER` marker into system/user halves."""
    parts = rendered.split("\n# USER\n", 1)
    if len(parts) != 2:
        return rendered, "Score the dimension."
    system_part = parts[0].replace("# SYSTEM\n", "", 1).strip()
    user_part = parts[1].strip()
    return system_part, user_part


def render_vision_prompt(session: SessionMeta) -> str:
    """Render the vision prompt with session context injected."""
    template = _jinja_env().from_string(load_prompt("vision"))
    return template.render(session=session.model_dump(mode="json"))
