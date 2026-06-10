"""
Q&A rubric loader + prompt renderer.

The rubric workbook (`~/Downloads/Teacher Quality Monitoring (1).xlsx`) has
one tab per subject: Art, Public Speaking, Robotics. Each tab is a flat
table of questions with one row per question and these columns:

  A. (unlabelled)         — section name, sparsely populated; carry-forward
                            until the next non-empty value. The four
                            distinct section values are
                            Environment / Content Knowledge / Facilitation / Warmth.
  B. "Criteria"           — group label, also carry-forward.
  C. "What AI needs to    — the actual question text. ONE row = ONE question.
       observe"
  D. "Input required"     — optional input reference (present in Art tab only).
  E. "Analysis"           — Visual / Audio / Visual + Audio tag. Present in
                            Art tab only; default-filled for the others
                            (PS + Robotics tabs are at v0 and don't carry
                            this column yet — see PLAN.md §2).

This module exposes ONE entry point — `load_rubric(workbook_path, subject)` —
that returns a fully typed `Rubric` (Pydantic; defined in pipeline.types).
Prompt rendering and scoring land in this module in step 8 of the migration.
"""

from __future__ import annotations

import logging
from pathlib import Path

import openpyxl

from adapters.llm import LLMAdapter, parse_json_lenient, prompt_hash
from pipeline.types import (
    Rubric,
    RubricAnswer,
    RubricAnswerSet,
    RubricQuestion,
    RubricSection,
)

log = logging.getLogger(__name__)

# Subject → workbook sheet name. The rubric workbook uses human-readable
# tab names, but the rest of the pipeline uses snake_case subject tokens
# everywhere else (cctv_cameras.xlsx, data/raw/<subject>/, etc.).
_SUBJECT_TO_SHEET = {
    "art": "Art",
    "public_speaking": "Public Speaking",
    "robotics": "Robotics",
}

# Default analysis tag for any question row that doesn't carry one (PS and
# Robotics tabs in the current workbook). Matches the most permissive option
# in the Art tab so the rubric prompt isn't artificially narrowed.
DEFAULT_ANALYSIS_TAG = "Visual + Audio"


