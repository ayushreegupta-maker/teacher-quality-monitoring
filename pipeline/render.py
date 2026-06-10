"""
Generic prompt loading + Jinja-templating helpers.

After the dead-code sweep on 2026-06-10, this module just exposes:
  strip_frontmatter, load_prompt, _jinja_env, split_system_user,
  render_vision_prompt.

The 4 legacy 5-dimension-rubric functions (load_rubric, render_transcript,
render_visual, render_score_prompt) moved to
`pipeline/_archive/render_legacy.py`. See DECISIONS.md for the rationale.
"""
import re
from pathlib import Path

from jinja2 import Environment

from pipeline.types import SessionMeta

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT / "prompts"


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    m = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
    return text[m.end():] if m else text


def load_prompt(prompt_id: str) -> str:
    """Load a prompt template by id from prompts/<id>.md, with frontmatter stripped."""
    return strip_frontmatter((PROMPT_DIR / f"{prompt_id}.md").read_text())


def _jinja_env() -> Environment:
    return Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)


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
