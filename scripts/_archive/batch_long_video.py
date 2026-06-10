"""
Batch runner for queued long-video sessions. Reads `status='queued'` rows from
the `sessions` table and runs each one through the full before/after pipeline
in parallel.

The DB is the work queue. To add work:
    .venv/bin/python tqm_db.py session queue \\
        --video data/raw/<filename>.mp4 \\
        --activity "<activity name>"

To run the queue:
    .venv/bin/python batch_long_video.py --concurrency 4

Filters:
    --status queued              # default; can also pass 'failed' to retry
    --date 2025-09-19            # only sessions recorded on this date
    --classroom-id 1             # only one classroom
    --limit 10                   # process at most N sessions this run

Each video produces a per-video report in data/long_video_reports/. A
cross-video summary lands in data/batch_long_reports/<timestamp>/.
"""

import argparse
import asyncio
import csv
import json
import logging
from datetime import date, datetime
from pathlib import Path

import adapters.db as db
from adapters.llm import LLMAdapter
from pipeline.extract import check_ffmpeg_available
from score_long_video import RUBRICS_AVAILABLE, process_long_video

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("batch_long_video")

ROOT = Path(__file__).resolve().parent
BATCH_REPORTS_DIR = ROOT / "data" / "batch_long_reports"


def parse_args():
    p = argparse.ArgumentParser(
        description="Process queued long-video sessions from the DB in parallel"
    )
    p.add_argument(
        "--status", default="queued",
        help="status filter (default 'queued'; use 'failed' to retry)",
    )
    p.add_argument(
        "--date", default=None,
        help="YYYY-MM-DD — only process sessions recorded on this date",
    )
    p.add_argument(
        "--classroom-id", type=int, default=None,
        help="only process sessions for this classroom",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="cap the number of sessions processed this run",
    )
    p.add_argument(
        "--rubrics", default="playground,toy_design",
        help=f"comma-separated rubric labels (available: {','.join(RUBRICS_AVAILABLE)})",
    )
    p.add_argument(
        "--concurrency", type=int, default=3,
        help="how many videos to process simultaneously (default 3). "
             "4 is safe on a paid Anthropic tier.",
    )
    p.add_argument(
        "--stagger-seconds", type=int, default=60,
        help="Seconds to wait between starting each video. Spreads out boundary "
             "calls (each can be 400-600K input tokens) to stay under Gemini's "
             "per-minute rate limit. Default 60s. On free tier (250K tokens/min), "
             "you may need 120-180s if videos are 25+ minutes. Set to 0 to disable "
             "for paid-tier accounts.",
    )
    p.add_argument(
        "--before-pre", type=float, default=1.0,
        help="minutes BEFORE first-child timestamp for the before-window (default 1)",
    )
    p.add_argument(
        "--before-post", type=float, default=4.0,
        help="minutes AFTER first-child timestamp for the before-window (default 4)",
    )
    p.add_argument(
        "--after-pre", type=float, default=4.0,
        help="minutes BEFORE last-child timestamp for the after-window (default 4)",
    )
    p.add_argument(
        "--after-post", type=float, default=1.0,
        help="minutes AFTER last-child timestamp for the after-window (default 1)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="list which sessions would be processed, then exit without running",
    )
    return p.parse_args()


def _score_cell(s) -> str:
    if s is None:
        return "-"
    if s.score == "insufficient_evidence":
        return "ie"
    return str(s.score)


def _delta(b, a) -> str:
    if b is None or a is None:
        return "-"
    if b.score == "insufficient_evidence" or a.score == "insufficient_evidence":
        return "—"
    diff = float(a.score) - float(b.score)
    if diff == 0:
        return "—"
    return f"{'↑' if diff > 0 else '↓'} {abs(diff):.2f}"


