"""
scripts/run_rubric.py — subject-agnostic rubric runner.

The single entry point for the new architecture. Takes a session_id +
rubric version + shape, walks the cache layers (session video → rubric
prompt → score), and writes a fully-typed RubricAnswerSet to
data/rubric_runs/<subject>/<config_slug>/.

Usage:
    .venv/bin/python scripts/run_rubric.py \\
        --session-id 2026-05-18__D28__0900 \\
        --rubric-version v1_2026-06-10 \\
        --shape A \\
        [--workbook "/Users/oh/Downloads/Teacher Quality Monitoring (1).xlsx"] \\
        [--reasoner gemini-3.1-pro] \\
        [--force] \\
        [--dry-run]

What gets written to data/rubric_runs/<subject>/<config_slug>/:
    0_config.json          ← exactly what was run, for audit + reproducibility
    4_rendered_prompt.txt  ← the prompt body sent to the model
    5_answers.json         ← typed RubricAnswerSet
    5_answers_raw.txt      ← raw model response (for debugging parse failures)

Shape A (Gemini direct) is implemented. Shape B (text reasoner over the
evidence bundle) is deferred to step 9 when the evidence cache lands.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.llm import LLMAdapter
from pipeline.boundaries import _parse_hms
from pipeline.rubric import load_rubric, render_prompt, score
from pipeline.session_context import (
    _load_camera_lookup,
    parse_session_id,
)
from pipeline.session_video import (
    build_session_video,
    compute_trim_window,
    session_dir_for,
    video_duration_seconds,
)

DEFAULT_WORKBOOK = Path.home() / "Downloads" / "Teacher Quality Monitoring (1).xlsx"
DEFAULT_CAMERAS_XLSX = ROOT / "data" / "cctv_cameras.xlsx"
RUBRIC_RUNS_DIR = ROOT / "data" / "rubric_runs"
PROMPTS_DIR = ROOT / "prompts"

log = logging.getLogger("run_rubric")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ─── Wall-clock derivation (lifted from legacy run_art_rubric_test.py) ────
def _derive_trimmed_metadata(
    *,
    trimmed: Path,
    combined: Path,
    boundaries_path: Path,
) -> dict:
    """Build the 4 prompt-metadata fields from the session-video cache:

      duration_str, duration_sec   — from ffprobe on the trimmed file
      wallclock_start, wallclock_end — derived from video_start_wall_clock
          in 2_boundaries.json + the trim-start offset computed from the
          same boundaries.

    Defaults to '??:??:??' for wall-clock if anything is missing — same
    graceful fallback as the legacy script (the prompt still renders, with
    a flagged unknown anchor).
    """
    dur_sec_f = video_duration_seconds(trimmed)
    dur_sec = int(round(dur_sec_f))
    h, rem = divmod(dur_sec, 3600)
    m, s = divmod(rem, 60)
    duration_str = f"{h:02d}:{m:02d}:{s:02d}"

    wallclock_start = "??:??:??"
    wallclock_end = "??:??:??"
    if boundaries_path.exists():
        try:
            b = json.loads(boundaries_path.read_text())
            combined_start_wc = b.get("video_start_wall_clock")
            comb_dur = video_duration_seconds(combined)
            trim_start_sec, _ = compute_trim_window(b, comb_dur)
            if combined_start_wc:
                base_sec = _parse_hms(combined_start_wc) or 0
                start_total = base_sec + int(trim_start_sec)
                end_total = start_total + dur_sec
                def _fmt(total_sec: int) -> str:
                    hh = (total_sec // 3600) % 24
                    mm = (total_sec // 60) % 60
                    ss = total_sec % 60
                    return f"{hh:02d}:{mm:02d}:{ss:02d}"
                wallclock_start = _fmt(start_total)
                wallclock_end = _fmt(end_total)
        except Exception as e:
            log.warning(f"couldn't derive wall-clock anchors: {e}")

    return {
        "duration_str": duration_str,
        "duration_sec": dur_sec,
        "wallclock_start": wallclock_start,
        "wallclock_end": wallclock_end,
    }


# ─── Path resolution ──────────────────────────────────────────────────────
def _resolve_subject(camera_id: str, cameras_xlsx: Path) -> str:
    cameras = _load_camera_lookup(cameras_xlsx)
    if camera_id not in cameras:
        raise SystemExit(
            f"camera {camera_id!r} not in {cameras_xlsx.name}"
        )
    return cameras[camera_id]["subject"]


def _resolve_prompt_path(subject: str, rubric_version: str) -> Path:
    """prompts/<subject>/rubric_<subject>_<rubric_version>.md"""
    name = f"rubric_{subject}_{rubric_version}.md"
    p = PROMPTS_DIR / subject / name
    if not p.exists():
        raise SystemExit(f"prompt not found: {p}")
    return p


def _build_run_dir(
    *,
    subject: str,
    started_at: datetime,
    rubric_version: str,
    reasoner: str,
    shape: str,
) -> Path:
    ts = started_at.strftime("%Y-%m-%dT%H%M%S")
    safe_reasoner = reasoner.replace("/", "_")
    config_slug = f"{ts}__{rubric_version}__{safe_reasoner}__{shape}"
    return RUBRIC_RUNS_DIR / subject / config_slug


# ─── Main flow ────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--session-id", required=True,
                   help="e.g. 2026-05-18__D28__0900")
    p.add_argument("--rubric-version", required=True,
                   help="e.g. v1_2026-06-10 — must match a file at "
                        "prompts/<subject>/rubric_<subject>_<version>.md")
    p.add_argument("--shape", choices=["A", "B"], default="A",
                   help="A = Gemini watches video directly; B = text reasoner "
                        "over evidence bundle (deferred to step 9)")
    p.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK,
                   help="Path to the rubric workbook")
    p.add_argument("--reasoner", default=None,
                   help="Override model name (defaults to LLMAdapter's "
                        "vision_model for Shape A)")
    p.add_argument("--cameras-xlsx", type=Path, default=DEFAULT_CAMERAS_XLSX,
                   help="Override the camera config")
    p.add_argument("--force", action="store_true",
                   help="Re-run all stages even if their outputs exist")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan + paths, exit without LLM calls")
    args = p.parse_args()

    started_at = datetime.utcnow().replace(microsecond=0)

    # 1. Resolve subject from session_id + camera config
    _, camera_id, _ = parse_session_id(args.session_id)
    subject = _resolve_subject(camera_id, args.cameras_xlsx)
    log.info(f"session_id={args.session_id} → camera={camera_id} subject={subject}")

    # 2. Resolve prompt path
    prompt_path = _resolve_prompt_path(subject, args.rubric_version)
    log.info(f"prompt: {prompt_path.relative_to(ROOT)}")

    # 3. Compute output dir
    reasoner_label = args.reasoner or "default"
    run_dir = _build_run_dir(
        subject=subject,
        started_at=started_at,
        rubric_version=args.rubric_version,
        reasoner=reasoner_label,
        shape=args.shape,
    )
    log.info(f"run_dir: {run_dir.relative_to(ROOT)}")

    if args.shape == "B":
        log.error("Shape B is not implemented yet (lands in step 9 with the evidence cache)")
        return 2

    if args.dry_run:
        log.info("--dry-run: plan above; skipping LLM calls + file writes")
        return 0

    # 4. Build/reuse the session-video cache (4 stages, all idempotent)
    llm = LLMAdapter()
    log.info("stage A: build_session_video()")
    sva = build_session_video(args.session_id, llm=llm, force=args.force)

    # 5. Load rubric + derive prompt metadata
    log.info("stage B: load_rubric() + derive prompt metadata")
    rubric = load_rubric(args.workbook, subject)
    meta = _derive_trimmed_metadata(
        trimmed=sva.trimmed,
        combined=sva.combined,
        boundaries_path=sva.boundaries_json,
    )
    log.info(
        f"  duration={meta['duration_str']} ({meta['duration_sec']}s), "
        f"wallclock {meta['wallclock_start']} → {meta['wallclock_end']} IST"
    )

    # 6. Render prompt
    log.info("stage C: render_prompt()")
    prompt = render_prompt(
        rubric=rubric, prompt_path=prompt_path, shape=args.shape, **meta,
    )

    # 7. Score
    log.info("stage D: score() — single LLM call")
    t0 = time.time()
    answer_set, raw_response = score(
        rubric=rubric, prompt=prompt, llm=llm,
        session_id=args.session_id,
        rubric_version=args.rubric_version,
        video_path=sva.trimmed,
        shape=args.shape,
        reasoner_model=args.reasoner,
    )
    elapsed = time.time() - t0
    log.info(f"  scored in {elapsed:.1f}s")

    # 8. Persist artifacts
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "4_rendered_prompt.txt").write_text(prompt)
    (run_dir / "5_answers_raw.txt").write_text(raw_response)
    (run_dir / "5_answers.json").write_text(answer_set.model_dump_json(indent=2))

    config = {
        "session_id": args.session_id,
        "subject": subject,
        "rubric_version": args.rubric_version,
        "shape": args.shape,
        "reasoner_model": args.reasoner,
        "workbook": str(args.workbook),
        "prompt_path": str(prompt_path),
        "session_dir": str(sva.session_dir),
        "started_at": started_at.isoformat(),
        "finished_at": datetime.utcnow().replace(microsecond=0).isoformat(),
        "wall_clock_seconds": round(elapsed, 1),
        "prompt_hash": answer_set.prompt_hash,
    }
    (run_dir / "0_config.json").write_text(json.dumps(config, indent=2))

    answered = sum(1 for a in answer_set.answers.values()
                   if not a.insufficient_information)
    insufficient = len(answer_set.answers) - answered
    log.info(
        f"DONE. wrote {run_dir.relative_to(ROOT)} — "
        f"{answered} answered, {insufficient} INSUFFICIENT"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
