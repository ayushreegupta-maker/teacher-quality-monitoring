"""
Run the 91-question diagnostic bank against multiple Gemini models on multiple
videos.

Loads questions.yaml, builds a single per-video prompt (with activity context
and the "INSUFFICIENT INFORMATION" instruction), and calls each chosen Gemini
model once per video. Saves raw answers + builds a consolidated CSV where
each row is a question and the columns expand to (model × video × {Answer,
Score, Remarks}).

Output:
  data/question_bank_runs/<timestamp>/
    raw_answers/<video_stem>__<model_slug>.json    # one per (video × model)
    answers.csv                                    # consolidated, score/remarks blank
    run_summary.json
"""

import argparse
import csv
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml

from adapters.llm import LLMAdapter, parse_json_lenient
from adapters.sessions import register_session
from pipeline.types import SessionMeta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("question_bank_runner")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.WARNING)


QUESTIONS_YAML = ROOT / "data" / "question_bank" / "questions.yaml"


# ─── Defaults (overridable via CLI) ─────────────────────────────────────────

DEFAULT_VIDEOS = [
    {
        "label": "Morning circle time",
        "short": "morning_circle",
        "path": ROOT / "data" / "raw" / "20250909_activity_1_Morning_circle_time_activities_and_group_learning.mp4",
        "activity_context": "Morning circle time — a group activity where children sit together for songs, storytelling, and guided discussion led by a teacher.",
    },
    {
        "label": "Balloon dance",
        "short": "balloon_dance",
        "path": ROOT / "data" / "raw" / "20250918_activity_5_balloon_dance.mp4",
        "activity_context": "Balloon dance — children move freely with balloons to music; physical movement activity using balloons as props for dance and play.",
    },
    {
        "label": "Colouring",
        "short": "colouring",
        "path": ROOT / "data" / "raw" / "D06_20250919105616.mp4",
        "activity_context": "Colouring — children get their individual colouring sheets and colour with markers/crayons.",
    },
]

DEFAULT_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.1-pro-preview",
    "gemini-3.5-flash",
]


# ─── Helpers ────────────────────────────────────────────────────────────────

def model_slug(model_name: str) -> str:
    return model_name.replace("/", "_").replace(".", "-")


def model_short_label(model_name: str) -> str:
    """Compact label for CSV headers."""
    mapping = {
        "gemini-2.5-flash": "G2.5-Flash",
        "gemini-2.5-pro": "G2.5-Pro",
        "gemini-3-flash-preview": "G3-Flash-Preview",
        "gemini-3.1-pro-preview": "G3.1-Pro",
        "gemini-3.5-flash": "G3.5-Flash",
    }
    return mapping.get(model_name, model_name)


def flatten_questions(data: dict) -> list[dict]:
    """Flatten YAML structure into a list of {id, text, level, bucket, sub_bucket}."""
    out = []
    for bucket in data["buckets"]:
        bucket_label = f"{bucket['id']}. {bucket['name']}"
        for sb in bucket["sub_buckets"]:
            sub_label = f"{sb['id']}. {sb['name']}"
            for q in sb["questions"]:
                out.append({
                    "id": q["id"],
                    "text": q["text"],
                    "level": q["level"],
                    "bucket": bucket_label,
                    "sub_bucket": sub_label,
                })
    return out


def format_questions_with_signposting(data: dict) -> str:
    """Render the questions block with bucket + sub-bucket headers, e.g.:

      === BUCKET 1: The Space (12 questions) ===

      A. Layout & Boundary
        1. Is there a clearly defined activity zone, and what defines it visually?
        2. Can a child intuitively understand where to enter the activity...
        ...

      === BUCKET 2: The Activity Design (13 questions) ===
      ...

    Giving the model thematic grouping helps it reason coherently within a
    bucket instead of treating each question as isolated.
    """
    lines = []
    for bucket in data["buckets"]:
        n = sum(len(sb["questions"]) for sb in bucket["sub_buckets"])
        lines.append(f"\n=== BUCKET {bucket['id']}: {bucket['name']} ({n} questions) ===\n")
        for sb in bucket["sub_buckets"]:
            lines.append(f"{sb['id']}. {sb['name']}")
            for q in sb["questions"]:
                lines.append(f"  {q['id']}. {q['text']}")
            lines.append("")
    return "\n".join(lines)


