"""
Batch scorer. Runs all videos in a manifest against the specified rubrics
and writes a consolidated report.

Usage:
    .venv/bin/python batch_score.py
        [--manifest data/raw/playground/batch_manifest.yaml]
        [--rubrics playground,toy_design]

Reads the manifest YAML (one entry per video) and for each video:
  1. Runs ONE vision pass (Gemini) — shared across all rubrics
  2. Scores against each rubric (Claude fan-out)
  3. Collects results

Outputs to data/batch_reports/<timestamp>/:
  - report.md       human-readable tables + per-activity rationale
  - scores.csv      flat data (one row per video × rubric × dimension)
  - summary.json    full structured output
"""

import argparse
import asyncio
import csv
import json
import logging
from datetime import date, datetime
from pathlib import Path

import yaml

import adapters.db as db
from adapters.llm import LLMAdapter
from adapters.sessions import register_session
from pipeline.items import consolidate_items
from pipeline.render import load_rubric
from pipeline.score import score_session
from pipeline.session_context import resolve_session_context
from pipeline.types import SessionMeta
from pipeline.vision import vision_observe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("batch_score")

ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "data" / "raw" / "playground" / "batch_manifest.yaml"
RUBRICS_AVAILABLE = {
    "playground": ROOT / "rubric" / "rubric_playground_v0_2.yaml",
    "toy_design": ROOT / "rubric" / "rubric_toy_design_v0_1.yaml",
}