def load_rubric(
    workbook_path: Path,
    subject: str,
    default_analysis_tag: str = DEFAULT_ANALYSIS_TAG,
) -> Rubric:
    """Load one subject's tab from the rubric workbook into a typed Rubric.

    Behaviour:
      - Iterates rows from row 2 onward.
      - Col A (section) and col B (criteria) are sparsely populated; the
        loader forward-fills from the most recent non-empty value so every
        question carries the section + criteria it belongs to.
      - A question row is any row whose col C (observe_text) is non-empty.
      - Question ids are assigned in encounter order: "Q1", "Q2", ...
      - Sections are emitted in encounter order (first time a new section
        name appears, a new RubricSection is opened).
      - Missing col D (input_ref) → None.
      - Missing col E (analysis_tag) → `default_analysis_tag` (PS+Robotics).

    Raises:
      FileNotFoundError if `workbook_path` doesn't exist.
      ValueError if `subject` doesn't map to a sheet in the workbook.
      ValueError if the sheet has no question rows.
    """
    workbook_path = Path(workbook_path)
    if not workbook_path.exists():
        raise FileNotFoundError(f"rubric workbook not found: {workbook_path}")

    sheet_name = _SUBJECT_TO_SHEET.get(subject)
    if sheet_name is None:
        raise ValueError(
            f"unknown subject {subject!r} — "
            f"expected one of {sorted(_SUBJECT_TO_SHEET)}"
        )

    wb = openpyxl.load_workbook(workbook_path, data_only=True)
    if sheet_name not in wb.sheetnames:
        raise ValueError(
            f"sheet {sheet_name!r} not in workbook (have: {wb.sheetnames})"
        )
    ws = wb[sheet_name]

    sections: list[RubricSection] = []
    # Maintain an open section we keep appending questions to until the
    # section name in col A changes.
    open_section: RubricSection | None = None
    current_section: str | None = None
    current_criteria: str | None = None
    q_counter = 0

    for row in ws.iter_rows(min_row=2, values_only=True):
        # Defensive: the sheet may have fewer than 5 columns (PS + Robotics)
        col_a = row[0] if len(row) > 0 else None
        col_b = row[1] if len(row) > 1 else None
        col_c = row[2] if len(row) > 2 else None
        col_d = row[3] if len(row) > 3 else None
        col_e = row[4] if len(row) > 4 else None

        # Carry-forward section + criteria from the most recent non-empty value
        if col_a is not None and str(col_a).strip():
            current_section = str(col_a).strip()
        if col_b is not None and str(col_b).strip():
            current_criteria = str(col_b).strip()

        # Skip rows that don't carry a question
        if col_c is None or not str(col_c).strip():
            continue

        if current_section is None:
            log.warning(
                f"[{subject}] question row with no section above it at "
                f"row {q_counter + 2}, skipping: {str(col_c)[:60]}"
            )
            continue

        # Open a new section when the section name changes
        if open_section is None or open_section.name != current_section:
            open_section = RubricSection(name=current_section, questions=[])
            sections.append(open_section)

        q_counter += 1
        question = RubricQuestion(
            id=f"Q{q_counter}",
            section=current_section,
            criteria=current_criteria,
            observe_text=str(col_c).strip(),
            input_ref=str(col_d).strip() if col_d and str(col_d).strip() else None,
            analysis_tag=(
                str(col_e).strip() if col_e and str(col_e).strip()
                else default_analysis_tag
            ),
        )
        open_section.questions.append(question)

    if q_counter == 0:
        raise ValueError(
            f"sheet {sheet_name!r} contained no question rows "
            "(no non-empty 'What AI needs to observe' cells)"
        )

    rubric = Rubric(
        subject=subject,
        source_path=str(workbook_path.resolve()),
        sections=sections,
    )
    log.info(
        f"[{subject}] loaded rubric: {q_counter} question(s) across "
        f"{len(sections)} section(s) from {workbook_path.name}"
    )
    return rubric


# ─── Prompt rendering ─────────────────────────────────────────────────────


def render_questions_block(rubric: Rubric) -> str:
    """Render the rubric questions as the `{questions_block}` substring
    that gets interpolated into the prompt template.

    Output structure (matches the legacy script's format_questions_block
    byte-for-byte when the rubric content is unchanged):

        \n=== <section> ===
        \n[Group] <criteria>
          Q1 [<analysis_tag>]: <observe_text>  (input ref: <input_ref>)
          Q2 [<analysis_tag>]: <observe_text>
          ...

    Section headers fire on each new section name; [Group] headers fire on
    each new criteria within a section. `(input ref: ...)` is only emitted
    when the question carries an input_ref (Art tab today).
    """
    lines: list[str] = []
    current_section: str | None = None
    current_criteria: str | None = None
    for q in rubric.all_questions():
        if q.section != current_section:
            current_section = q.section
            current_criteria = None
            lines.append(f"\n=== {current_section} ===")
        if q.criteria != current_criteria:
            current_criteria = q.criteria
            lines.append(f"\n[Group] {current_criteria}")
        analysis_tag = f"[{q.analysis_tag}]" if q.analysis_tag else ""
        input_hint = f"  (input ref: {q.input_ref})" if q.input_ref else ""
        lines.append(f"  {q.id} {analysis_tag}: {q.observe_text}{input_hint}")
    return "\n".join(lines)


# Shape labels mirror PLAN.md §3 — "A" = Gemini watches the video directly,
# "B" = Gemini extracted evidence, Claude (or another text reasoner) scores.
_SHAPE_A = "A"
_SHAPE_B = "B"


