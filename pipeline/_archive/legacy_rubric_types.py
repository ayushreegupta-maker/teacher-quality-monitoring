"""
Legacy 5-dimension rubric Pydantic types — archived 2026-06-10.

Used only by the archived `pipeline.render.render_score_prompt()` flow and
the archived YAML rubrics under `_archive/rubric/`. The new Q&A
architecture (PLAN.md §3.1) uses `pipeline.types.Rubric` / `RubricSection`
/ `RubricQuestion` instead.

To restore for use by archived code: import from this module instead of
`pipeline.types`.
"""
from typing import Optional

from pydantic import BaseModel, field_validator


class LegacyRubricDimension(BaseModel):
    id: str
    label: str
    description: str
    indicators: list[str]
    signal_mappings: dict[str, list[str]]
    anchors: dict[float, str]
    common_failure_modes: Optional[list[str]] = None
    scoring_direction: Optional[str] = None


class LegacyRubricDomain(BaseModel):
    id: str
    label: str
    description: str
    dimensions: list[LegacyRubricDimension]


class LegacyRubric(BaseModel):
    version: str
    name: str
    created_at: str
    context: dict
    scoring_scale: dict
    anti_bias_rules: list[str]
    evidence_requirements: list[str]
    domains: list[LegacyRubricDomain]

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_to_str(cls, v):
        return str(v)

    def get_dimension(self, dim_id: str) -> LegacyRubricDimension:
        for domain in self.domains:
            for dim in domain.dimensions:
                if dim.id == dim_id:
                    return dim
        raise KeyError(f"Dimension {dim_id} not found in rubric")

    def all_dimensions(self) -> list[LegacyRubricDimension]:
        return [dim for domain in self.domains for dim in domain.dimensions]
