"""
Model-comparison runner: tests every (vision_model × reasoning_model) combo
on each of the 7 short videos, against both rubrics (playground + toy_design).

Architecture
------------
- 7 videos × 3 vision models × 4 reasoning models = 84 combos.
- Vision output is CACHED per (video × vision_model) — 21 vision calls only,
  not 84. The cached vision JSON is then re-used across all reasoning models.
- Scoring is 5 dims × 2 rubrics per combo = 10 reasoning calls. Run in parallel
  via asyncio.gather.
- Idempotent: skips any combo whose result file already exists. Re-run after
  a crash and it resumes.
- Results go to data/model_comparison/<timestamp>/

Outputs per combo:
  data/model_comparison/<timestamp>/scores/
    {video_stem}__{vision_model_slug}__{reasoning_model_slug}__{rubric}.json
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

# Ensure project root on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml

from adapters.llm import LLMAdapter, prompt_hash
from adapters.sessions import register_session
from pipeline.render import (
    load_prompt,
    load_rubric,
    render_score_prompt,
    split_system_user,
)
from pipeline.types import (
    DimensionScore,
    Rubric,
    SessionMeta,
    Transcript,
    VisualObservations,
)
from pipeline.vision import vision_observe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("compare_models")

# Quieten Gemini SDK's verbose retry logger
logging.getLogger("google_genai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


# ─── Configuration ──────────────────────────────────────────────────────────

VISION_MODELS = [
    "gemini-2.5-flash",       # current baseline
    "gemini-2.5-pro",          # more accurate
    "gemini-3-flash-preview",  # newer Flash generation
]

REASONING_MODELS = [
    "claude-sonnet-4-6",  # current baseline
    "claude-opus-4",       # more accurate (~5× cost)
    "gpt-4o",              # different vendor
    "gemini-2.5-pro",      # multimodal-as-text
]

RUBRICS = {
    "playground": ROOT / "rubric" / "rubric_playground_v0_2.yaml",
    "toy_design": ROOT / "rubric" / "rubric_toy_design_v0_1.yaml",
}

MANIFEST_PATH = ROOT / "data" / "raw" / "playground" / "batch_manifest.yaml"
RAW_DIR = ROOT / "data" / "raw" / "playground"


# ─── Helpers ────────────────────────────────────────────────────────────────

def slugify(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s).strip("_").lower()


def dispatch_score_call(
    llm: LLMAdapter,
    reasoning_model: str,
    system: str,
    user: str,
    schema_class,
) -> DimensionScore:
    """Dispatch a single scoring call to the right vendor based on model name.

    This is the per-call switch between Claude / OpenAI / Gemini-as-text. The
    underlying adapter methods all return a validated schema_class instance.
    """
    if reasoning_model.startswith("claude"):
        return llm.call_claude_json(
            system=system, user=user, schema=schema_class,
            model_name=reasoning_model,
        )
    elif reasoning_model.startswith("gpt"):
        return llm.call_openai_json(
            system=system, user=user, schema=schema_class,
            model_name=reasoning_model,
        )
    elif reasoning_model.startswith("gemini"):
        return llm.call_gemini_text_json(
            system=system, user=user, schema=schema_class,
            model_name=reasoning_model,
        )
    else:
        raise ValueError(f"Unknown reasoning model: {reasoning_model!r}")


async def score_one_dim_with_model(
    dim_id: str,
    rubric: Rubric,
    session: SessionMeta,
    transcript: Transcript,
    observations: VisualObservations,
    llm: LLMAdapter,
    reasoning_model: str,
) -> tuple[str, DimensionScore | None, str | None]:
    """Score one dimension with the specified reasoning model.

    Returns (dim_id, score_or_None, error_string_or_None).
    """
    dimension = rubric.get_dimension(dim_id)
    rendered = render_score_prompt(dimension, rubric, session, transcript, observations)
    system, user = split_system_user(rendered)
    try:
        score = await asyncio.to_thread(
            dispatch_score_call,
            llm, reasoning_model, system, user, DimensionScore,
        )
        score.prompt_hash = prompt_hash(load_prompt("score_dimension"))
        return dim_id, score, None
    except Exception as e:
        return dim_id, None, f"{type(e).__name__}: {str(e)[:200]}"


def get_or_compute_vision(
    video_path: Path,
    vision_model: str,
    activity_context: str,
    duration_minutes: int,
    cache_dir: Path,
    video_stem: str,
) -> tuple[Transcript, VisualObservations] | tuple[None, None]:
    """Get cached vision output for (video, vision_model), or compute + cache."""
    cache_file = cache_dir / f"{video_stem}__{slugify(vision_model)}.json"
    if cache_file.exists():
        log.info(f"  → cached vision for {video_stem} / {vision_model}")
        data = json.loads(cache_file.read_text())
        transcript = Transcript.model_validate(data["transcript"])
        observations = VisualObservations.model_validate(data["observations"])
        return transcript, observations

    log.info(f"  → computing vision for {video_stem} / {vision_model}…")
    # Build a one-shot LLMAdapter pinned to this vision model. Cheap to
    # instantiate; lets vision_observe use the right model without a global flag.
    llm = LLMAdapter(vision_model=vision_model)
    meta = SessionMeta(
        session_id=f"compare_vision_{video_stem}_{slugify(vision_model)}",
        recorded_at=date.today(),
        duration_minutes=duration_minutes,
        activity_context=activity_context,
        video_path=video_path,
    )
    register_session(meta)
    try:
        t0 = time.time()
        transcript, observations = vision_observe(meta, llm)
        elapsed = time.time() - t0
        log.info(
            f"    vision done in {elapsed:.0f}s — "
            f"{len(transcript.segments)} transcript segs, "
            f"{len(observations.observations)} observations"
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "transcript": json.loads(transcript.model_dump_json()),
            "observations": json.loads(observations.model_dump_json()),
        }, indent=2))
        return transcript, observations
    except Exception as e:
        log.error(f"    vision FAILED: {type(e).__name__}: {str(e)[:200]}")
        return None, None


async def score_one_combo(
    video_stem: str,
    vision_model: str,
    reasoning_model: str,
    rubric_name: str,
    rubric: Rubric,
    session: SessionMeta,
    transcript: Transcript,
    observations: VisualObservations,
    llm: LLMAdapter,
    scores_dir: Path,
) -> dict:
    """Run scoring for one (video × vision × reasoning × rubric) combo.

    Idempotent: skips if the result file already exists. Persists immediately
    on success.
    """
    out_file = scores_dir / (
        f"{video_stem}__"
        f"{slugify(vision_model)}__"
        f"{slugify(reasoning_model)}__"
        f"{rubric_name}.json"
    )
    if out_file.exists():
        return {"status": "skipped (cached)", "file": str(out_file.name)}

    dim_ids = [d.id for d in rubric.all_dimensions()]
    t0 = time.time()
    results = await asyncio.gather(
        *[
            score_one_dim_with_model(
                d_id, rubric, session, transcript, observations,
                llm, reasoning_model,
            )
            for d_id in dim_ids
        ],
        return_exceptions=False,
    )
    elapsed = time.time() - t0

    scores: dict = {}
    errors: dict = {}
    for d_id, score, err in results:
        if err:
            errors[d_id] = err
        else:
            scores[d_id] = json.loads(score.model_dump_json())

    payload = {
        "video_stem": video_stem,
        "vision_model": vision_model,
        "reasoning_model": reasoning_model,
        "rubric_name": rubric_name,
        "rubric_version": rubric.version,
        "scores": scores,
        "errors": errors,
        "elapsed_seconds": elapsed,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    scores_dir.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(payload, indent=2))
    return {"status": "OK", "n_scores": len(scores), "n_errors": len(errors), "elapsed": elapsed}


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Multi-model scoring comparison")
    parser.add_argument(
        "--out-root",
        default=ROOT / "data" / "model_comparison" / datetime.now().strftime("%Y-%m-%d_%H%M%S"),
        type=Path,
        help="Output root directory (default: timestamped under data/model_comparison/)",
    )
    parser.add_argument(
        "--vision-models", default=",".join(VISION_MODELS),
        help="Comma-separated vision model list",
    )
    parser.add_argument(
        "--reasoning-models", default=",".join(REASONING_MODELS),
        help="Comma-separated reasoning model list",
    )
    parser.add_argument(
        "--limit-videos", type=int, default=None,
        help="Process only the first N videos (for testing)",
    )
    args = parser.parse_args()

    vision_models = [m.strip() for m in args.vision_models.split(",") if m.strip()]
    reasoning_models = [m.strip() for m in args.reasoning_models.split(",") if m.strip()]

    out_root: Path = args.out_root
    out_root.mkdir(parents=True, exist_ok=True)
    vision_cache_dir = out_root / "vision_outputs"
    scores_dir = out_root / "scores"

    # Load manifest
    manifest = yaml.safe_load(MANIFEST_PATH.read_text())
    videos = manifest["videos"]
    if args.limit_videos:
        videos = videos[:args.limit_videos]

    # Load rubrics once
    rubric_objs = {name: load_rubric(path) for name, path in RUBRICS.items()}

    log.info(f"Configuration:")
    log.info(f"  videos: {len(videos)}")
    log.info(f"  vision_models: {vision_models}")
    log.info(f"  reasoning_models: {reasoning_models}")
    log.info(f"  rubrics: {list(RUBRICS)}")
    log.info(f"  output: {out_root}")
    log.info(f"  TOTAL combos: "
             f"{len(videos)} × {len(vision_models)} × {len(reasoning_models)} × {len(RUBRICS)} = "
             f"{len(videos) * len(vision_models) * len(reasoning_models) * len(RUBRICS)}")
    log.info("")

    # Shared LLMAdapter (vision_model swapped per call via instance creation
    # inside get_or_compute_vision; reasoning model passed per-call)
    llm = LLMAdapter()

    overall_t0 = time.time()
    combo_results: list[dict] = []

    for v_idx, entry in enumerate(videos, start=1):
        video_file = entry["video_file"]
        video_path = (RAW_DIR / video_file).resolve()
        if not video_path.exists():
            log.error(f"video not found: {video_path}")
            continue
        video_stem = slugify(video_path.stem)
        activity_name = entry.get("activity_name", video_stem)
        duration_minutes = entry["duration_minutes"]
        activity_context = entry.get("activity_context")
        log.info(f"=== ({v_idx}/{len(videos)}) {activity_name} — {video_file} ===")

        for vm_idx, vision_model in enumerate(vision_models, start=1):
            log.info(f"  vision model ({vm_idx}/{len(vision_models)}): {vision_model}")
            transcript, observations = get_or_compute_vision(
                video_path=video_path,
                vision_model=vision_model,
                activity_context=activity_context or "",
                duration_minutes=duration_minutes,
                cache_dir=vision_cache_dir,
                video_stem=video_stem,
            )
            if transcript is None:
                log.error(f"    skipping all reasoning combos for {video_stem}/{vision_model} (vision failed)")
                continue

            # Build a per-combo SessionMeta for the score prompt rendering
            base_session = SessionMeta(
                session_id=f"compare_{video_stem}_{slugify(vision_model)}",
                recorded_at=date.today(),
                duration_minutes=duration_minutes,
                activity_context=activity_context,
                video_path=video_path,
            )

            for rm_idx, reasoning_model in enumerate(reasoning_models, start=1):
                log.info(f"    reasoning model ({rm_idx}/{len(reasoning_models)}): {reasoning_model}")
                for rubric_name, rubric in rubric_objs.items():
                    result = await score_one_combo(
                        video_stem=video_stem,
                        vision_model=vision_model,
                        reasoning_model=reasoning_model,
                        rubric_name=rubric_name,
                        rubric=rubric,
                        session=base_session,
                        transcript=transcript,
                        observations=observations,
                        llm=llm,
                        scores_dir=scores_dir,
                    )
                    label = f"{video_stem}/{slugify(vision_model)}/{slugify(reasoning_model)}/{rubric_name}"
                    log.info(f"      {rubric_name}: {result.get('status')}")
                    combo_results.append({"label": label, **result})

    total_elapsed = time.time() - overall_t0
    summary = {
        "out_root": str(out_root),
        "videos": [v["video_file"] for v in videos],
        "vision_models": vision_models,
        "reasoning_models": reasoning_models,
        "rubrics": list(RUBRICS),
        "total_combos": len(combo_results),
        "total_elapsed_seconds": total_elapsed,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "combos": combo_results,
    }
    (out_root / "run_summary.json").write_text(json.dumps(summary, indent=2))
    log.info("")
    log.info(f"Run summary: {out_root / 'run_summary.json'}")
    log.info(f"Total wall time: {total_elapsed/60:.1f} min")


if __name__ == "__main__":
    asyncio.run(main())