def write_batch_summary(
    summaries: list[dict],
    rubric_labels: list[str],
    out_dir: Path,
) -> None:
    """Cross-video summary: per-video status + side-by-side scores per rubric."""
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Long-Video Batch Summary",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Videos: {len(summaries)}    Rubrics: {', '.join(rubric_labels)}",
        "",
        "## Per-video status",
        "",
        "| Activity | Video | Duration | Status | Report |",
        "|---|---|---|---|---|",
    ]
    for s in summaries:
        dur = s.get("total_seconds")
        dur_str = f"{int(dur // 60)}:{int(dur % 60):02d}" if dur else "-"
        status = "✓ scored" if s.get("success") else "✗ failed"
        rpt = s.get("out_dir")
        rpt_str = str(Path(rpt).relative_to(ROOT)) if rpt else (s.get("error") or "-")
        lines.append(
            f"| {s.get('activity_name', '-')} | "
            f"`{Path(s.get('video_path', '-')).name}` | "
            f"{dur_str} | {status} | `{rpt_str}` |"
        )
    lines.append("")

    for rubric_label in rubric_labels:
        lines.append(f"## {rubric_label.replace('_', ' ').title()} — All videos")
        lines.append("")

        # find any successful video to extract dim ids
        dim_ids = None
        for s in summaries:
            for seg in ("before", "after"):
                ss = s.get("results", {}).get(seg, {}).get("scores", {}).get(rubric_label)
                if ss is not None:
                    dim_ids = list(ss.scores.keys())
                    break
            if dim_ids is not None:
                break
        if dim_ids is None:
            lines.append("_No video produced scores for this rubric._")
            lines.append("")
            continue

        header = ["Activity"] + [f"{d} (B/A/Δ)" for d in dim_ids] + ["Overall (B/A)"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")

        for s in summaries:
            ss_b = s.get("results", {}).get("before", {}).get("scores", {}).get(rubric_label)
            ss_a = s.get("results", {}).get("after", {}).get("scores", {}).get(rubric_label)
            row = [s.get("activity_name", "-")]
            for d in dim_ids:
                b = ss_b.scores.get(d) if ss_b else None
                a = ss_a.scores.get(d) if ss_a else None
                row.append(f"{_score_cell(b)} / {_score_cell(a)} / {_delta(b, a)}")
            ov_b = f"{ss_b.overall:.2f}" if (ss_b and ss_b.overall is not None) else "-"
            ov_a = f"{ss_a.overall:.2f}" if (ss_a and ss_a.overall is not None) else "-"
            row.append(f"**{ov_b}** / **{ov_a}**")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    (out_dir / "batch_summary.md").write_text("\n".join(lines))

    # CSV
    with open(out_dir / "batch_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "activity_name", "video", "segment", "rubric", "dimension",
            "score", "confidence", "anchor_matched", "scorer_notes",
        ])
        for s in summaries:
            for seg_label, seg in s.get("results", {}).items():
                for rubric_label, ss in seg.get("scores", {}).items():
                    if ss is None:
                        continue
                    for dim_id, ds in ss.scores.items():
                        w.writerow([
                            s.get("activity_name", "-"),
                            Path(s.get("video_path", "-")).name,
                            seg_label, rubric_label, dim_id,
                            ds.score, ds.confidence,
                            (ds.anchor_matched or "")[:200],
                            (ds.scorer_notes or "").replace("\n", " ")[:800],
                        ])

    # JSON
    payload = []
    for s in summaries:
        out_dir_val = s.get("out_dir")
        payload.append({
            "activity_name": s.get("activity_name"),
            "video_path": s.get("video_path"),
            "total_seconds": s.get("total_seconds"),
            "success": s.get("success"),
            "parent_session_id": s.get("parent_session_id"),
            "out_dir": str(out_dir_val) if out_dir_val else None,
            "boundaries": (
                json.loads(s["boundaries"].model_dump_json())
                if s.get("boundaries") else None
            ),
            "windows": s.get("windows"),
            "scores": {
                seg_label: {
                    rl: (json.loads(ss.model_dump_json()) if ss else None)
                    for rl, ss in seg.get("scores", {}).items()
                }
                for seg_label, seg in s.get("results", {}).items()
            },
        })
    (out_dir / "batch_summary.json").write_text(json.dumps(payload, indent=2, default=str))


def fetch_queued_sessions(args) -> list[dict]:
    """Pull sessions matching the CLI filters from the DB."""
    start = date.fromisoformat(args.date) if args.date else None
    end = start  # single-date filter
    rows = db.list_sessions(
        classroom_id=args.classroom_id,
        status=args.status,
        start_date=start,
        end_date=end,
    )
    # Sort: oldest recorded first, so re-runs are deterministic
    rows.sort(key=lambda r: r["recorded_at"])
    if args.limit:
        rows = rows[:args.limit]
    return rows