def build_prompt(template: str, activity_context: str, data: dict) -> str:
    questions_list = format_questions_with_signposting(data)
    return template.replace("{activity_context}", activity_context).replace("{questions_list}", questions_list)


def run_one(video: dict, model: str, prompt: str, llm: LLMAdapter, cache_dir: Path) -> tuple[dict, str | None]:
    """Run one (video × model) combo. Returns (answers_dict, error_str_or_None).

    Idempotent: returns cached answers if the JSON already exists.
    """
    cache_file = cache_dir / f"{video['short']}__{model_slug(model)}.json"
    if cache_file.exists():
        log.info(f"  → cached: {cache_file.name}")
        return json.loads(cache_file.read_text()), None

    try:
        log.info(f"  → uploading {video['path'].name} (if not already uploaded)…")
        t_up = time.time()
        video_file = llm.upload_video(video["path"])
        log.info(f"  → uploaded in {time.time() - t_up:.0f}s")

        log.info(f"  → calling {model} with 91-question prompt…")
        t_call = time.time()
        # Per-model `thinking_budget`:
        # - Gemini 2.5: pass 0 to DISABLE thinking (otherwise it silently eats
        #   the output token budget and truncates JSON output).
        # - Gemini 3.x: thinking is REQUIRED — passing 0 returns
        #   `Budget 0 is invalid. This model only works in thinking mode.`
        #   Pass None to let the model use its default thinking budget.
        if model.startswith("gemini-2.5"):
            thinking_budget = 0
            max_output_tokens = 30000   # tight cap on runaway-tail risk
        else:
            # 3.x: thinking is on. Give a generous output budget since the
            # thinking tokens come out of this same budget.
            thinking_budget = None
            max_output_tokens = 65536

        raw = llm.call_gemini_video(
            prompt=prompt,
            video_file=video_file,
            model_name=model,
            max_output_tokens=max_output_tokens,
            temperature=0.0,
            force_json=True,
            thinking_budget=thinking_budget,
        )
        log.info(f"  → response in {time.time() - t_call:.0f}s ({len(raw)} chars)")

        # Save raw response for debugging
        (cache_dir / f"{video['short']}__{model_slug(model)}__raw.txt").write_text(raw)

        parsed = parse_json_lenient(raw)
        if not isinstance(parsed, dict):
            return {}, f"expected dict, got {type(parsed).__name__}"

        # Coerce all keys to strings (some models may return ints)
        answers = {str(k): v for k, v in parsed.items()}

        cache_file.write_text(json.dumps(answers, indent=2, ensure_ascii=False))
        log.info(f"  → saved {cache_file.name}  ({len(answers)} answers)")
        return answers, None
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:300]}"
        log.error(f"  → FAILED: {err}")
        return {}, err


def build_csv(
    questions: list[dict],
    results: dict[tuple[str, str], dict],
    videos: list[dict],
    models: list[str],
    csv_path: Path,
) -> None:
    """Build the consolidated CSV.

    Column layout:
      base: S No | Bucket | Sub-category | Question | Question Type
      per model (3 cols × 3 videos = 9 cols per model):
        {model} - {video_label} Answer | Score | Remarks
    """
    base_headers = ["S No", "Bucket", "Sub-category", "Question", "Question Type"]
    model_headers = []
    for model in models:
        ml = model_short_label(model)
        for video in videos:
            vl = video["label"]
            model_headers.extend([
                f"{ml} | {vl} | Answer",
                f"{ml} | {vl} | Score",
                f"{ml} | {vl} | Remarks",
            ])
    headers = base_headers + model_headers

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for q in questions:
            row = [
                q["id"], q["bucket"], q["sub_bucket"], q["text"], q["level"],
            ]
            for model in models:
                for video in videos:
                    answers = results.get((video["label"], model), {})
                    raw = answers.get(str(q["id"]), "")
                    cell = _flatten_answer_for_cell(raw)
                    row.extend([cell, "", ""])  # Answer, Score (blank), Remarks (blank)
            writer.writerow(row)
    log.info(f"Wrote {csv_path}")


