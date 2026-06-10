"""
TQM consolidation — migration step 1: archive legacy files.

Moves listed in PLAN.md §3.7 step 1. Each move is logged to
migration_log.txt via scripts.migrate.atomic_mv_with_log, so any subset can
be undone later via `python scripts/migrate.py reverse --step 1`.

Usage:
    # Preview (default — touches nothing)
    .venv/bin/python scripts/migrate_step1_archive.py

    # Actually perform the moves
    .venv/bin/python scripts/migrate_step1_archive.py --execute

    # Skip the y/N confirmation in --execute mode
    .venv/bin/python scripts/migrate_step1_archive.py --execute --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.migrate import (
    PROJECT_ROOT,
    atomic_mv_with_log,
    require_confirmation,
)

STEP = "1"

# Source → target. Sources that don't exist are silently skipped (printed as
# "SKIP (missing source)" by atomic_mv_with_log) — keeps the script
# idempotent if you re-run after a partial completion.
MOVES: list[tuple[Path, Path, str]] = [
    # --- data dirs ---
    (
        PROJECT_ROOT / "data" / "sessions",
        PROJECT_ROOT / "data" / "_archive" / "sessions_batch_era",
        "legacy outputs from older batch_*.py runners",
    ),
    (
        PROJECT_ROOT / "data" / "segments",
        PROJECT_ROOT / "data" / "_archive" / "segments",
        "old chunking step outputs",
    ),
    (
        PROJECT_ROOT / "data" / "batch_long_reports",
        PROJECT_ROOT / "data" / "_archive" / "batch_long_reports",
        "older batch-runner reports",
    ),
    (
        PROJECT_ROOT / "data" / "batch_reports",
        PROJECT_ROOT / "data" / "_archive" / "batch_reports",
        "older batch-runner reports",
    ),
    (
        PROJECT_ROOT / "data" / "long_video_reports",
        PROJECT_ROOT / "data" / "_archive" / "long_video_reports",
        "older long-video runner reports",
    ),
    # --- top-level app scaffold (unused) ---
    (
        PROJECT_ROOT / "app",
        PROJECT_ROOT / "data" / "_archive" / "app",
        "unused web UI scaffold",
    ),
    # --- legacy top-level Python ---
    (
        PROJECT_ROOT / "batch_long_video.py",
        PROJECT_ROOT / "scripts" / "_archive" / "batch_long_video.py",
        "predecessor of run_art_rubric_test.py",
    ),
    (
        PROJECT_ROOT / "batch_score.py",
        PROJECT_ROOT / "scripts" / "_archive" / "batch_score.py",
        "old 5-dim rubric batch runner",
    ),
    (
        PROJECT_ROOT / "score_long_video.py",
        PROJECT_ROOT / "scripts" / "_archive" / "score_long_video.py",
        "old long-video scoring runner",
    ),
    (
        PROJECT_ROOT / "tqm_db.py",
        PROJECT_ROOT / "scripts" / "_archive" / "tqm_db.py",
        "top-level legacy DB helper",
    ),
    (
        PROJECT_ROOT / "test_render.py",
        PROJECT_ROOT / "scripts" / "_archive" / "test_render.py",
        "old test scaffold at project root",
    ),
    (
        PROJECT_ROOT / "score_cli.py",
        PROJECT_ROOT / "scripts" / "_archive" / "score_cli.py",
        "phase-0a single-video scorer, uses old 5-dim rubric",
    ),
    # --- pipeline modules tied to old rubric shape ---
    (
        PROJECT_ROOT / "pipeline" / "extract.py",
        PROJECT_ROOT / "pipeline" / "_archive" / "extract.py",
        "older artifact-extraction helpers",
    ),
    (
        PROJECT_ROOT / "pipeline" / "items.py",
        PROJECT_ROOT / "pipeline" / "_archive" / "items.py",
        "item/dimension classes for old 5-dim rubric",
    ),
    (
        PROJECT_ROOT / "pipeline" / "score.py",
        PROJECT_ROOT / "pipeline" / "_archive" / "score.py",
        "old per-dimension scoring, replaced by pipeline/rubric.py",
    ),
    # --- prompts tied to old flow ---
    (
        PROJECT_ROOT / "prompts" / "score_dimension.md",
        PROJECT_ROOT / "prompts" / "_archive" / "score_dimension.md",
        "old 5-dimension scoring prompt",
    ),
    (
        PROJECT_ROOT / "prompts" / "consolidate_items.md",
        PROJECT_ROOT / "prompts" / "_archive" / "consolidate_items.md",
        "item-consolidation prompt for old flow",
    ),
]


def _short(p: Path) -> str:
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)


def _build_plan_lines() -> list[str]:
    lines = [
        f"Migration step {STEP}: archive legacy files.",
        f"Log target: {_short(PROJECT_ROOT / 'migration_log.txt')}",
        "",
    ]
    present = 0
    missing = 0
    target_collision = 0
    for src, dst, note in MOVES:
        status = ""
        if not src.exists():
            status = "  [SKIP — source missing]"
            missing += 1
        elif dst.exists():
            status = "  [SKIP — destination already exists]"
            target_collision += 1
        else:
            present += 1
        lines.append(f"  {_short(src):<48} →  {_short(dst)}{status}")
        lines.append(f"      ({note})")
    lines.append("")
    lines.append(
        f"Summary: {present} will move, {missing} missing, "
        f"{target_collision} already at target."
    )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform the moves (default is dry-run)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the y/N confirmation in --execute mode",
    )
    args = parser.parse_args()

    plan_lines = _build_plan_lines()
    for line in plan_lines:
        print(line)

    if not args.execute:
        print("\nDRY RUN — nothing moved. Re-run with --execute to perform the moves.")
        return 0

    if not require_confirmation(["About to execute the moves above."], auto_yes=args.yes):
        print("Aborted by user.")
        return 1

    print()
    moved = 0
    for src, dst, note in MOVES:
        ok = atomic_mv_with_log(src, dst, step=STEP, note=note)
        if ok:
            moved += 1

    print(f"\nDone. Moved {moved} of {len(MOVES)} entries.")
    print(f"Log written to {_short(PROJECT_ROOT / 'migration_log.txt')}.")
    print(f"To reverse: python scripts/migrate.py reverse --step {STEP} --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
