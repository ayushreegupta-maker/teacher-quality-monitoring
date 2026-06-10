"""One-shot Shape B experiment: hand Claude the Gemini-extracted evidence and
ask it to answer the same 31 art-rubric questions.

Goal is comparative — Claude's answers will land next to the existing
Gemini-direct `4_rubric_answers.json` so we can read the two side by side.

Reads (from the run dir):
    2_boundaries.json
    4_phases.json
    4_explanations.json
    4_rubric_prompt.txt           ◄── source of the 31-question block
    6_transcript_deduped_v2.json  ◄── cleaned transcript
    6_observations.json

Writes (into the run dir):
    7_claude_prompt.txt           ◄── exact prompt sent
    7_claude_raw.txt              ◄── raw Claude response
    7_claude_answers.json         ◄── parsed answers

Usage:
    .venv/bin/python scripts/score_art_with_claude.py \
        data/art_rubric_runs/2026-06-04_122724
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.llm import LLMAdapter, extract_json

MODEL = "claude-opus-4-7"
MAX_TOKENS = 12000


def slice_questions_block(prompt_text: str) -> str:
    """Extract the 31-question block from the existing Gemini rubric prompt.

    The block begins at the first line that looks like a section header
    (e.g. '=== Content Knowledge ===' or 'Q1 ...') and runs to EOF.
    We anchor on the first occurrence of 'Q1 ' since the header layout
    above it may move between prompt versions.
    """
    lines = prompt_text.splitlines()
    start_idx = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("Q1 ")),
        None,
    )
    if start_idx is None:
        raise SystemExit("could not find Q1 in prompt; check 4_rubric_prompt.txt")
    # Walk backwards through preceding non-empty lines to include the group/section
    # headers that frame Q1 (e.g. '=== Compliance ===', '[Group] Was the class ...').
    while start_idx > 0 and lines[start_idx - 1].strip() != "":
        start_idx -= 1
    return "\n".join(lines[start_idx:]).strip()


SYSTEM = """You are a senior teaching-quality evaluator for Openhouse preschool.

You are evaluating an art-class video session, but you cannot see the video directly. Instead, another model (Gemini) has watched the full session and extracted structured evidence for you:

  - boundary detection (when the class actually starts and ends within the recording)
  - phase enumeration (ordered list of phases: setup, warm_up, art_games, etc., with start/end timestamps and what happened)
  - per-phase explanations (the teacher's verbal explanations and tone)
  - a cleaned transcript (speaker-attributed utterances; loops have been collapsed)
  - visual observations (discrete observed events with timestamps)

You will answer 31 rubric questions strictly from this evidence. Treat the evidence as the source of truth, but stay skeptical — if a question asks about something the evidence simply does not cover, return "INSUFFICIENT INFORMATION" rather than guessing.

Rules:

1. EVERY answer must cite at least one evidence timestamp in HH:MM:SS (video-relative) format. Wall-clock readings (e.g. "09:18:42") are also fine where the evidence provides them.
2. NEVER invent dialogue, names, or events that aren't in the evidence.
3. For numeric questions ("# of minutes", "# of children"), give a single integer. If a precise number is impossible from the evidence, give your best bounded estimate and say so in the rationale.
4. Speaker labels in the transcript are imperfect (Gemini sometimes mixes TEACHER and STUDENT). If a citation depends on speaker, briefly note the uncertainty in your rationale.
5. Be cautious — Gemini's evidence is noisy. Where the transcript shows obvious loop artifacts (annotations like "(repeated 552 times — likely transcription loop artifact)"), DO NOT treat the repetition as a real classroom event.

Output: ONE JSON object, keyed Q1..Q31. No prose outside the JSON, no markdown fences.

Each value must be an object:
{
  "answer": "<your answer — string, integer, or 'INSUFFICIENT INFORMATION'>",
  "confidence": "high" | "medium" | "low",
  "evidence_timestamps": ["HH:MM:SS", ...],
  "rationale": "<one or two sentences citing the specific evidence>"
}
"""


def main(run_dir: Path) -> None:
    needed = [
        "2_boundaries.json",
        "4_phases.json",
        "4_explanations.json",
        "4_rubric_prompt.txt",
        "6_transcript_deduped_v2.json",
        "6_observations.json",
    ]
    for f in needed:
        if not (run_dir / f).exists():
            sys.exit(f"missing artifact: {run_dir / f}")

    boundaries = json.loads((run_dir / "2_boundaries.json").read_text())
    phases = json.loads((run_dir / "4_phases.json").read_text())
    explanations = json.loads((run_dir / "4_explanations.json").read_text())
    transcript = json.loads((run_dir / "6_transcript_deduped_v2.json").read_text())
    observations = json.loads((run_dir / "6_observations.json").read_text())

    rubric_prompt_text = (run_dir / "4_rubric_prompt.txt").read_text()
    questions_block = slice_questions_block(rubric_prompt_text)

    user_parts = [
        "## EVIDENCE",
        "",
        "### Boundaries",
        "```json",
        json.dumps(boundaries, indent=2),
        "```",
        "",
        "### Phases",
        "```json",
        json.dumps(phases, indent=2),
        "```",
        "",
        "### Per-phase explanations",
        "```json",
        json.dumps(explanations, indent=2),
        "```",
        "",
        "### Visual observations",
        "```json",
        json.dumps(observations, indent=2),
        "```",
        "",
        "### Cleaned transcript",
        "```json",
        json.dumps(transcript, indent=2),
        "```",
        "",
        "## QUESTIONS",
        "",
        questions_block,
        "",
        "## OUTPUT",
        "",
        "Return ONE JSON object keyed Q1..Q31 in the schema described in the system prompt. No prose outside the JSON.",
    ]
    user = "\n".join(user_parts)

    prompt_path = run_dir / "7_claude_prompt.txt"
    prompt_path.write_text(f"# SYSTEM\n\n{SYSTEM}\n\n# USER\n\n{user}\n")
    print(f"wrote prompt ({len(user):,} chars) to {prompt_path}")

    llm = LLMAdapter()
    print(f"calling {MODEL} (max_tokens={MAX_TOKENS}) …")
    raw = llm.call_claude_text(
        system=SYSTEM,
        user=user,
        model_name=MODEL,
        max_tokens=MAX_TOKENS,
        temperature=None,  # Opus 4.7 rejects the `temperature` parameter.
    )

    raw_path = run_dir / "7_claude_raw.txt"
    raw_path.write_text(raw)
    print(f"wrote raw response ({len(raw):,} chars) to {raw_path}")

    try:
        parsed = json.loads(extract_json(raw))
    except Exception as e:
        sys.exit(
            f"FAILED to parse JSON from Claude response: {e!r}\n"
            f"Inspect {raw_path} and re-run."
        )

    out_path = run_dir / "7_claude_answers.json"
    out_path.write_text(json.dumps(parsed, indent=2))
    answered = sum(
        1
        for v in parsed.values()
        if isinstance(v, dict) and str(v.get("answer", "")).upper() != "INSUFFICIENT INFORMATION"
    )
    print(
        f"wrote {len(parsed)} answers to {out_path} "
        f"({answered} answered, {len(parsed) - answered} INSUFFICIENT INFORMATION)"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: .venv/bin/python scripts/score_art_with_claude.py <run_dir>")
    main(Path(sys.argv[1]).resolve())
