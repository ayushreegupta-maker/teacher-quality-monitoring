"""
Smoke test for the score_dimension prompt.

Loads the rubric, picks one dimension, renders the prompt against a
synthetic session (transcript + visual observations defined inline below),
and writes the rendered prompt to test_output/.

If ANTHROPIC_API_KEY is set in the environment, also calls Claude and
saves the JSON response. Otherwise prints the rendered prompt path so you
can paste it manually.

Run:
    .venv/bin/python test_render.py                 # default: positive_climate
    .venv/bin/python test_render.py behavior_management
"""

import json
import os
import re
import sys
from pathlib import Path

import yaml
from jinja2 import Environment

ROOT = Path(__file__).parent
RUBRIC_PATH = ROOT / "rubric" / "rubric_v0_1.yaml"
PROMPT_PATH = ROOT / "prompts" / "score_dimension.md"
OUT_DIR = ROOT / "test_output"

# --- Synthetic session (entirely fabricated, no real classroom recorded) ---

SESSION = {
    "session_id": "synthetic_001",
    "recorded_at": "2026-05-15",
    "age_range": "3-5 years",
    "duration_minutes": 30,
    "subject": "general preschool",
}

TRANSCRIPT = [
    ("00:00:05", "TEACHER", "Good morning, friends. Come and find a spot on the carpet. Aarav, I love your bright shirt today."),
    ("00:00:18", "STUDENT_1", "Ms Sara, can I sit next to Maya?"),
    ("00:00:22", "TEACHER", "Of course, Aarav. Maya, would you scoot over a little so Aarav can join you?"),
    ("00:00:31", "STUDENT_2", "Like this?"),
    ("00:00:33", "TEACHER", "Perfect. Thank you. Riya, ready to start? Come on over, I saved your favourite spot."),
    ("00:00:48", "STUDENT_3", "I'm coming."),
    ("00:00:51", "TEACHER", "There we go. Let's all take three big breaths together. In, and out. In, and out. One more. In, and out."),
    ("00:01:18", "TEACHER", "Beautiful. Now, who can tell me what we did at the end of yesterday's circle?"),
    ("00:01:28", "STUDENT_4", "We sang the goodbye song."),
    ("00:01:31", "TEACHER", "Yes, Kabir. And what was special about how we sang it?"),
    ("00:01:36", "STUDENT_4", "We did the hand thing."),
    ("00:01:39", "TEACHER", "The hand thing, exactly. Can you show everyone the hand thing?"),
    ("00:01:48", "TEACHER", "Wonderful. Friends, let's all try Kabir's hand movement together."),
    ("00:02:08", "STUDENT_5", "Ms Sara, my brother taught me a different one."),
    ("00:02:14", "TEACHER", "Oh, would you like to show us yours too, Ananya? We have time."),
    ("00:02:28", "TEACHER", "That's lovely. Friends, we have two hand movements now. Should we try both?"),
    ("00:02:35", "STUDENTS", "Yeah."),
]

VISUAL = [
    ("00:00:00", "00:00:30", "Teacher walks toward circle area, smiling, arms slightly open. Eight children visible, ages approximately 3-5. Teacher kneels at child's level as students gather."),
    ("00:00:30", "00:01:20", "Teacher seated on floor with students in semicircle. Makes eye contact with each child as they arrive. Smiling consistently. Pats one child gently on shoulder."),
    ("00:01:20", "00:02:00", "All students seated on carpet, attentive. Teacher leans toward speaking student. Student gestures with both hands; teacher mirrors the gesture."),
    ("00:02:00", "00:02:40", "Teacher turns body to face Ananya when she speaks. Open posture, nodding. Other students watching attentively. No off-task behaviour visible."),
]


def load_rubric():
    return yaml.safe_load(RUBRIC_PATH.read_text())


def get_dimension(rubric, dim_id):
    for domain in rubric["domains"]:
        for dim in domain["dimensions"]:
            if dim["id"] == dim_id:
                return dim
    raise KeyError(f"Dimension {dim_id} not found in rubric")


def strip_frontmatter(text):
    if not text.startswith("---"):
        return text
    m = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
    return text[m.end():] if m else text


def render_transcript(segments):
    return "\n".join(f"[{ts}] {speaker}: {text}" for ts, speaker, text in segments)


def render_visual(observations):
    return "\n".join(f"[{a}-{b}] {desc}" for a, b, desc in observations)


def render_prompt(dimension, rubric, session, transcript, visual):
    template_text = strip_frontmatter(PROMPT_PATH.read_text())
    env = Environment(autoescape=False, trim_blocks=False, lstrip_blocks=False)
    template = env.from_string(template_text)
    return template.render(
        dimension=dimension,
        rubric_version=rubric["version"],
        anti_bias_rules=rubric["anti_bias_rules"],
        session=session,
        transcript_rendered=render_transcript(transcript),
        visual_observations_rendered=render_visual(visual),
        few_shot_examples=[],
    )


def split_system_user(rendered):
    """Split rendered prompt at the # USER marker into (system, user) parts."""
    parts = rendered.split("\n# USER\n", 1)
    if len(parts) != 2:
        return rendered, "Score the dimension."
    system_part = parts[0].replace("# SYSTEM\n", "", 1).strip()
    user_part = parts[1].strip()
    return system_part, user_part


def call_claude(system_prompt, user_prompt):
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return response.content[0].text


def main():
    dim_id = sys.argv[1] if len(sys.argv) > 1 else "positive_climate"
    rubric = load_rubric()
    dimension = get_dimension(rubric, dim_id)

    rendered = render_prompt(dimension, rubric, SESSION, TRANSCRIPT, VISUAL)

    OUT_DIR.mkdir(exist_ok=True)
    rendered_path = OUT_DIR / f"rendered_{dim_id}.txt"
    rendered_path.write_text(rendered)
    print(f"[ok] rendered prompt -> {rendered_path}  ({len(rendered)} chars)")

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("[i] ANTHROPIC_API_KEY not set; skipping live Claude call.")
        print("    To call Claude: export ANTHROPIC_API_KEY=... and re-run.")
        return

    system_prompt, user_prompt = split_system_user(rendered)
    print(f"[..] calling Claude (system={len(system_prompt)} chars, user={len(user_prompt)} chars)")
    raw = call_claude(system_prompt, user_prompt)

    raw_path = OUT_DIR / f"response_{dim_id}.txt"
    raw_path.write_text(raw)
    print(f"[ok] raw response -> {raw_path}")

    try:
        parsed = json.loads(raw)
        parsed_path = OUT_DIR / f"response_{dim_id}.json"
        parsed_path.write_text(json.dumps(parsed, indent=2))
        print(f"[ok] parsed JSON -> {parsed_path}")
        print(f"     score={parsed.get('score')}  confidence={parsed.get('confidence')}  evidence_count={len(parsed.get('evidence', []))}")
    except json.JSONDecodeError as e:
        print(f"[!] JSON parse failed: {e}")
        print(f"    See {raw_path} for raw output.")


if __name__ == "__main__":
    main()