def slugify(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()


async def process_video(entry: dict, rubric_pairs: list, llm: LLMAdapter) -> dict:
    """Run vision + items consolidation once + score against each rubric.
    Returns {"scores": {rubric_label: SessionScores|None}, "items": ConsolidatedItems|None}."""
    activity_slug = slugify(entry["activity_name"])
    video_path = (ROOT / "data" / "raw" / "playground" / entry["video_file"]).resolve()

    if not video_path.exists():
        log.error(f"video not found: {video_path}")
        return {
            "scores": {label: None for label, _ in rubric_pairs},
            "items": None,
        }

    # Manifest can override the recorded_at; otherwise filename / today
    manifest_recorded_at = entry.get("recorded_at")
    if isinstance(manifest_recorded_at, str):
        manifest_recorded_at = date.fromisoformat(manifest_recorded_at)
    elif not isinstance(manifest_recorded_at, date):
        manifest_recorded_at = None

    # Resolve camera + DB-backed activity context (manifest value still wins
    # for activity_context, since the batch manifest is hand-curated).
    ctx = resolve_session_context(
        video_path=video_path,
        recorded_at=manifest_recorded_at,
        fallback_activity_context=entry.get("activity_context"),
    )
    if entry.get("activity_context"):
        ctx["activity_context"] = entry["activity_context"]
        ctx["source"] = "manifest_override"

    recorded_at = (
        manifest_recorded_at
        or ctx.get("recorded_date")
        or date.today()
    )
    log.info(
        f"[{activity_slug}] resolved: camera={ctx.get('camera_id') or '-'}  "
        f"date={recorded_at}  activity_source={ctx['source']}"
    )

    base_meta = SessionMeta(
        session_id=f"batch_vision_{activity_slug}",
        recorded_at=recorded_at,
        duration_minutes=entry["duration_minutes"],
        age_range=entry.get("age_range", "3-5 years"),
        subject=entry.get("subject", "play-space and toy design evaluation"),
        activity_context=ctx.get("activity_context"),
        classroom_id=str(ctx["classroom_id"]) if ctx.get("classroom_id") else None,
        video_path=video_path,
    )
    register_session(base_meta)

    log.info(f"[{activity_slug}] vision pass starting")
    try:
        # Off-load blocking Gemini I/O so peer videos can run in parallel.
        transcript, observations = await asyncio.to_thread(
            vision_observe, base_meta, llm,
        )
    except Exception as e:
        log.error(f"[{activity_slug}] vision pass FAILED: {e!r}")
        return {
            "scores": {label: None for label, _ in rubric_pairs},
            "items": None,
        }
    log.info(
        f"[{activity_slug}] vision done: {len(transcript.segments)} transcript segs, "
        f"{len(observations.observations)} observations"
    )

    # Consolidate items inventory (Claude call) — used in the toy design report section
    items_inventory = None
    try:
        items_inventory = await asyncio.to_thread(
            consolidate_items, base_meta, observations, llm,
        )
    except Exception as e:
        log.error(f"[{activity_slug}] items consolidation FAILED: {e!r}")

    # The "recorded_at" datetime to persist in the DB. If the manifest gave a
    # date-only, combine with midnight; otherwise prefer the parsed-from-filename
    # datetime.
    db_recorded_at = ctx.get("recorded_at") or datetime.combine(recorded_at, datetime.min.time())

    scores_by_rubric = {}
    for rubric_label, rubric_path in rubric_pairs:
        score_session_id = f"batch_{rubric_label}_{activity_slug}"
        try:
            rubric = load_rubric(rubric_path)
            score_meta = base_meta.model_copy(update={"session_id": score_session_id})
            register_session(score_meta)

            # Register the per-rubric session in the DB so scores have an FK target
            try:
                db.register_session(
                    session_id=score_session_id,
                    school_id=base_meta.school_id,
                    classroom_id=ctx.get("classroom_id"),
                    camera_id=ctx.get("camera_id"),
                    recorded_at=db_recorded_at,
                    video_path=video_path,
                    activity_id=ctx.get("activity_id"),
                    activity_context=base_meta.activity_context,
                    status="processing",
                    duration_seconds=float(entry["duration_minutes"]) * 60,
                )
            except Exception as db_err:
                log.warning(f"[{activity_slug}] DB register_session failed: {db_err!r}")

            log.info(f"[{activity_slug}] scoring against {rubric_label}...")
            scores = await score_session(score_meta, transcript, observations, rubric, llm)
            scores_by_rubric[rubric_label] = scores
            log.info(f"[{activity_slug}] {rubric_label}: overall={scores.overall}")

            try:
                db.save_session_scores(score_session_id, rubric_label, scores)
                db.update_session_status(score_session_id, "scored")
            except Exception as db_err:
                log.warning(
                    f"[{activity_slug}] DB write of {rubric_label} scores failed: "
                    f"{db_err!r} (file artifacts still saved)"
                )
        except Exception as e:
            log.error(f"[{activity_slug}] {rubric_label} FAILED: {e!r}")
            scores_by_rubric[rubric_label] = None
            try:
                db.update_session_status(score_session_id, "failed", error=str(e))
            except Exception:
                pass

    return {"scores": scores_by_rubric, "items": items_inventory}


def _score_cell(s) -> str:
    if s is None:
        return "-"
    if s.score == "insufficient_evidence":
        return "ie"
    return str(s.score)


def _render_items_block(items) -> list:
    """Markdown lines for the items inventory section under toy_design."""
    lines = ["**Consolidated items at activity zone** (from vision observations):", ""]
    if items is None:
        lines.append("_(items inventory unavailable for this video)_")
        lines.append("")
        return lines
    if not items.activity_zone_items:
        lines.append("_(none identified)_")
    else:
        for it in items.activity_zone_items:
            bits = [f"**{it.name}**"]
            if it.category:
                bits.append(f"_({it.category})_")
            qty = it.count_or_quantity
            if qty and qty != "unknown":
                bits.append(f"× {qty}")
            if it.specifics:
                bits.append(f"— {it.specifics}")
            lines.append("- " + " ".join(bits))
    if items.other_items_in_room:
        lines.append("")
        lines.append("Other items observed elsewhere in the room (not at activity zone):")
        for it in items.other_items_in_room:
            loc = f" — {it.location}" if it.location else ""
            lines.append(f"- {it.name}{loc}")
    if items.notes:
        lines.append("")
        lines.append(f"_Notes: {items.notes}_")
    lines.append("")
    return lines


def write_markdown_report(results: dict, rubric_labels: list, out_dir: Path) -> None:
    lines = [
        "# Batch Scoring Report",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Activities: {len(results)}    Rubrics: {', '.join(rubric_labels)}",
        "",
        "Note: `ie` = insufficient_evidence (excluded from the Overall average).",
        "",
    ]

    for rubric_label in rubric_labels:
        sample = next(
            (r["scores"][rubric_label] for r in results.values() if r["scores"].get(rubric_label) is not None),
            None,
        )
        lines.append(f"## {rubric_label.replace('_', ' ').title()} — Summary")
        lines.append("")
        if sample is None:
            lines.append("_No successful runs._")
            lines.append("")
            continue

        dim_ids = list(sample.scores.keys())
        lines.append("| Activity | " + " | ".join(dim_ids) + " | Overall |")
        lines.append("|" + "|".join(["---"] * (len(dim_ids) + 2)) + "|")
        for activity, payload in results.items():
            ss = payload["scores"].get(rubric_label)
            if ss is None:
                cells = ["FAILED"] * (len(dim_ids) + 1)
            else:
                cells = [_score_cell(ss.scores.get(d)) for d in dim_ids]
                cells.append(f"{ss.overall:.2f}" if ss.overall is not None else "-")
            lines.append(f"| {activity} | " + " | ".join(cells) + " |")
        lines.append("")

    lines.append("## Per-activity detail")
    lines.append("")
    for activity, payload in results.items():
        lines.append(f"### {activity}")
        lines.append("")
        for rubric_label in rubric_labels:
            ss = payload["scores"].get(rubric_label)
            if ss is None:
                lines.append(f"**{rubric_label}**: FAILED")
                lines.append("")
                continue
            ov = f"{ss.overall:.2f}" if ss.overall is not None else "n/a"
            lines.append(f"**{rubric_label}** — overall {ov}")
            lines.append("")
            for dim_id, s in ss.scores.items():
                lines.append(f"- **{dim_id}**: `{s.score}` ({s.confidence})")
                if s.anchor_matched:
                    lines.append(f"    - anchor: {s.anchor_matched}")
                if s.scorer_notes:
                    notes = s.scorer_notes.replace("\n", " ")
                    if len(notes) > 500:
                        notes = notes[:500] + "…"
                    lines.append(f"    - rationale: {notes}")
            lines.append("")
            # Surface the consolidated items inventory specifically under toy_design
            if rubric_label == "toy_design":
                lines.extend(_render_items_block(payload.get("items")))

    (out_dir / "report.md").write_text("\n".join(lines))


def write_csv(results: dict, out_dir: Path) -> None:
    with open(out_dir / "scores.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["activity", "rubric", "dimension", "score", "confidence",
                    "anchor_matched", "scorer_notes"])
        for activity, payload in results.items():
            for rubric_label, ss in payload["scores"].items():
                if ss is None:
                    continue
                for dim_id, s in ss.scores.items():
                    w.writerow([
                        activity, rubric_label, dim_id, s.score, s.confidence,
                        (s.anchor_matched or "")[:200],
                        (s.scorer_notes or "").replace("\n", " ")[:1000],
                    ])

    # Separate CSV for the items inventory (one row per activity-zone item)
    with open(out_dir / "items_inventory.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["activity", "category", "name", "count_or_quantity",
                    "specifics", "first_seen_at"])
        for activity, payload in results.items():
            items = payload.get("items")
            if items is None:
                continue
            for it in items.activity_zone_items:
                w.writerow([
                    activity, it.category, it.name,
                    it.count_or_quantity or "",
                    (it.specifics or "")[:300],
                    it.first_seen_at or "",
                ])