def render_prompt(
    rubric: Rubric,
    prompt_path: Path,
    *,
    shape: str = _SHAPE_A,
    duration_str: str,
    duration_sec: int,
    wallclock_start: str,
    wallclock_end: str,
) -> str:
    """Load a rubric prompt template from `prompt_path` and interpolate it
    with the rubric's question block + the run metadata.

    Shape A (Gemini direct):
      Returns a fully-rendered prompt ready to send alongside the trimmed
      video. The template uses Python str.format() — placeholders are bare
      `{name}` and literal braces are escaped `{{` / `}}` (per the
      externalised art template at prompts/art/rubric_art_v1_*.md).

    Shape B (text reasoner over Gemini-extracted evidence):
      Not implemented in 8a. The Shape B prompt template differs (inlines
      evidence JSON instead of attaching video), and the scoring path
      (`score()`) doesn't exist yet. Raises NotImplementedError.

    `prompt_path` is either an absolute Path or relative to prompts/.
    Both `prompts/art/rubric_art_v1_2026-06-10` and the full Path are OK —
    we don't load via load_prompt() here because the templates use
    str.format() not Jinja, and load_prompt strips frontmatter cleanly.
    """
    if shape != _SHAPE_A:
        raise NotImplementedError(
            f"render_prompt(shape={shape!r}) not implemented — "
            f"Shape B comes in migration step 8b"
        )

    # Resolve prompt_path. Accept either an absolute path or a prompts/-
    # relative id (e.g. 'art/rubric_art_v1_2026-06-10').
    if not isinstance(prompt_path, Path):
        prompt_path = Path(prompt_path)
    if not prompt_path.is_absolute():
        # Look under prompts/, trying both bare and .md-suffixed
        candidates = [
            prompt_path,
            _PROMPTS_DIR / prompt_path,
            _PROMPTS_DIR / f"{prompt_path}.md",
        ]
        for cand in candidates:
            if cand.exists():
                prompt_path = cand
                break
        else:
            raise FileNotFoundError(
                f"prompt not found under {_PROMPTS_DIR}: tried {candidates}"
            )

    raw = prompt_path.read_text()
    body = _strip_frontmatter(raw)

    questions_block = render_questions_block(rubric)

    rendered = body.format(
        questions_block=questions_block,
        duration_str=duration_str,
        duration_sec=duration_sec,
        wallclock_start=wallclock_start,
        wallclock_end=wallclock_end,
    )
    return rendered


# ─── Internal helpers ─────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _PROJECT_ROOT / "prompts"

import re as _re

_FRONTMATTER_RE = _re.compile(r"^---\n.*?\n---\n", _re.DOTALL)


def _strip_frontmatter(text: str) -> str:
    """Strip a leading `---\\n...\\n---\\n` YAML frontmatter block, if present.
    Mirrors pipeline.render.strip_frontmatter — duplicated here so this
    module has no dependency on the legacy render module."""
    if not text.startswith("---"):
        return text
    m = _FRONTMATTER_RE.match(text)
    return text[m.end():] if m else text


_HMS_RE = _re.compile(r"^\d{1,2}:\d{2}:\d{2}$")


def _is_valid_hms(s: str) -> bool:
    """Strict HH:MM:SS or H:MM:SS check. Does NOT validate field ranges
    (24h, 60m, 60s); just shape."""
    if not isinstance(s, str):
        return False
    return bool(_HMS_RE.match(s.strip()))


# ─── Scoring ──────────────────────────────────────────────────────────────


