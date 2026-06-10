"""
Score a single long classroom recording by:
  1. Detecting class boundaries (when first/last child is visible) via one Gemini call
  2. Extracting 5-min "before" (prepared space) and "after" (aftermath) clips with ffmpeg
  3. Scoring each clip against the playground + toy_design rubrics
  4. Writing a side-by-side before/after report

The "before" score reflects design quality (the space as prepared).
The "after" score reflects design resilience (did the space hold up?).
The delta column shows which dimensions changed under use.

Usage:
    .venv/bin/python score_long_video.py \\
        --video data/raw/D06_20250919105616.mp4 \\
        --activity-name "Morning class 2026-05-20" \\
        --activity-context "Description of the activity setup..." \\
        [--segment-minutes 5] \\
        [--rubrics playground,toy_design]
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
from adapters.sessions import register_session
from pipeline.boundaries import detect_boundaries
from pipeline.extract import (
    check_ffmpeg_available,
    extract_segment,
    hms_to_seconds,
    probe_duration_seconds,
    seconds_to_hms,
)
from pipeline.items import consolidate_items
from pipeline.render import load_rubric
from pipeline.score import score_session
from pipeline.session_resolve import resolve_session_context
from pipeline.types import SessionMeta
from pipeline.vision import vision_observe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("score_long_video")

ROOT = Path(__file__).resolve().parent
SEGMENTS_DIR = ROOT / "data" / "segments"
REPORTS_DIR = ROOT / "data" / "long_video_reports"
RUBRICS_AVAILABLE = {
    "playground": ROOT / "rubric" / "rubric_playground_v0_2.yaml",
    "toy_design": ROOT / "rubric" / "rubric_toy_design_v0_1.yaml",
}


def slugify(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()


def _validate_timestamp(seconds, total_seconds: float, label: str):
    """Validate a parsed boundary timestamp against the actual video duration.

    Returns (valid_seconds_or_None, warning_or_None). Out-of-range timestamps
    (negative, or >5% beyond video end) are treated as detection failures so
    the caller falls back. Slight overshoots (within 5%) are clamped to the
    end of the video with a soft warning.
    """
    if seconds is None:
        return None, None
    if seconds < 0:
        return None, f"{label} timestamp negative ({seconds}s); ignoring"
    grace = total_seconds * 1.05
    if seconds > grace:
        return (
            None,
            f"{label} timestamp ({seconds}s) is beyond video duration "
            f"({total_seconds:.0f}s) by more than 5% — likely hallucinated by the "
            f"boundary model; treating as missing and falling back",
        )
    if seconds > total_seconds:
        return total_seconds, (
            f"{label} timestamp ({seconds}s) slightly past video end "
            f"({total_seconds:.0f}s); clamping to end"
        )
    return float(seconds), None


# How close to a recording edge counts as "at the edge". The model sometimes
# returns 00:00:00 to mean "from the very start" and {duration} to mean "all
# the way through". When a timestamp lands within this many seconds of the
# corresponding edge, we treat it as edge-aligned and use a clean N-min window
# from that edge instead of the asymmetric ±pre/post window.
EDGE_TOLERANCE_SECONDS = 5.0


def _before_window(
    first_s: float | None,
    total_seconds: float,
    before_pre_seconds: int,
    before_post_seconds: int,
) -> tuple[float, float]:
    """Compute the (start, end) of the before-window.

    Three cases:
    - first_s is None        → fall back to the first N min of recording.
    - first_s ≈ 0 (at start) → first N min of recording (no negative pre, no shift).
    - otherwise              → asymmetric: (first_s − pre, first_s + post).
    """
    before_total = before_pre_seconds + before_post_seconds
    if first_s is None or first_s <= EDGE_TOLERANCE_SECONDS:
        return (0.0, min(float(before_total), total_seconds))
    return (
        max(0.0, first_s - before_pre_seconds),
        min(total_seconds, first_s + before_post_seconds),
    )


def _after_window(
    last_s: float | None,
    total_seconds: float,
    after_pre_seconds: int,
    after_post_seconds: int,
) -> tuple[float, float]:
    """Compute the (start, end) of the after-window.

    Three cases:
    - last_s is None              → fall back to the last N min of recording.
    - last_s ≈ duration (at end)  → last N min of recording.
    - otherwise                   → asymmetric: (last_s − pre, last_s + post).
    """
    after_total = after_pre_seconds + after_post_seconds
    if last_s is None or last_s >= total_seconds - EDGE_TOLERANCE_SECONDS:
        return (max(0.0, total_seconds - after_total), total_seconds)
    return (
        max(0.0, last_s - after_pre_seconds),
        min(total_seconds, last_s + after_post_seconds),
    )


def compute_windows(
    boundaries,
    total_seconds: float,
    before_pre_seconds: int,
    before_post_seconds: int,
    after_pre_seconds: int,
    after_post_seconds: int,
) -> dict:
    """Compute (start, end) for the before- and after-windows.

    Asymmetric model: the "before" window straddles first_child_visible_at,
    with `before_pre_seconds` before that moment and `before_post_seconds`
    after. The "after" window straddles last_child_visible_at similarly.

    Layered defence against model misbehaviour:
    1. `_validate_timestamp` rejects values that are negative or beyond the
       recording duration (with 5% grace).
    2. Post-validation here catches malformed model outputs that pass
       _validate_timestamp but don't make semantic sense:
         (a) self_check_passed=False — the model itself flagged its answer as
             uncertain (observed when the model emitted contradictory values
             but knew they were wrong).
         (b) first == last == 0 — model emitted defaults instead of null/null.
         (c) last < first — order violation.
         (d) first invalidated AND last ≈ 0 — one side got nulled by
             _validate_timestamp; the other is a suspiciously-near-zero default
             that the simple "both 0" check misses.
         (e) last_s - first_s shorter than max(before_post, after_pre) — the
             implied class duration is too short for non-overlapping before /
             after windows; the model has either guessed wrong or genuinely
             saw a transient visit, neither of which gives useful evaluation
             windows.
       Any of these reduce both timestamps to None → first/last N min fallback.
    3. `_before_window` / `_after_window` apply edge-aligned defaults when a
       timestamp lands at the start/end of the recording — children visible
       throughout get clean first-5-min / last-5-min windows.
    """
    raw_first = hms_to_seconds(boundaries.first_child_visible_at) if boundaries.first_child_visible_at else None
    raw_last = hms_to_seconds(boundaries.last_child_visible_at) if boundaries.last_child_visible_at else None
    first_s, w1 = _validate_timestamp(raw_first, total_seconds, "first_child")
    last_s, w2 = _validate_timestamp(raw_last, total_seconds, "last_child")
    warnings: list[str] = [w for w in (w1, w2) if w]

    before_total = before_pre_seconds + before_post_seconds
    after_total = after_pre_seconds + after_post_seconds

    # Fix 1: model self-flagged its answer as bad → trust that signal
    if getattr(boundaries, "self_check_passed", None) is False:
        warnings.append(
            "Model returned self_check_passed=False — its own answer is "
            "flagged unreliable; falling back to first "
            f"{before_total // 60} min / last {after_total // 60} min."
        )
        first_s = None
        last_s = None

    # Fix 2 (Post-val a): both 0 → model emitted defaults instead of null/null
    if (first_s is not None and last_s is not None
            and first_s < 1.0 and last_s < 1.0):
        warnings.append(
            "Both timestamps are 00:00:00 — likely the model emitted defaults "
            "instead of null/null; falling back to first "
            f"{before_total // 60} min / last {after_total // 60} min."
        )
        first_s = None
        last_s = None

    # Fix 3 (Post-val b): order violation → treat last as missing
    if first_s is not None and last_s is not None and last_s < first_s:
        warnings.append(
            f"Order violation: last_child ({last_s:.0f}s) < first_child "
            f"({first_s:.0f}s); treating last as missing — after-window will "
            f"use last {after_total // 60} min of recording."
        )
        last_s = None

    # Fix 4 (Post-val c): "first invalidated by _validate_timestamp +
    # last suspiciously near zero" — covers the Colouring wall-clock case where
    # first=00:59:19 (nulled as out-of-range) and last=00:00:00 sneaks through.
    # The simple "both 0" check above misses this because first is already None.
    if first_s is None and last_s is not None and last_s < 30.0:
        warnings.append(
            f"last_child ({last_s:.0f}s) is suspiciously close to recording start "
            f"while first_child was invalidated (likely wall-clock leak); "
            f"treating last as null — after-window will use last "
            f"{after_total // 60} min of recording."
        )
        last_s = None
    # Symmetric defensive case
    if last_s is None and first_s is not None and first_s > total_seconds - 30.0:
        warnings.append(
            f"first_child ({first_s:.0f}s) is suspiciously close to recording end "
            f"while last_child was invalidated; treating first as null — "
            f"before-window will use first {before_total // 60} min of recording."
        )
        first_s = None

    # Fix 5 (Post-val d): implied class duration too short for non-overlapping
    # before/after windows. If last - first < the larger of (before_post,
    # after_pre), the asymmetric windows overlap, which is nonsensical —
    # either the boundaries are wrong or the class was so brief that
    # before/after analysis is meaningless. Fall back to first/last N min.
    min_class_duration = max(before_post_seconds, after_pre_seconds)
    if (first_s is not None and last_s is not None
            and last_s - first_s < min_class_duration):
        warnings.append(
            f"Implied class duration ({last_s - first_s:.0f}s = last_child - "
            f"first_child) is shorter than required for non-overlapping "
            f"windows ({min_class_duration}s); boundary detection is "
            f"implausible — falling back to first {before_total // 60} min / "
            f"last {after_total // 60} min."
        )
        first_s = None
        last_s = None

    # Emit fallback warnings for the genuinely-null cases — but only if no
    # more-specific warning above already explained the fallback.
    _explained_keywords = (
        "00:00:00",            # both-zero
        "Order violation",     # last < first
        "self_check_passed",   # model self-flagged
        "wall-clock leak",     # first invalidated + last≈0
        "Implied class",       # too-short class duration
        "suspiciously close",  # first≈end + last invalidated (symmetric)
    )
    already_explained = any(k in w for w in warnings for k in _explained_keywords)

    if first_s is None and last_s is None and not already_explained:
        warnings.append(
            f"No valid child boundary detected — falling back to first "
            f"{before_total // 60} min / last {after_total // 60} min of recording."
        )
    elif first_s is None and last_s is not None and not already_explained:
        warnings.append(
            f"First-child timestamp invalid — before-window uses first "
            f"{before_total // 60} min of recording."
        )
    elif last_s is None and first_s is not None and not already_explained:
        warnings.append(
            f"Last-child timestamp invalid — after-window uses last "
            f"{after_total // 60} min of recording."
        )

    before = _before_window(first_s, total_seconds, before_pre_seconds, before_post_seconds)
    after = _after_window(last_s, total_seconds, after_pre_seconds, after_post_seconds)

    if before[1] - before[0] < 30:
        warnings.append(f"Before window very short ({before[1] - before[0]:.0f}s)")
    if after[1] - after[0] < 30:
        warnings.append(f"After window very short ({after[1] - after[0]:.0f}s)")

    return {"before": before, "after": after, "warnings": warnings}


async def process_segment(
    segment_label: str,
    clip_path: Path,
    base_meta: SessionMeta,
    rubric_pairs: list,
    llm: LLMAdapter,
    segment_seconds: int,
    session_prefix: str,
    db_context: dict,
) -> dict:
    """Vision + items + scoring for one extracted segment (before or after).

    Each scoring run gets its own DB session row so scores can be persisted
    with a unique (session_id, rubric, dimension) tuple. The vision pass
    itself doesn't get its own DB session — it's an intermediate artifact
    feeding the scoring sessions.

    `session_prefix` should be the video-stem-derived slug used by the parent
    long-video session, so all sub-sessions share a stable, video-unique key.
    """
    vision_session_id = f"long_vision_{session_prefix}_{segment_label}"
    vision_meta = base_meta.model_copy(update={
        "session_id": vision_session_id,
        "duration_minutes": max(1, int(round(segment_seconds / 60))),
        "video_path": clip_path,
    })
    register_session(vision_meta)

    log.info(f"[{segment_label}] vision pass starting")
    try:
        # vision_observe is blocking I/O (Gemini uploads + per-chunk calls).
        # Off-loading to a thread keeps the asyncio event loop free so peer
        # segments / peer videos can make progress in parallel.
        transcript, observations = await asyncio.to_thread(
            vision_observe, vision_meta, llm,
        )
    except Exception as e:
        log.error(f"[{segment_label}] vision pass FAILED: {e!r}")
        return {"items": None, "scores": {label: None for label, _ in rubric_pairs}}
    log.info(
        f"[{segment_label}] vision done: "
        f"{len(transcript.segments)} segments, {len(observations.observations)} observations"
    )

    items = None
    try:
        items = await asyncio.to_thread(
            consolidate_items, vision_meta, observations, llm,
        )
    except Exception as e:
        log.error(f"[{segment_label}] items consolidation failed: {e!r}")

    scores_by_rubric: dict = {}
    for rubric_label, rubric_path in rubric_pairs:
        score_session_id = f"long_{segment_label}_{rubric_label}_{session_prefix}"
        try:
            rubric = load_rubric(rubric_path)
            score_meta = vision_meta.model_copy(update={"session_id": score_session_id})
            register_session(score_meta)

            # Register this scoring sub-session in the DB so the score rows have
            # a parent session_id to FK against.
            db.register_session(
                session_id=score_session_id,
                school_id=base_meta.school_id,
                classroom_id=db_context.get("classroom_id"),
                camera_id=db_context.get("camera_id"),
                recorded_at=db_context.get("recorded_at") or datetime.combine(
                    base_meta.recorded_at, datetime.min.time()
                ),
                video_path=clip_path,
                activity_id=db_context.get("activity_id"),
                activity_context=base_meta.activity_context,
                status="processing",
                duration_seconds=float(segment_seconds),
            )

            log.info(f"[{segment_label}] scoring against {rubric_label}")
            scores = await score_session(score_meta, transcript, observations, rubric, llm)
            scores_by_rubric[rubric_label] = scores
            log.info(f"[{segment_label}] {rubric_label}: overall={scores.overall}")

            # Persist scores into the DB
            try:
                db.save_session_scores(score_session_id, rubric_label, scores)
                db.update_session_status(score_session_id, "scored")
            except Exception as db_err:
                log.warning(
                    f"[{segment_label}] DB write of {rubric_label} scores failed: "
                    f"{db_err!r} (file-based artifacts still saved)"
                )
        except Exception as e:
            log.error(f"[{segment_label}] {rubric_label} FAILED: {e!r}")
            scores_by_rubric[rubric_label] = None
            try:
                db.update_session_status(score_session_id, "failed", error=str(e))
            except Exception:
                pass

    return {"items": items, "scores": scores_by_rubric}


def _score_cell(s) -> str:
    if s is None:
        return "-"
    if s.score == "insufficient_evidence":
        return "ie"
    return str(s.score)


def _delta(before_score, after_score) -> str:
    if before_score is None or after_score is None:
        return "-"
    sb = before_score.score
    sa = after_score.score
    if sb == "insufficient_evidence" and sa == "insufficient_evidence":
        return "—"
    if sb == "insufficient_evidence":
        return f"ie → {sa}"
    if sa == "insufficient_evidence":
        return f"{sb} → ie"
    diff = float(sa) - float(sb)
    if diff == 0:
        return "—"
    arrow = "↑" if diff > 0 else "↓"
    return f"{arrow} {abs(diff):.2f}"


def write_report(
    activity_name: str,
    video_path: Path,
    total_seconds: float,
    boundaries,
    windows: dict,
    results: dict,
    rubric_labels: list,
    out_dir: Path,
) -> None:
    """Write report.md + scores.csv + summary.json with side-by-side before/after."""
    before = results.get("before", {})
    after = results.get("after", {})

    lines = [
        f"# Long Video Report: {activity_name}",
        "",
        f"**Source:** `{video_path.name}`",
        f"**Total duration:** {seconds_to_hms(total_seconds)} ({total_seconds:.0f} sec)",
        f"**Boundary detection:** first child at "
        f"`{boundaries.first_child_visible_at or 'null'}`, "
        f"last child at `{boundaries.last_child_visible_at or 'null'}` "
        f"(confidence: **{boundaries.confidence}**)",
    ]
    if boundaries.notes:
        lines.append(f"**Boundary notes:** {boundaries.notes}")
    bs, be = windows["before"]
    as_, ae = windows["after"]
    lines.append(
        f"**Before window:** `{seconds_to_hms(bs)} – {seconds_to_hms(be)}` "
        f"({be - bs:.0f} sec)"
    )
    lines.append(
        f"**After window:** `{seconds_to_hms(as_)} – {seconds_to_hms(ae)}` "
        f"({ae - as_:.0f} sec)"
    )
    if windows["warnings"]:
        lines.append("")
        lines.append("**Warnings:**")
        for w in windows["warnings"]:
            lines.append(f"- {w}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Side-by-side tables per rubric
    for rubric_label in rubric_labels:
        ss_before = before.get("scores", {}).get(rubric_label)
        ss_after = after.get("scores", {}).get(rubric_label)
        sample = ss_before or ss_after
        lines.append(f"## {rubric_label.replace('_', ' ').title()} — Before vs After")
        lines.append("")
        if sample is None:
            lines.append("_No successful runs for this rubric._")
            lines.append("")
            continue

        dim_ids = list(sample.scores.keys())
        lines.append("| Dimension | Before | After | Delta |")
        lines.append("|---|---|---|---|")
        for d in dim_ids:
            b = ss_before.scores.get(d) if ss_before else None
            a = ss_after.scores.get(d) if ss_after else None
            lines.append(
                f"| {d} | {_score_cell(b)} | {_score_cell(a)} | {_delta(b, a)} |"
            )
        ov_b = f"{ss_before.overall:.2f}" if (ss_before and ss_before.overall is not None) else "-"
        ov_a = f"{ss_after.overall:.2f}" if (ss_after and ss_after.overall is not None) else "-"
        ov_delta = ""
        if (ss_before and ss_after and
                ss_before.overall is not None and ss_after.overall is not None):
            d = ss_after.overall - ss_before.overall
            ov_delta = "—" if d == 0 else f"{'↑' if d > 0 else '↓'} {abs(d):.2f}"
        lines.append(f"| **Overall** | **{ov_b}** | **{ov_a}** | **{ov_delta}** |")
        lines.append("")

    # Per-segment rationale
    lines.append("---")
    lines.append("")
    lines.append("## Per-segment rationale")
    lines.append("")
    for seg_label, seg_data in [("Before", before), ("After", after)]:
        lines.append(f"### {seg_label}")
        lines.append("")
        for rubric_label in rubric_labels:
            ss = seg_data.get("scores", {}).get(rubric_label)
            if ss is None:
                lines.append(f"**{rubric_label}**: FAILED")
                lines.append("")
                continue
            ov = f"{ss.overall:.2f}" if ss.overall is not None else "n/a"
            lines.append(f"**{rubric_label}** — overall {ov}")
            lines.append("")
            for dim_id, s in ss.scores.items():
                lines.append(f"- **{dim_id}**: `{s.score}` ({s.confidence})")
                if s.scorer_notes:
                    notes = s.scorer_notes.replace("\n", " ")
                    if len(notes) > 400:
                        notes = notes[:400] + "…"
                    lines.append(f"    - {notes}")
            lines.append("")

    # Items inventory
    lines.append("---")
    lines.append("")
    lines.append("## Items inventory")
    lines.append("")
    for seg_label, seg_data in [("Before", before), ("After", after)]:
        items = seg_data.get("items")
        lines.append(f"### {seg_label}")
        lines.append("")
        if items is None:
            lines.append("_(items inventory unavailable)_")
            lines.append("")
            continue
        if not items.activity_zone_items:
            lines.append("_(no items identified at activity zone)_")
        else:
            for it in items.activity_zone_items:
                bits = [f"**{it.name}**"]
                if it.category:
                    bits.append(f"_({it.category})_")
                if it.count_or_quantity and it.count_or_quantity != "unknown":
                    bits.append(f"× {it.count_or_quantity}")
                if it.specifics:
                    bits.append(f"— {it.specifics}")
                lines.append("- " + " ".join(bits))
        lines.append("")

    (out_dir / "report.md").write_text("\n".join(lines))

    # CSV
    with open(out_dir / "scores.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "segment", "rubric", "dimension", "score", "confidence",
            "anchor_matched", "scorer_notes",
        ])
        for seg_label, seg_data in [("before", before), ("after", after)]:
            for rubric_label, ss in seg_data.get("scores", {}).items():
                if ss is None:
                    continue
                for dim_id, s in ss.scores.items():
                    w.writerow([
                        seg_label, rubric_label, dim_id, s.score, s.confidence,
                        (s.anchor_matched or "")[:200],
                        (s.scorer_notes or "").replace("\n", " ")[:800],
                    ])

    # JSON dump
    out = {
        "activity_name": activity_name,
        "video": str(video_path),
        "total_seconds": total_seconds,
        "boundaries": json.loads(boundaries.model_dump_json()),
        "windows": {
            "before": [windows["before"][0], windows["before"][1]],
            "after": [windows["after"][0], windows["after"][1]],
            "warnings": windows["warnings"],
        },
        "results": {
            "before": {
                "items": (None if before.get("items") is None
                          else json.loads(before["items"].model_dump_json())),
                "scores": {
                    rl: (None if ss is None else json.loads(ss.model_dump_json()))
                    for rl, ss in before.get("scores", {}).items()
                },
            },
            "after": {
                "items": (None if after.get("items") is None
                          else json.loads(after["items"].model_dump_json())),
                "scores": {
                    rl: (None if ss is None else json.loads(ss.model_dump_json()))
                    for rl, ss in after.get("scores", {}).items()
                },
            },
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(out, indent=2, default=str))


def parse_args():
    p = argparse.ArgumentParser(description="Score a long classroom recording (before vs after)")
    p.add_argument("--video", type=Path, required=True, help="path to the long video file")
    p.add_argument(
        "--activity-name", default=None,
        help="short name for the activity (used in the report filename). "
             "If omitted, resolved from the DB via camera+recorded_at; "
             "falls back to the video stem.",
    )
    p.add_argument(
        "--activity-context", default=None,
        help="Free-form description of the activity setup. "
             "If omitted, resolved from the DB; if DB has no entry, runs without context.",
    )
    p.add_argument(
        "--camera", default=None,
        help="camera id (e.g. D06). If omitted, parsed from the filename "
             "(expects 'CAM_YYYYMMDDHHMMSS' pattern, e.g. 'D06_20250919105616.mp4').",
    )
    p.add_argument(
        "--before-pre", type=float, default=1.0,
        help="minutes BEFORE the first-child timestamp to include in the before-window (default 1)",
    )
    p.add_argument(
        "--before-post", type=float, default=4.0,
        help="minutes AFTER the first-child timestamp to include in the before-window (default 4)",
    )
    p.add_argument(
        "--after-pre", type=float, default=4.0,
        help="minutes BEFORE the last-child timestamp to include in the after-window (default 4)",
    )
    p.add_argument(
        "--after-post", type=float, default=1.0,
        help="minutes AFTER the last-child timestamp to include in the after-window (default 1)",
    )
    p.add_argument(
        "--rubrics",
        default="playground,toy_design",
        help=f"comma-separated rubric labels (available: {','.join(RUBRICS_AVAILABLE)})",
    )
    p.add_argument(
        "--recorded-at", default=None,
        help="YYYY-MM-DD; if omitted, parsed from filename, else defaults to today",
    )
    p.add_argument("--age-range", default="3-5 years")
    return p.parse_args()


async def process_long_video(
    video_path: Path,
    llm: LLMAdapter,
    rubric_pairs: list,
    *,
    activity_name: str | None = None,
    activity_context: str | None = None,
    camera: str | None = None,
    recorded_at: date | None = None,
    age_range: str = "3-5 years",
    before_pre_minutes: float = 1.0,
    before_post_minutes: float = 4.0,
    after_pre_minutes: float = 4.0,
    after_post_minutes: float = 1.0,
) -> dict:
    """End-to-end long-video pipeline: resolve context → boundary detection
    → before/after extraction → score each segment → write report → persist DB.

    Designed to be callable from both the single-video CLI (`main()` below) and
    the batch wrapper (`batch_long_video.py`).

    Returns a summary dict with keys:
        video_path, activity_name, total_seconds, parent_session_id,
        boundaries, windows, results, out_dir, success
    """
    if not video_path.exists():
        log.error(f"video not found: {video_path}")
        return {
            "video_path": str(video_path), "activity_name": activity_name,
            "success": False, "error": "video not found",
        }

    # Resolve camera + recorded_at + activity from CLI args, filename, DB.
    # activity_name (if supplied) doubles as a DB lookup hint when the
    # filename doesn't carry a camera id (e.g. '20250918_activity_5_*.mp4').
    ctx = resolve_session_context(
        video_path=video_path,
        camera_id=camera,
        recorded_at=recorded_at,
        fallback_activity_context=activity_context,
        activity_name_hint=activity_name,
    )
    if activity_context:
        # Explicit caller-provided context always wins
        ctx["activity_context"] = activity_context
        ctx["source"] = "caller_override"

    resolved_activity_name = (
        activity_name
        or ctx.get("activity_name")
        or video_path.stem
    )
    activity_slug = slugify(resolved_activity_name)  # human-readable, for report dir
    # session_prefix is the *stable* identifier for THIS specific video file.
    # All DB session_ids for this long video derive from it, so re-queueing
    # the same file always overwrites the same DB rows. Two different videos
    # of the same activity get distinct session_ids automatically.
    session_prefix = slugify(video_path.stem)

    log.info(f"=== {resolved_activity_name} ({video_path.name}) ===")
    log.info(
        f"[{session_prefix}] resolved: camera={ctx.get('camera_id') or '-'}  "
        f"recorded_date={ctx.get('recorded_date') or '-'}  "
        f"classroom={ctx.get('classroom_name') or '-'}  "
        f"activity_source={ctx['source']}"
    )
    if ctx.get("activity_context"):
        log.info(
            f"[{session_prefix}] activity_context: "
            f"{ctx['activity_context'][:120]}{'…' if len(ctx['activity_context']) > 120 else ''}"
        )

    total_seconds = probe_duration_seconds(video_path)
    log.info(f"[{session_prefix}] video duration: {seconds_to_hms(total_seconds)} ({total_seconds:.1f} sec)")

    recorded_date = ctx.get("recorded_date") or recorded_at or date.today()

    parent_session_id = f"long_full_{session_prefix}"
    base_meta = SessionMeta(
        session_id=parent_session_id,
        recorded_at=recorded_date,
        duration_minutes=max(1, int(round(total_seconds / 60))),
        age_range=age_range,
        subject="play-space design — full recording",
        activity_context=ctx.get("activity_context"),
        classroom_id=str(ctx["classroom_id"]) if ctx.get("classroom_id") else None,
        video_path=video_path.resolve(),
    )
    register_session(base_meta)

    parent_recorded_at = ctx.get("recorded_at") or datetime.combine(recorded_date, datetime.min.time())
    try:
        db.register_session(
            session_id=parent_session_id,
            school_id=base_meta.school_id,
            classroom_id=ctx.get("classroom_id"),
            camera_id=ctx.get("camera_id"),
            recorded_at=parent_recorded_at,
            video_path=video_path.resolve(),
            activity_id=ctx.get("activity_id"),
            activity_context=ctx.get("activity_context"),
            status="processing",
            duration_seconds=total_seconds,
        )
    except Exception as e:
        log.warning(f"[{session_prefix}] DB register_session failed: {e!r}")

    # 1) Boundary detection (blocking Gemini call; off-load so peer long-video
    #    runs can make progress on their own boundaries in parallel)
    boundaries = await asyncio.to_thread(detect_boundaries, base_meta, llm)
    try:
        db.save_boundaries(parent_session_id, boundaries)
    except Exception as e:
        log.warning(f"[{session_prefix}] DB save_boundaries failed: {e!r}")

    # 2) Compute before / after windows
    before_pre_s = int(round(before_pre_minutes * 60))
    before_post_s = int(round(before_post_minutes * 60))
    after_pre_s = int(round(after_pre_minutes * 60))
    after_post_s = int(round(after_post_minutes * 60))
    windows = compute_windows(
        boundaries, total_seconds,
        before_pre_s, before_post_s, after_pre_s, after_post_s,
    )
    log.info(
        f"[{session_prefix}] before window: {windows['before']} "
        f"(pre={before_pre_s}s, post={before_post_s}s around first_child)"
    )
    log.info(
        f"[{session_prefix}] after window: {windows['after']} "
        f"(pre={after_pre_s}s, post={after_post_s}s around last_child)"
    )
    for w in windows["warnings"]:
        log.warning(f"[{session_prefix}] {w}")

    # 3) Extract clips
    segments_dir = SEGMENTS_DIR / video_path.stem
    before_clip = segments_dir / f"before_{int(before_pre_minutes)}pre_{int(before_post_minutes)}post.mp4"
    after_clip = segments_dir / f"after_{int(after_pre_minutes)}pre_{int(after_post_minutes)}post.mp4"
    bs, be = windows["before"]
    as_, ae = windows["after"]
    # Off-load ffmpeg too (blocking subprocess) so peer videos extract in parallel
    await asyncio.gather(
        asyncio.to_thread(extract_segment, video_path, before_clip, bs, be - bs),
        asyncio.to_thread(extract_segment, video_path, after_clip, as_, ae - as_),
    )

    # 4) Process before + after segments in parallel
    segment_specs = [
        ("before", before_clip, windows["before"]),
        ("after", after_clip, windows["after"]),
    ]
    log.info(f"[{session_prefix}] processing {len(segment_specs)} segments in parallel")

    async def _run_segment(label, clip, win):
        seg_seconds = int(round(win[1] - win[0]))
        return label, await process_segment(
            label, clip, base_meta, rubric_pairs, llm,
            seg_seconds, session_prefix, ctx,
        )

    segment_outcomes = await asyncio.gather(
        *[_run_segment(label, clip, win) for label, clip, win in segment_specs],
        return_exceptions=True,
    )

    results: dict = {}
    for outcome in segment_outcomes:
        if isinstance(outcome, Exception):
            log.error(f"[{session_prefix}] segment task crashed: {outcome!r}")
            continue
        label, payload = outcome
        results[label] = payload
    for label, _, _ in segment_specs:
        results.setdefault(label, {"items": None, "scores": {l: None for l, _ in rubric_pairs}})

    # 5) Write report
    rubric_labels = [lbl for lbl, _ in rubric_pairs]
    out_dir = REPORTS_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_{activity_slug}"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_report(
        resolved_activity_name, video_path, total_seconds, boundaries,
        windows, results, rubric_labels, out_dir,
    )

    # 6) Stamp final status + persist data_dir
    any_success = any(
        any(s is not None for s in seg["scores"].values())
        for seg in results.values()
    )
    try:
        db.update_session_status(
            parent_session_id,
            "scored" if any_success else "failed",
            error=None if any_success else "no segment×rubric produced scores",
        )
        with db.db_conn() as conn:
            conn.execute(
                "UPDATE sessions SET data_dir = ? WHERE session_id = ?",
                (str(out_dir), parent_session_id),
            )
    except Exception as e:
        log.warning(f"[{session_prefix}] DB final-status update failed: {e!r}")

    log.info(f"[{session_prefix}] Report saved to: {out_dir}")

    return {
        "video_path": str(video_path),
        "activity_name": resolved_activity_name,
        "activity_slug": activity_slug,
        "session_prefix": session_prefix,
        "total_seconds": total_seconds,
        "parent_session_id": parent_session_id,
        "boundaries": boundaries,
        "windows": windows,
        "results": results,
        "out_dir": out_dir,
        "success": any_success,
    }


async def main():
    args = parse_args()
    if not args.video.exists():
        log.error(f"video not found: {args.video}")
        return

    try:
        db.init_db()
    except Exception as e:
        log.warning(f"DB init failed ({e!r}); proceeding without DB writes")

    rubric_labels = [r.strip() for r in args.rubrics.split(",") if r.strip()]
    unknown = [r for r in rubric_labels if r not in RUBRICS_AVAILABLE]
    if unknown:
        log.error(f"unknown rubric labels: {unknown}; available: {list(RUBRICS_AVAILABLE)}")
        return
    rubric_pairs = [(lbl, RUBRICS_AVAILABLE[lbl]) for lbl in rubric_labels]

    check_ffmpeg_available()
    llm = LLMAdapter()

    explicit_date = date.fromisoformat(args.recorded_at) if args.recorded_at else None

    summary = await process_long_video(
        video_path=args.video,
        llm=llm,
        rubric_pairs=rubric_pairs,
        activity_name=args.activity_name,
        activity_context=args.activity_context,
        camera=args.camera,
        recorded_at=explicit_date,
        age_range=args.age_range,
        before_pre_minutes=args.before_pre,
        before_post_minutes=args.before_post,
        after_pre_minutes=args.after_pre,
        after_post_minutes=args.after_post,
    )

    if summary.get("out_dir"):
        log.info("")
        log.info(f"Report saved to: {summary['out_dir']}")
        log.info(f"  - {summary['out_dir'] / 'report.md'}")
        log.info(f"  - {summary['out_dir'] / 'scores.csv'}")
        log.info(f"  - {summary['out_dir'] / 'summary.json'}")


if __name__ == "__main__":
    asyncio.run(main())
