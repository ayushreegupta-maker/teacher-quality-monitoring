"""
One-off seed: populate `data/tqm_answers.xlsx` from the cached
2026-06-04 art run + the one-off Claude (Shape B) experiment from the
same week. Gives us a non-empty workbook to verify the merge/pivot
behaviour before pilot runs land.

The two source files we have:
  data/_archive/art_rubric_runs/2026-06-04_122724/4_rubric_answers.json
      ← Shape A, Gemini-2.5-flash, art rubric v1
  data/_archive/art_rubric_runs/2026-06-04_122724/7_claude_answers.json
      ← Shape B, Claude Opus 4.7, art rubric v1
                                  (the "score_art_with_claude" one-off)

Both score the same session (2026-05-18__D28__0900) against the same 31
art rubric questions. They become run_n=1 and run_n=1 respectively
(different shapes, so each is the FIRST run of its config).

Usage:
    .venv/bin/python scripts/seed_answers_book.py [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from pipeline.answers_book import (
    compute_run_n,
    init_workbook,
    merge_queue,
    write_sidecar,
)
from pipeline.rubric import load_rubric
from pipeline.types import RubricAnswer, RubricAnswerSet

WORKBOOK = Path.home() / "Downloads" / "Teacher Quality Monitoring (1).xlsx"
ANSWERS_XLSX = ROOT / "data" / "tqm_answers.xlsx"
QUEUE_DIR = ROOT / "data" / "_answer_queue"
CACHED_RUN = ROOT / "data" / "_archive" / "art_rubric_runs" / "2026-06-04_122724"

SESSION_ID = "2026-05-18__D28__0900"
RUBRIC_VERSION = "v1_2026-06-10"


def _answer_from_dict(qid: str, d: dict) -> RubricAnswer:
    """Best-effort conversion from a legacy answer dict into the new shape.
    Same logic as pipeline.rubric._build_answer_set's per-answer pass."""
    ans_str = str(d.get("answer", "")).strip()
    is_ins = ans_str.upper().startswith("INSUFFICIENT")
    # Legacy Gemini Shape A used 'evidence' (singular string),
    # legacy Claude Shape B used 'evidence_timestamps' (list of strings)
    if "evidence_timestamps" in d:
        ev = d.get("evidence_timestamps") or []
        if isinstance(ev, str):
            ev = [ev]
        ev = [str(x).strip() for x in ev if str(x).strip()]
    else:
        # The Shape A 'evidence' field is free-form prose with embedded
        # timestamps. Pull HH:MM:SS-shaped tokens out so the accumulator
        # still has SOMETHING in evidence_timestamps; otherwise leave empty.
        import re
        ev_text = str(d.get("evidence", ""))
        ev = re.findall(r"\b\d{1,2}:\d{2}:\d{2}\b", ev_text)
    confidence = str(d.get("confidence", "low")).lower().strip()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    from pipeline.rubric import _is_valid_hms
    return RubricAnswer(
        id=qid,
        answer=ans_str,
        confidence=confidence,
        evidence_timestamps=ev,
        rationale=d.get("rationale") or d.get("evidence"),
        insufficient_information=is_ins,
        had_evidence=bool(ev),
        evidence_parse_ok=all(_is_valid_hms(t) for t in ev) if ev else True,
    )


def _answer_set_from_legacy(
    src: Path, *, shape: str, source_model: str, prompt_hash: str
) -> RubricAnswerSet:
    """Convert a legacy 4_rubric_answers.json or 7_claude_answers.json into
    a typed RubricAnswerSet, ready to be folded into the accumulator."""
    raw = json.loads(src.read_text())
    answers = {qid: _answer_from_dict(qid, d) for qid, d in raw.items()}
    return RubricAnswerSet(
        session_id=SESSION_ID,
        subject="art",
        rubric_version=RUBRIC_VERSION,
        answers=answers,
        source_model=source_model,
        shape=shape,
        prompt_hash=prompt_hash,
    )


def _seed_one(
    *,
    answer_set: RubricAnswerSet,
    config: dict,
    config_slug: str,
    started_at: str,
    finished_at: str,
    wall_clock_seconds: float,
    rubric,
) -> None:
    run_id = started_at
    run_n = compute_run_n(
        ANSWERS_XLSX,
        session_id=answer_set.session_id, subject=answer_set.subject,
        rubric_version=answer_set.rubric_version, shape=answer_set.shape,
        reasoner=answer_set.source_model,
    )
    write_sidecar(
        QUEUE_DIR,
        answer_set=answer_set, rubric=rubric, config=config,
        run_id=run_id, started_at=started_at, finished_at=finished_at,
        wall_clock_seconds=wall_clock_seconds, config_slug=config_slug,
        run_n=run_n,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--force", action="store_true",
                   help="Delete existing tqm_answers.xlsx + queue first")
    args = p.parse_args()

    if args.force and ANSWERS_XLSX.exists():
        print(f"--force: removing {ANSWERS_XLSX}")
        ANSWERS_XLSX.unlink()
    if args.force and QUEUE_DIR.exists():
        for f in QUEUE_DIR.iterdir():
            f.unlink()

    init_workbook(ANSWERS_XLSX)
    rubric = load_rubric(WORKBOOK, "art")

    # ── Shape A: 4_rubric_answers.json ──
    src_a = CACHED_RUN / "4_rubric_answers.json"
    if src_a.exists():
        aset_a = _answer_set_from_legacy(
            src_a, shape="A", source_model="gemini-2.5-flash",
            prompt_hash="seed-2026-06-04-shapeA",
        )
        config_a = {
            "session_id": SESSION_ID, "subject": "art",
            "rubric_version": RUBRIC_VERSION, "shape": "A",
            "reasoner_model": None, "source_model": "gemini-2.5-flash",
            "vision_model": None, "vision_fps": None, "chunking": "5min",
            "seeded_from": str(src_a),
        }
        _seed_one(
            answer_set=aset_a, config=config_a,
            config_slug="2026-06-04T122724__v1_2026-06-10__gemini-2.5-flash__A",
            started_at="2026-06-04T12:27:24",
            finished_at="2026-06-04T13:42:11",
            wall_clock_seconds=4487.0,
            rubric=rubric,
        )
        print(f"seeded Shape A from {src_a.name}")

    # ── Shape B: 7_claude_answers.json ──
    src_b = CACHED_RUN / "7_claude_answers.json"
    if src_b.exists():
        aset_b = _answer_set_from_legacy(
            src_b, shape="B", source_model="claude-opus-4-7",
            prompt_hash="seed-2026-06-04-shapeB",
        )
        config_b = {
            "session_id": SESSION_ID, "subject": "art",
            "rubric_version": RUBRIC_VERSION, "shape": "B",
            "reasoner_model": "claude-opus-4-7", "source_model": "claude-opus-4-7",
            "vision_model": "gemini-2.5-flash", "vision_fps": None, "chunking": "5min",
            "seeded_from": str(src_b),
        }
        _seed_one(
            answer_set=aset_b, config=config_b,
            config_slug="2026-06-09T193800__v1_2026-06-10__claude-opus-4-7__B",
            started_at="2026-06-09T19:38:00",
            finished_at="2026-06-09T19:38:30",
            wall_clock_seconds=25.0,
            rubric=rubric,
        )
        print(f"seeded Shape B from {src_b.name}")

    result = merge_queue(ANSWERS_XLSX, QUEUE_DIR)
    print(f"merge: {result}")
    print(f"wrote {ANSWERS_XLSX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
