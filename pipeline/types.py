from datetime import date
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field


class SessionMeta(BaseModel):
    session_id: str
    recorded_at: date
    age_range: str = "3-5 years"
    duration_minutes: int
    subject: str = "general preschool"
    activity_context: Optional[str] = None
    teacher_id: Optional[str] = None
    classroom_id: Optional[str] = None
    school_id: str = "default"
    video_path: Path


class TranscriptSegment(BaseModel):
    ts_start: str
    ts_end: Optional[str] = None
    speaker: str
    text: str


class Transcript(BaseModel):
    session_id: str
    segments: list[TranscriptSegment]
    source_model: str
    prompt_hash: Optional[str] = None


class VisualObservation(BaseModel):
    ts_start: str
    ts_end: str
    description: str


class VisualObservations(BaseModel):
    session_id: str
    observations: list[VisualObservation]
    source_model: str
    prompt_hash: Optional[str] = None


# Evidence + ScoreValue + DimensionScore + SessionScores + ItemEntry +
# OtherItem + ConsolidatedItems archived 2026-06-10 to
# pipeline/_archive/legacy_score_types.py — all tied to the legacy 5-dim
# scoring + items-extraction flow. Zero live callers.


class BoundaryDetection(BaseModel):
    first_child_visible_at: Optional[str] = None  # HH:MM:SS or null (elapsed)
    last_child_visible_at: Optional[str] = None
    confidence: Literal["high", "medium", "low"]
    notes: Optional[str] = None
    # New in prompt v0.7.0: rather than fighting the model's tendency to read
    # the burned-in CCTV wall clock (which produced systemic leaks like
    # last_child=00:42:00 from a 09:42:00 wall reading), we now LET the model
    # read the clock and report the readings verbatim at three moments.
    # Pipeline does the subtraction (clock_event − clock_start) to compute
    # elapsed time in `first_child_visible_at` / `last_child_visible_at`.
    # All three may be null if no clock is visible (then model fills the
    # elapsed fields directly as before).
    video_start_wall_clock: Optional[str] = None
    first_child_wall_clock: Optional[str] = None
    last_child_wall_clock: Optional[str] = None
    # Added in prompt v0.6.0: symmetric grounding for first_child. Previously
    # only last_child had a required evidence field, which is why first_child
    # timestamps tended to be vaguer (off by ~53 sec on Morning circle time).
    first_child_evidence: Optional[str] = None
    # Added in prompt v0.3.0: forces the model to ground last_child_visible_at
    # in specific visual evidence (defends against generic "the room is empty"
    # answers that often accompany hallucinated timestamps).
    last_child_evidence: Optional[str] = None
    # Added in prompt v0.3.0: the model's own assertion that both timestamps
    # fall within the recording duration AND grounding was provided. The
    # pipeline acts on self_check_passed=False as a hard fallback to null/null
    # (see Fix 1 in compute_windows).
    self_check_passed: Optional[bool] = None
    # Pipeline-set fields (not returned by the LLM)
    session_id: Optional[str] = None
    source_model: Optional[str] = None
    prompt_hash: Optional[str] = None


# Legacy 5-dim rubric types (LegacyRubric, LegacyRubricDimension,
# LegacyRubricDomain) lived here through migration step 6; archived
# 2026-06-10 to pipeline/_archive/legacy_rubric_types.py once the
# archived render_score_prompt() became their sole consumer. See
# DECISIONS.md for the dead-code sweep rationale.

# ─── New Q&A rubric (Excel-driven; one tab per subject) ────────────────────
# Drives the new architecture. See PLAN.md §3.1 + pipeline/rubric.py.
AnswerType = Literal["scored_1_4", "yes_no", "numeric", "multi_choice", "free_text"]


class RubricQuestion(BaseModel):
    """One row in the workbook's 'What AI needs to observe' column."""
    id: str                              # "Q1", "Q2", ...
    section: str                         # "Environment", "Content Knowledge", ...
    criteria: Optional[str] = None       # the group label (col B); spans multiple Qs
    observe_text: str                    # the actual question (col C)
    input_ref: Optional[str] = None      # optional input reference (col D — art only)
    analysis_tag: str                    # "Visual" / "Audio" / "Visual + Audio" (col E)
    # ─── New columns added 2026-06-10 — typed answer support ─────────────
    answer_type: AnswerType = "free_text"  # col F; defaults preserve legacy behaviour
    description: Optional[str] = None      # col G — "what good looks like"
    levels: Optional[list[str]] = None     # cols H-K — 4 strings for scored_1_4 only
    options: Optional[list[str]] = None    # col L — for multi_choice only


class RubricSection(BaseModel):
    name: str                            # "Environment" / "Content Knowledge" / ...
    questions: list[RubricQuestion]