def write_json(results: dict, out_dir: Path) -> None:
    out = {}
    for activity, payload in results.items():
        out[activity] = {
            "scores": {},
            "items": None,
        }
        for rubric_label, ss in payload["scores"].items():
            out[activity]["scores"][rubric_label] = (
                None if ss is None else json.loads(ss.model_dump_json())
            )
        if payload.get("items") is not None:
            out[activity]["items"] = json.loads(payload["items"].model_dump_json())
    (out_dir / "summary.json").write_text(json.dumps(out, indent=2, default=str))


def parse_args():
    p = argparse.ArgumentParser(description="Batch score videos against multiple rubrics")
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument(
        "--rubrics",
        default="playground,toy_design",
        help=f"comma-separated rubric labels (available: {','.join(RUBRICS_AVAILABLE)})",
    )
    p.add_argument(
        "--concurrency", type=int, default=3,
        help="how many videos to process simultaneously (default 3). "
             "Raise carefully: each concurrent video does its own Gemini upload + "
             "vision pass + Claude scoring fan-out, so Anthropic/Gemini rate limits "
             "are the practical ceiling. 10 should be safe on a paid Anthropic tier.",
    )
    return p.parse_args()


async def main():
    args = parse_args()
    if not args.manifest.exists():
        log.error(f"manifest not found: {args.manifest}")
        return
    manifest = yaml.safe_load(args.manifest.read_text())

    # Ensure DB is initialised; pipelines still run if this fails.
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

    videos = manifest.get("videos", [])
    if not videos:
        log.error("manifest has no `videos` list")
        return
    log.info(f"manifest: {len(videos)} videos, rubrics: {rubric_labels}")

    llm = LLMAdapter()
    concurrency = max(1, args.concurrency)
    sem = asyncio.Semaphore(concurrency)
    log.info(
        f"running {len(videos)} videos with concurrency={concurrency} "
        f"(rubrics={rubric_labels})"
    )

    async def _run_one(idx: int, entry: dict):
        activity = entry.get("activity_name", entry.get("video_file", "?"))
        async with sem:
            log.info(f"=== ({idx}/{len(videos)}) [START] {activity} ===")
            try:
                payload = await process_video(entry, rubric_pairs, llm)
            except Exception as e:
                log.error(f"=== ({idx}/{len(videos)}) [CRASH] {activity}: {e!r} ===")
                payload = {
                    "scores": {label: None for label, _ in rubric_pairs},
                    "items": None,
                }
            log.info(f"=== ({idx}/{len(videos)}) [DONE]  {activity} ===")
            return activity, payload

    outcomes = await asyncio.gather(
        *[_run_one(i, entry) for i, entry in enumerate(videos, start=1)],
        return_exceptions=True,
    )

    all_results: dict = {}
    for outcome in outcomes:
        if isinstance(outcome, Exception):
            log.error(f"task crashed at the gather layer: {outcome!r}")
            continue
        activity, payload = outcome
        all_results[activity] = payload

    out_dir = ROOT / "data" / "batch_reports" / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    write_markdown_report(all_results, rubric_labels, out_dir)
    write_csv(all_results, out_dir)
    write_json(all_results, out_dir)

    log.info("")
    log.info(f"Report saved to: {out_dir}")
    log.info(f"  - {out_dir / 'report.md'}")
    log.info(f"  - {out_dir / 'scores.csv'}")
    log.info(f"  - {out_dir / 'summary.json'}")


if __name__ == "__main__":
    asyncio.run(main())
