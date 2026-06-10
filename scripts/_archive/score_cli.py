"""
Phase 0a entry point. Score one local classroom video end-to-end.

Usage:
    .venv/bin/python score_cli.py path/to/video.mp4 \\
        --session-id my_session_001 \\
        --recorded-at 2026-05-15 \\
        --duration 30 \\
        [--age-range "3-5 years"] \\
        [--subject "general preschool"] \\
        [--teacher-id T_042] \\
        [--classroom-id C_07]

Writes artifacts to data/sessions/<session_id>/.
Requires ANTHROPIC_API_KEY and GOOGLE_API_KEY in env.
"""

import argparse
import asyncio
import logging
from datetime import date
from pathlib import Path

from adapters.llm import LLMAdapter
from adapters.sessions import register_session, session_dir
from pipeline.render import load_rubric
from pipeline.score import score_session
from pipeline.types import SessionMeta
from pipeline.vision import vision_observe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("score_cli")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 0a end-to-end scoring CLI")
    p.add_argument("video", type=Path, help="local path to the video file")
    p.add_argument("--session-id", required=True)
    p.add_argument("--recorded-at", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    p.add_argument("--duration", type=int, required=True, help="duration in minutes")
    p.add_argument("--age-range", default="3-5 years")
    p.add_argument("--subject", default="general preschool")
    p.add_argument(
        "--activity-context",
        default=None,
        help="Free-form description of the activity/setup that helps the vision and scoring models understand what they are looking at (e.g. 'playground design eval — empty space first, then children entering').",
    )
    p.add_argument("--teacher-id", default=None)
    p.add_argument("--classroom-id", default=None)
    p.add_argument("--rubric", type=Path, default=None, help="path to rubric YAML (default: rubric/rubric_v0_1.yaml)")
    return p.parse_args()


async def main():
    args = parse_args()

    if not args.video.exists():
        raise FileNotFoundError(f"video not found: {args.video}")

    meta = SessionMeta(
        session_id=args.session_id,
        recorded_at=args.recorded_at,
        age_range=args.age_range,
        duration_minutes=args.duration,
        subject=args.subject,
        activity_context=args.activity_context,
        teacher_id=args.teacher_id,
        classroom_id=args.classroom_id,
        video_path=args.video.resolve(),
    )

    sd = register_session(meta)
    log.info(f"registered session at {sd}")

    rubric = load_rubric(args.rubric)
    log.info(
        f"loaded rubric '{rubric.name}' v{rubric.version} "
        f"with {len(rubric.all_dimensions())} dimensions"
    )

    llm = LLMAdapter()

    log.info("=== vision pass ===")
    transcript, observations = vision_observe(meta, llm)

    log.info("=== per-dimension scoring ===")
    scores = await score_session(meta, transcript, observations, rubric, llm)

    print(f"\n=== {meta.session_id} ===")
    print(f"Overall: {scores.overall:.2f}" if scores.overall is not None else "Overall: N/A")
    for dim_id, ds in scores.scores.items():
        score_str = ds.score if not isinstance(ds.score, int) else str(ds.score)
        print(f"  {dim_id:35s}  score={score_str:5}  confidence={ds.confidence}")
    print(f"\nArtifacts in: {session_dir(meta.session_id)}")


if __name__ == "__main__":
    asyncio.run(main())
