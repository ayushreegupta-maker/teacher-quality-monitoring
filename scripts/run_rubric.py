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
        [--workbook prompts/rubrics.xlsx] \\
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

from adapters.llm import LLMAdapter
from pipeline.answers_book import (
    compute_run_n,
    init_workbook,
    merge_queue,
    write_sidecar,
)
from pipeline.boundaries import _parse_hms
from pipeline.evidence import build_evidence_bundle
from pipeline.rubric import (
    DEFAULT_SHAPE_B_REASONER,
    load_rubric,
    render_prompt,
    score,
)
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

DEFAULT_WORKBOOK = ROOT / "prompts" / "rubrics.xlsx"
DEFAULT_CAMERAS_XLSX = ROOT / "data" / "cctv_cameras.xlsx"
RUBRIC_RUNS_DIR = ROOT / "data" / "rubric_runs"
PROMPTS_DIR = ROOT / "prompts"
ANSWERS_XLSX = ROOT / "data" / "tqm_answers.xlsx"
ANSWERS_QUEUE_DIR = ROOT / "data" / "_answer_queue"

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


def _resolve_prompt_path(subject: str, rubric_version: str, shape: str) -> Path:
    """Path to the prompt file for this (subject, rubric_version, shape).

    Shape A: prompts/<subject>/rubric_<subject>_<rubric_version>.md
    Shape B: prompts/<subject>/rubric_<subject>_<rubric_version>_shape_b.md
    """
    base = f"rubric_{subject}_{rubric_version}"
    suffix = "_shape_b" if shape == "B" else ""
    p = PROMPTS_DIR / subject / f"{base}{suffix}.md"
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
    # Pick ONE of these two paths to find the trimmed video to score:
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--session-id",
        help="Run the full session_video pipeline (combine → boundary → trim) "
             "from data/raw. Format: <YYYY-MM-DD>__<camera>__<HHMM>, "
             "e.g. 2026-05-18__D28__0900",
    )
    target.add_argument(
        "--trimmed-video", type=Path,
        help="Path to an existing trimmed .mp4. Skips ffmpeg + boundary "
             "detection. Subject + session_id auto-derived from the path: "
             "data/sessions/<subject>/<session_id>/3_trimmed.mp4",
    )
    p.add_argument("--rubric-version", required=True,
                   help="e.g. v1_2026-06-10 — must match a file at "
                        "prompts/<subject>/rubric_<subject>_<version>.md")
    p.add_argument("--shape", choices=["A", "B"], default="A",
                   help="A = Gemini watches video directly; "
                        "B = text reasoner reads cached evidence")
    p.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK,
                   help="Path to the rubric workbook")
    p.add_argument("--reasoner", default=None,
                   help="Override model name. Shape A: defaults to LLMAdapter "
                        f"vision_model. Shape B: defaults to "
                        f"{DEFAULT_SHAPE_B_REASONER}")
    p.add_argument("--cameras-xlsx", type=Path, default=DEFAULT_CAMERAS_XLSX,
                   help="Override the camera config")
    # Shape B: evidence-cache key parameters (vision_model + fps + chunking)
    p.add_argument("--vision-model", default=None,
                   help="(Shape B) Vision model used to build the evidence "
                        "bundle. Defaults to LLMAdapter's vision_model")
    p.add_argument("--vision-fps", type=float, default=None,
                   help="fps Gemini should sample the video at. None = "
                        "Gemini default (~1 fps). Lower this for long videos "
                        "that blow the 1M-input-token ceiling — e.g. 0.5 fps "
                        "for a 90+ min trimmed class. Applies to both Shape A "
                        "(scoring call) and Shape B (vision-pass cache key). "
                        "NOTE: Gemini may silently ignore fps for files "
                        "uploaded via the Files API — prefer --media-resolution low.")
    p.add_argument("--media-resolution", choices=["low", "medium", "high"], default=None,
                   help="Gemini tokens-per-frame: low=66, medium=258 (default), "
                        "high=516. Use 'low' for long videos (>30min) to fit "
                        "under the 1M input-token ceiling. 108min @ default "
                        "(MEDIUM) ≈ 1.67M tokens (over); @ LOW ≈ 428k tokens.")
    p.add_argument("--chunking", default="5min",
                   help="(Shape B) vision-pass chunking: 5min / 10min / single")
    # Methodology-test flags: reproduce earlier pipeline states by toggling
    # off sections of the vision prompt. Default ON = current production.
    # Both flags affect the vision-pass prompt only; reasoner is unchanged.
    p.add_argument("--no-phase-extraction", action="store_true",
                   help="(Shape B) Suppress the phases/explanations/disturbances "
                        "block in the vision prompt. Use to reproduce the pre-"
                        "phase-inline behaviour where the bundle has empty "
                        "phase/explanation/disturbance arrays. Combine with "
                        "--force so the cached bundle is rebuilt.")
    p.add_argument("--no-tightened-rules", action="store_true",
                   help="(Shape B) Suppress the non-overlap rule + the broadened "
                        "Q&A-counts-as-explanation guidance added in the late-"
                        "2026-06-19 prompt tightening. Use to reproduce the "
                        "earlier vision prompt. Combine with --force.")
    # Per-session context fed into the vision-pass prompt (option-c
    # patch). Not part of the evidence-cache key — pass --force when
    # context changes for the same session.
    p.add_argument("--activity-context", default=None,
                   help="Free-form notes the vision pass should know about, "
                        "e.g. 'Week 3 lesson 4: Eric Carle collage'. Goes "
                        "into the vision prompt's `session.activity_context`. "
                        "Recorded on the EvidenceBundle for traceability")
    p.add_argument("--teacher-id", default=None,
                   help="Teacher identifier for this session. Recorded on "
                        "the EvidenceBundle + the accumulator XLSX. Will be "
                        "auto-resolved from teacher_schedule once task #33 "
                        "lands")
    p.add_argument("--force", action="store_true",
                   help="Re-run all stages even if their outputs exist")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan + paths, exit without LLM calls")
    args = p.parse_args()

    started_at = datetime.utcnow().replace(microsecond=0)

    # 1. Resolve subject + session_id from whichever input was given
    if args.trimmed_video:
        # data/sessions/<subject>/<session_id>/3_trimmed.mp4
        tv = args.trimmed_video.resolve()
        if not tv.exists():
            raise SystemExit(f"trimmed video not found: {tv}")
        try:
            sid = tv.parent.name                     # 2026-05-18__D28__0900
            subject = tv.parent.parent.name          # art
            assert tv.parent.parent.parent.name == "sessions", \
                f"expected data/sessions/<subject>/<sid>/, got {tv}"
        except (AssertionError, IndexError) as e:
            raise SystemExit(
                f"--trimmed-video path must be data/sessions/<subject>/"
                f"<session_id>/3_trimmed.mp4; got {tv} ({e})"
            )
        args.session_id = sid
        log.info(f"--trimmed-video → subject={subject} session_id={sid}")
    else:
        _, camera_id, _ = parse_session_id(args.session_id)
        subject = _resolve_subject(camera_id, args.cameras_xlsx)
        log.info(f"session_id={args.session_id} → camera={camera_id} subject={subject}")

    # 2. Resolve prompt path (shape-specific)
    prompt_path = _resolve_prompt_path(subject, args.rubric_version, args.shape)
    log.info(f"prompt: {prompt_path.relative_to(ROOT)}")

    # 3. Compute output dir
    reasoner_label = args.reasoner or (
        DEFAULT_SHAPE_B_REASONER if args.shape == "B" else "default"
    )
    run_dir = _build_run_dir(
        subject=subject,
        started_at=started_at,
        rubric_version=args.rubric_version,
        reasoner=reasoner_label,
        shape=args.shape,
    )
    log.info(f"run_dir: {run_dir.relative_to(ROOT)}")

    if args.dry_run:
        log.info("--dry-run: plan above; skipping LLM calls + file writes")
        return 0

    # 4. Build/reuse the session-video cache (always — even Shape B needs the
    #    trimmed window's metadata + boundaries for the evidence bundle).
    #    Skipped when --trimmed-video is given: the user vouched for the file
    #    and we just construct the artifacts handle from the sibling files.
    llm = LLMAdapter()
    if args.trimmed_video:
        from pipeline.session_video import SessionVideoArtifacts
        sdir = args.trimmed_video.resolve().parent
        log.info(f"--trimmed-video: skipping stage A, using {sdir.relative_to(ROOT)}")
        sva = SessionVideoArtifacts(
            session_id=args.session_id,
            subject=subject,
            session_dir=sdir,
            segments_used=[],
            combined=sdir / "1a_combined.mp4",
            boundary_input=sdir / "1b_boundary_input.mp4",
            boundaries_json=sdir / "2_boundaries.json",
            trimmed=args.trimmed_video.resolve(),
        )
    else:
        log.info("stage A: build_session_video()")
        sva = build_session_video(args.session_id, llm=llm, force=args.force)

    # 5. Load rubric
    log.info("stage B: load_rubric()")
    rubric = load_rubric(args.workbook, subject)

    # 6. Render prompt + score (shape-specific)
    if args.shape == "A":
        meta = _derive_trimmed_metadata(
            trimmed=sva.trimmed, combined=sva.combined,
            boundaries_path=sva.boundaries_json,
        )
        log.info(
            f"  duration={meta['duration_str']} ({meta['duration_sec']}s), "
            f"wallclock {meta['wallclock_start']} → {meta['wallclock_end']} IST"
        )
        log.info("stage C: render_prompt(A)")
        prompt = render_prompt(
            rubric=rubric, prompt_path=prompt_path, shape="A", **meta,
        )
        log.info("stage D: score(A) — single Gemini call")
        t0 = time.time()
        answer_set, raw_response = score(
            rubric=rubric, prompt=prompt, llm=llm,
            session_id=args.session_id,
            rubric_version=args.rubric_version,
            video_path=sva.trimmed,
            shape="A",
            reasoner_model=args.reasoner,
            fps=args.vision_fps,
            media_resolution=args.media_resolution,
        )
    else:  # Shape B
        log.info("stage C-1: load/build evidence bundle")
        bundle = build_evidence_bundle(
            session_id=args.session_id, llm=llm,
            subject=subject,
            vision_model=args.vision_model,
            fps=args.vision_fps,
            chunking=args.chunking,
            activity_context=args.activity_context,
            teacher_id=args.teacher_id,
            force=args.force,
            phase_extraction=not args.no_phase_extraction,
            tightened_rules=not args.no_tightened_rules,
            # When the caller passed --trimmed-video, hand the synthetic sva
            # in so the evidence builder skips re-running combine/boundary
            # detection (it would otherwise re-resolve raw segments).
            sva=sva if args.trimmed_video else None,
        )
        log.info(
            f"  bundle: {len(bundle.transcript)} transcript segs, "
            f"{len(bundle.observations)} observations, "
            f"phases={'+' if bundle.phases else '-'}, "
            f"explanations={'+' if bundle.explanations else '-'}, "
            f"disturbances={'+' if bundle.disturbances else '-'}"
        )
        log.info("stage C-2: render_prompt(B)")
        prompt = render_prompt(
            rubric=rubric, prompt_path=prompt_path, shape="B",
            evidence=bundle,
        )
        log.info("stage D: score(B) — single Claude call")
        t0 = time.time()
        answer_set, raw_response = score(
            rubric=rubric, prompt=prompt, llm=llm,
            session_id=args.session_id,
            rubric_version=args.rubric_version,
            shape="B",
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
        "source_model": answer_set.source_model,
        "workbook": str(args.workbook),
        "prompt_path": str(prompt_path),
        "session_dir": str(sva.session_dir),
        "started_at": started_at.isoformat(),
        "finished_at": datetime.utcnow().replace(microsecond=0).isoformat(),
        "wall_clock_seconds": round(elapsed, 1),
        "prompt_hash": answer_set.prompt_hash,
        # Per-session context fed into the vision pass (option-c).
        # Recorded here for audit even on Shape A (where they're not used
        # — Shape A's rubric prompt has no activity_context hook today).
        "activity_context": args.activity_context,
        "teacher_id": args.teacher_id,
    }
    if args.shape == "B":
        config["vision_model"] = args.vision_model
        config["vision_fps"] = args.vision_fps
        config["chunking"] = args.chunking
    (run_dir / "0_config.json").write_text(json.dumps(config, indent=2))

    # 9. Emit sidecar + merge into the rolling accumulator.
    finished_at = datetime.utcnow().replace(microsecond=0).isoformat()
    run_id = started_at.isoformat()
    config_slug = run_dir.name
    run_n = compute_run_n(
        ANSWERS_XLSX,
        session_id=args.session_id,
        subject=subject,
        rubric_version=args.rubric_version,
        shape=args.shape,
        reasoner=answer_set.source_model,
    )
    init_workbook(ANSWERS_XLSX)  # no-op if it already exists
    write_sidecar(
        ANSWERS_QUEUE_DIR,
        answer_set=answer_set, rubric=rubric, config=config,
        run_id=run_id,
        started_at=started_at.isoformat(),
        finished_at=finished_at,
        wall_clock_seconds=round(elapsed, 1),
        config_slug=config_slug,
        run_n=run_n,
    )
    merge_result = merge_queue(ANSWERS_XLSX, ANSWERS_QUEUE_DIR)
    if merge_result.get("backup_path"):
        log.warning(
            f"accumulator merge FAILED — backup at {merge_result['backup_path']}, "
            "sidecar retained for retry"
        )

    answered = sum(1 for a in answer_set.answers.values()
                   if not a.insufficient_information)
    insufficient = len(answer_set.answers) - answered
    log.info(
        f"DONE. wrote {run_dir.relative_to(ROOT)} — "
        f"{answered} answered, {insufficient} INSUFFICIENT "
        f"(run_n={run_n})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