class Rubric(BaseModel):
    subject: str                         # "art" / "public_speaking" / "robotics"
    source_path: str                     # absolute path to the workbook on disk
    sections: list[RubricSection]

    def all_questions(self) -> list[RubricQuestion]:
        return [q for s in self.sections for q in s.questions]

    def get_question(self, qid: str) -> RubricQuestion:
        for q in self.all_questions():
            if q.id == qid:
                return q
        raise KeyError(f"Question {qid!r} not found in rubric {self.subject!r}")


class RubricAnswer(BaseModel):
    """One answered question from a single rubric scoring run."""
    id: str                              # "Q1", "Q2", ...
    answer: str                          # the model's free-form answer text;
                                         # numbers come through as strings
    confidence: Literal["high", "medium", "low"]
    evidence_timestamps: list[str] = Field(default_factory=list)  # HH:MM:SS
    rationale: Optional[str] = None
    # Convenience flags populated by the scorer for downstream filtering.
    # Stored on the answer (not derived ad-hoc) so the accumulator XLSX
    # columns 21-23 (insufficient_information, had_evidence,
    # evidence_parse_ok) can pivot without re-parsing answer strings.
    insufficient_information: bool = False
    had_evidence: bool = False
    evidence_parse_ok: bool = True
    # True if `answer` matches the question's declared answer_type
    # (e.g. integer for `numeric`, "Yes"/"No" for `yes_no`, etc.).
    # Set by pipeline.rubric._build_answer_set after the model's response
    # is parsed.  Defaults to True so legacy `free_text` answers stay valid.
    answer_type_valid: bool = True


class MaterialSeen(BaseModel):
    """One distinct teaching material / apparatus / resource observed across
    a session. Emitted by the Shape B reasoner alongside Q1-QN answers."""
    item: str                            # concise name, e.g. "lego blocks (red, white)"
    first_seen: Optional[str] = None     # HH:MM:SS of earliest observation
    category: Optional[str] = None       # kit / consumable / book / card / device / other
    notes: Optional[str] = None          # short context, e.g. "primary build material"


class RubricAnswerSet(BaseModel):
    """The output of one rubric scoring run on one session."""
    session_id: str
    subject: str
    rubric_version: str                  # e.g. "v1_2026-06-10"
    answers: dict[str, RubricAnswer]     # keyed by Q-id
    source_model: str                    # the reasoning model that answered
    shape: Literal["A", "B"]             # A = Gemini direct, B = Gemini→text reasoner
    prompt_hash: Optional[str] = None
    rendered_prompt_path: Optional[str] = None  # for audit
    raw_response_path: Optional[str] = None     # for audit
    # Deduplicated list of materials the reasoner saw across the session.
    # Designed to be diffed against a canonical Openhouse-supplied materials
    # list to flag missing / extra items.  None when the reasoner didn't emit
    # the field (e.g. older prompt versions).
    materials_seen: Optional[list[MaterialSeen]] = None


class EvidenceBundle(BaseModel):
    """Cached vision-pass output for one session, keyed by
    (session_id, vision_model, vision_fps, chunking). The Shape B scoring
    path consumes this bundle instead of re-watching the video.

    Per the step-9 design decision (Q3 = "vision + Shape-A-derived"):
    the bundle carries the vision-pure block (boundaries, transcript,
    observations) plus optional Shape-A-derived enrichment (phases,
    explanations, disturbances) that's populated when a prior Shape A
    run is available for the same session.
    """
    # Cache key (must match the on-disk dir name)
    session_id: str
    subject: str
    vision_model: str
    vision_fps: str           # "default" if no fps override; otherwise "0.30" etc.
    chunking: str             # "5min" / "10min" / "single"
    # Vision-pure block
    boundaries: dict                              # 2_boundaries.json shape
    transcript: list[dict] = Field(default_factory=list)    # cleaned segments
    observations: list[dict] = Field(default_factory=list)
    # Shape-A enrichment (optional)
    phases: Optional[list[dict]] = None
    explanations: Optional[list[dict]] = None
    disturbances: Optional[list[dict]] = None
    # Per-session context fed into the vision-pass prompt (PLAN.md /
    # session-metadata gap, option-c patch). Recorded in the bundle so
    # downstream Shape B reasoning can also see what context the vision
    # pass had. Both fields are deliberately NOT part of the cache key —
    # the caller is expected to pass `--force` when context changes for
    # the same session.
    activity_context: Optional[str] = None
    teacher_id: Optional[str] = None
    # Provenance + traceability
    built_at: Optional[str] = None                # ISO timestamp
    source_run_dir: Optional[str] = None
    transcript_source_model: Optional[str] = None
    observations_source_model: Optional[str] = None