def _flatten_answer_for_cell(raw) -> str:
    """Convert the structured {answer, confidence, evidence} dict into a single
    cell-friendly string. If the model returned a plain string (older format
    or refusal), pass through. If empty, return empty."""
    if raw == "" or raw is None:
        return ""
    if isinstance(raw, str):
        # Plain-string answer (legacy or refusal)
        return raw
    if isinstance(raw, dict):
        answer = str(raw.get("answer", "")).strip()
        confidence = str(raw.get("confidence", "")).strip()
        evidence = str(raw.get("evidence", "")).strip()
        parts = []
        if answer:
            parts.append(answer)
        meta_bits = []
        if confidence:
            meta_bits.append(f"confidence: {confidence}")
        if evidence:
            meta_bits.append(f"evidence: {evidence}")
        if meta_bits:
            parts.append(f"[{' | '.join(meta_bits)}]")
        return "\n".join(parts)
    # Fallback: JSON-stringify
    return json.dumps(raw, ensure_ascii=False)


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Run the 91-question diagnostic bank")
    parser.add_argument(
        "--out-root",
        default=ROOT / "data" / "question_bank_runs" / datetime.now().strftime("%Y-%m-%d_%H%M%S"),
        type=Path,
    )
    parser.add_argument(
        "--models", default=",".join(DEFAULT_MODELS),
        help="Comma-separated Gemini models to test",
    )
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    videos = DEFAULT_VIDEOS

    out_dir: Path = args.out_root
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "raw_answers"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Load YAML
    with open(QUESTIONS_YAML) as f:
        data = yaml.safe_load(f)
    template = data["prompt_template"]
    questions = flatten_questions(data)
    log.info(f"Loaded {len(questions)} questions from {QUESTIONS_YAML.name}")

    # Verify all videos exist
    for video in videos:
        if not video["path"].exists():
            log.error(f"video missing: {video['path']}")
            sys.exit(1)

    log.info(f"Will run {len(videos)} videos × {len(models)} models = {len(videos) * len(models)} combos")
    log.info(f"Models: {models}")
    log.info(f"Videos: {[v['label'] for v in videos]}")
    log.info(f"Output: {out_dir}")
    log.info("")

    llm = LLMAdapter()
    overall_t0 = time.time()
    results: dict[tuple[str, str], dict] = {}
    combo_status: list[dict] = []

    combo_idx = 0
    total = len(videos) * len(models)
    for video in videos:
        prompt = build_prompt(template, video["activity_context"], data)
        for model in models:
            combo_idx += 1
            log.info(f"=== ({combo_idx}/{total}) {video['label']} × {model} ===")
            answers, err = run_one(video, model, prompt, llm, cache_dir)
            results[(video["label"], model)] = answers
            combo_status.append({
                "video": video["label"], "model": model,
                "n_answers": len(answers),
                "error": err,
            })

    # Build the consolidated CSV
    csv_path = out_dir / "answers.csv"
    build_csv(questions, results, videos, models, csv_path)

    # Run summary
    total_elapsed = time.time() - overall_t0
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "videos": [{"label": v["label"], "path": str(v["path"])} for v in videos],
        "models": models,
        "questions_loaded": len(questions),
        "combos_run": len(results),
        "total_elapsed_seconds": total_elapsed,
        "combo_status": combo_status,
        "csv": str(csv_path),
    }
    (out_dir / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str))

    log.info("")
    log.info(f"Done in {total_elapsed/60:.1f} min")
    log.info(f"CSV:      {csv_path}")
    log.info(f"Summary:  {out_dir / 'run_summary.json'}")
    n_ok = sum(1 for s in combo_status if s["error"] is None and s["n_answers"] >= 80)
    log.info(f"Success rate: {n_ok}/{total} combos returned ≥80 answers cleanly")


if __name__ == "__main__":
    main()
