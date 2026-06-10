"""
Q&A rubric loader.

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

from pipeline.types import Rubric, RubricQuestion, RubricSection

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
