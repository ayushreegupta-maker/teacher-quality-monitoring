from datetime import date
from pathlib import Path
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


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


class Evidence(BaseModel):
    ts_start: str
    ts_end: str
    type: Literal["transcript", "visual"]
    quote: str
    indicator: str
    reasoning: str


ScoreValue = Union[int, float, Literal["insufficient_evidence"]]


class DimensionScore(BaseModel):
    dimension_id: str
    rubric_version: str
    score: ScoreValue
    anchor_matched: Optional[str] = None
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"]
    scorer_notes: Optional[str] = None
    prompt_hash: Optional[str] = None


class SessionScores(BaseModel):
    session_id: str
    rubric_version: str
    scores: dict[str, DimensionScore]
    overall: Optional[float] = None
    cost_usd: Optional[float] = None
    duration_seconds: Optional[float] = None


class ItemEntry(BaseModel):
    category: str
    name: str
    count_or_quantity: Optional[str] = None
    specifics: Optional[str] = None
    first_seen_at: Optional[str] = None


class OtherItem(BaseModel):
    name: str
    location: Optional[str] = None


class ConsolidatedItems(BaseModel):
    activity_zone_items: list[ItemEntry] = Field(default_factory=list)
    other_items_in_room: list[OtherItem] = Field(default_factory=list)
    notes: Optional[str] = None
    # Pipeline-set fields (not returned by the LLM)
    session_id: Optional[str] = None
    source_model: Optional[str] = None
    prompt_hash: Optional[str] = None


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


class RubricDimension(BaseModel):
    id: str
    label: str
    description: str
    indicators: list[str]
    signal_mappings: dict[str, list[str]]
    anchors: dict[float, str]
    common_failure_modes: Optional[list[str]] = None
    scoring_direction: Optional[str] = None


class RubricDomain(BaseModel):
    id: str
    label: str
    description: str
    dimensions: list[RubricDimension]


class Rubric(BaseModel):
    version: str
    name: str
    created_at: str
    context: dict
    scoring_scale: dict
    anti_bias_rules: list[str]
    evidence_requirements: list[str]
    domains: list[RubricDomain]

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_to_str(cls, v):
        return str(v)

    def get_dimension(self, dim_id: str) -> RubricDimension:
        for domain in self.domains:
            for dim in domain.dimensions:
                if dim.id == dim_id:
                    return dim
        raise KeyError(f"Dimension {dim_id} not found in rubric")

    def all_dimensions(self) -> list[RubricDimension]:
        return [dim for domain in self.domains for dim in domain.dimensions]
