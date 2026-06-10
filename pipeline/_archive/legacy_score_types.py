"""
Archived Pydantic types from pipeline.types — 2026-06-10 (round 2).

All seven types belong to the legacy 5-dimension scoring path or the
items-extraction pipeline that the new Q&A architecture doesn't use:

  Evidence            — per-indicator evidence quote (transcript|visual)
  ScoreValue          — Union[int, float, Literal['insufficient_evidence']]
  DimensionScore      — one dimension's score + evidence + confidence
  SessionScores       — all dimension scores for one session
  ItemEntry           — material/resource extracted by consolidate_items
  OtherItem           — non-zone item in the room
  ConsolidatedItems   — the consolidated-items pipeline output

Restore via: `from pipeline._archive.legacy_score_types import ...`
"""
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field


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