def score(
    *,
    rubric: Rubric,
    prompt: str,
    llm: LLMAdapter,
    session_id: str,
    rubric_version: str,
    video_path: Path,
    shape: str = _SHAPE_A,
    reasoner_model: str | None = None,
) -> tuple[RubricAnswerSet, str]:
    """Run one rubric scoring call. Returns (RubricAnswerSet, raw_response).

    Shape A (default): uploads `video_path` to Gemini and asks the rendered
    `prompt`. The model watches the trimmed class window and returns one
    answer per question. `reasoner_model` overrides the adapter's default
    vision model for this single call (used by the model-sweep runner).

    Shape B: defers to step 9 — the evidence-cache layer needs to land
    before we can drive a text reasoner over per-session evidence bundles.

    The returned RubricAnswerSet carries denormalised flags
    (insufficient_information, had_evidence, evidence_parse_ok) on each
    answer so the accumulator XLSX (step 10) can pivot without re-parsing
    answer strings. Raw model response is returned separately so the
    caller can persist it for audit alongside the parsed answers.
    """
    if shape != _SHAPE_A:
        raise NotImplementedError(
            f"score(shape={shape!r}) not implemented — "
            "Shape B comes in migration step 9 with the evidence cache"
        )
    if not video_path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")

    log.info(
        f"[{session_id}] score: uploading {video_path.name} + asking "
        f"{reasoner_model or llm.vision_model} about "
        f"{len(rubric.all_questions())} questions"
    )
    video_file = llm.upload_video(video_path)
    raw = llm.call_gemini_video(
        prompt=prompt,
        video_file=video_file,
        model_name=reasoner_model,
    )

    parsed = parse_json_lenient(raw)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"expected dict response keyed by Q-id, got {type(parsed).__name__}"
        )

    answer_set = _build_answer_set(
        parsed_response=parsed,
        rubric=rubric,
        session_id=session_id,
        rubric_version=rubric_version,
        shape=shape,
        source_model=reasoner_model or llm.vision_model,
        prompt=prompt,
    )
    log.info(
        f"[{session_id}] score: {len(answer_set.answers)} answers parsed "
        f"({sum(1 for a in answer_set.answers.values() if a.insufficient_information)} "
        "INSUFFICIENT)"
    )
    return answer_set, raw


def _build_answer_set(
    *,
    parsed_response: dict,
    rubric: Rubric,
    session_id: str,
    rubric_version: str,
    shape: str,
    source_model: str,
    prompt: str,
) -> RubricAnswerSet:
    """Walk the model's JSON response, validate per-question shape, and
    assemble a typed RubricAnswerSet. Skips unexpected keys + malformed
    rows with a warning — never raises on a single bad answer."""
    valid_ids = {q.id for q in rubric.all_questions()}
    answers: dict[str, RubricAnswer] = {}

    for qid, payload in parsed_response.items():
        if qid not in valid_ids:
            log.warning(f"[{session_id}] unexpected qid {qid!r}, skipping")
            continue
        if not isinstance(payload, dict):
            log.warning(
                f"[{session_id}] {qid}: payload not a dict ({type(payload).__name__}), "
                "skipping"
            )
            continue

        ans_str = str(payload.get("answer", "")).strip()
        is_insufficient = ans_str.upper().startswith("INSUFFICIENT")

        ev_ts = payload.get("evidence_timestamps") or []
        if isinstance(ev_ts, str):
            ev_ts = [ev_ts]
        ev_ts = [str(t).strip() for t in ev_ts if str(t).strip()]
        had_evidence = bool(ev_ts)
        evidence_parse_ok = all(_is_valid_hms(t) for t in ev_ts) if had_evidence else True

        confidence = str(payload.get("confidence", "low")).strip().lower()
        if confidence not in ("high", "medium", "low"):
            log.warning(
                f"[{session_id}] {qid}: confidence={confidence!r} not in "
                "{high, medium, low}; coercing to 'low'"
            )
            confidence = "low"

        try:
            answers[qid] = RubricAnswer(
                id=qid,
                answer=ans_str,
                confidence=confidence,
                evidence_timestamps=ev_ts,
                rationale=payload.get("rationale"),
                insufficient_information=is_insufficient,
                had_evidence=had_evidence,
                evidence_parse_ok=evidence_parse_ok,
            )
        except Exception as e:
            log.warning(f"[{session_id}] {qid}: validation failed ({e!r}); skipping")

    missing = sorted(valid_ids - set(answers.keys()))
    if missing:
        log.warning(
            f"[{session_id}] {len(missing)} questions not in model response: "
            f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
        )

    return RubricAnswerSet(
        session_id=session_id,
        subject=rubric.subject,
        rubric_version=rubric_version,
        answers=answers,
        source_model=source_model,
        shape=shape,
        prompt_hash=prompt_hash(prompt),
    )