async def main():
    args = parse_args()
    try:
        db.init_db()
    except Exception as e:
        log.warning(f"DB init failed ({e!r}); proceeding anyway")

    rubric_labels = [r.strip() for r in args.rubrics.split(",") if r.strip()]
    unknown = [r for r in rubric_labels if r not in RUBRICS_AVAILABLE]
    if unknown:
        log.error(f"unknown rubric labels: {unknown}; available: {list(RUBRICS_AVAILABLE)}")
        return
    rubric_pairs = [(lbl, RUBRICS_AVAILABLE[lbl]) for lbl in rubric_labels]

    queued = fetch_queued_sessions(args)
    if not queued:
        log.info(
            f"No sessions match the filter "
            f"(status={args.status!r}, date={args.date}, classroom_id={args.classroom_id})."
        )
        log.info("Queue some work first:")
        log.info(
            "  .venv/bin/python tqm_db.py session queue "
            "--video <path> --activity '<name>'"
        )
        return

    log.info(f"Found {len(queued)} session(s) to process:")
    for r in queued:
        log.info(
            f"  - {r['session_id']:50s}  "
            f"camera={r.get('camera_id') or '-':5}  "
            f"date={r['recorded_at'][:10]}  "
            f"activity={r.get('activity_name') or '-'}  "
            f"video={Path(r['video_path']).name}"
        )

    if args.dry_run:
        log.info("(--dry-run) Stopping without processing.")
        return

    check_ffmpeg_available()
    llm = LLMAdapter()
    concurrency = max(1, args.concurrency)
    stagger = max(0, args.stagger_seconds)
    sem = asyncio.Semaphore(concurrency)
    log.info(
        f"Running with concurrency={concurrency} stagger={stagger}s "
        f"(rubrics={rubric_labels})"
    )
    if stagger > 0:
        log.info(
            f"  → video N starts ~{stagger}s after video N-1 to spread "
            f"Gemini token usage under per-minute limits"
        )

    async def _run_one(idx: int, row: dict) -> dict:
        video_path = Path(row["video_path"])
        label = row.get("activity_name") or video_path.stem
        # Stagger starts so video N waits (N-1) × stagger_seconds before doing
        # anything. Combined with the semaphore, this spreads out the heavy
        # boundary-detection input-token burst without limiting peak parallelism.
        if idx > 1 and stagger > 0:
            wait_secs = (idx - 1) * stagger
            log.info(f"=== ({idx}/{len(queued)}) {label} — staggered start in {wait_secs}s ===")
            await asyncio.sleep(wait_secs)
        async with sem:
            log.info(f"=== ({idx}/{len(queued)}) [START] {label} ===")
            try:
                # Mark as processing so a concurrent run wouldn't pick it up
                db.update_session_status(row["session_id"], "processing")
            except Exception:
                pass
            try:
                # All resolution params come from the DB row — no filename
                # re-parsing needed; the queue command already did that.
                recorded_dt_str = row["recorded_at"]
                recorded_date_obj = date.fromisoformat(recorded_dt_str[:10])
                summary = await process_long_video(
                    video_path=video_path,
                    llm=llm,
                    rubric_pairs=rubric_pairs,
                    activity_name=row.get("activity_name"),
                    activity_context=row.get("activity_context"),
                    camera=row.get("camera_id"),
                    recorded_at=recorded_date_obj,
                    before_pre_minutes=args.before_pre,
                    before_post_minutes=args.before_post,
                    after_pre_minutes=args.after_pre,
                    after_post_minutes=args.after_post,
                )
            except Exception as e:
                log.error(f"=== ({idx}/{len(queued)}) [CRASH] {label}: {e!r} ===")
                summary = {
                    "success": False, "error": repr(e),
                    "activity_name": label, "video_path": str(video_path),
                }
                try:
                    db.update_session_status(row["session_id"], "failed", error=repr(e))
                except Exception:
                    pass
            outcome = "DONE" if summary.get("success") else "FAILED"
            log.info(f"=== ({idx}/{len(queued)}) [{outcome}] {label} ===")
            return summary

    outcomes = await asyncio.gather(
        *[_run_one(i, r) for i, r in enumerate(queued, start=1)],
        return_exceptions=True,
    )

    summaries: list[dict] = []
    for o in outcomes:
        if isinstance(o, Exception):
            log.error(f"gather-level crash: {o!r}")
            summaries.append({"success": False, "error": repr(o)})
        else:
            summaries.append(o)

    out_dir = BATCH_REPORTS_DIR / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    write_batch_summary(summaries, rubric_labels, out_dir)

    ok = sum(1 for s in summaries if s.get("success"))
    log.info("")
    log.info(f"Batch complete: {ok}/{len(summaries)} succeeded")
    log.info(f"Cross-video summary: {out_dir}")
    log.info(f"  - {out_dir / 'batch_summary.md'}")
    log.info(f"  - {out_dir / 'batch_summary.csv'}")
    log.info(f"  - {out_dir / 'batch_summary.json'}")


if __name__ == "__main__":
    asyncio.run(main())
