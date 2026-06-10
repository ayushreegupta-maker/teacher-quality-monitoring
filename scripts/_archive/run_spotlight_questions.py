"""
Run the 13-question spotlight diagnostic on 3 videos × 3 Gemini models.

10 questions probe a single FOCUS CHILD (anchored to a per-video visual
descriptor) — testing whether the model can re-identify and track one child
across a long video, and interpret learning signals (challenge vs proficiency
= ZPD bookends).

3 questions probe TEACHER COACHING — testing whether the model can deliver
grounded "did well / could improve / missed a moment" feedback that an
Openhouse mentor could act on.

Output:
  data/spotlight_runs/<timestamp>/
    raw_answers/<video_stem>__<model_slug>.json    # one per (video × model)
    raw_answers/<video_stem>__<model_slug>__raw.txt
    answers.csv                                    # consolidated
    run_summary.json
"""

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml

from adapters.llm import LLMAdapter, parse_json_lenient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("spotlight_runner")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.WARNING)


QUESTIONS_YAML = ROOT / "data" / "spotlight_questions" / "questions.yaml"


# ─── Videos (with per-video child descriptor) ────────────────────────────────

DEFAULT_VIDEOS = [
    {
        "label": "Morning circle time",
        "short": "morning_circle",
        "path": ROOT / "data" / "raw" / "20250909_activity_1_Morning_circle_time_activities_and_group_learning.mp4",
        "activity_context": "Morning circle time — a group activity where children sit together for songs, storytelling, and guided discussion led by a teacher.",
        "child_descriptor": "the boy in the red shirt",
    },
    {
        "label": "Balloon dance",
        "short": "balloon_dance",
        "path": ROOT / "data" / "raw" / "20250918_activity_5_balloon_dance.mp4",
        "activity_context": "Balloon dance — children move freely with balloons to music; physical movement activity using balloons as props for dance and play.",
        "child_descriptor": "the girl in the blue denim dress with 2 ponytails, playing with the yellow balloon",
    },
    {
        "label": "Colouring",
        "short": "colouring",
        "path": ROOT / "data" / "raw" / "D06_20250919105616.mp4",
        "activity_context": "Colouring — children get their individual colouring sheets and colour with markers/crayons.",
        "child_descriptor": "the girl in the white dress with 1 ponytail and 4 clips, sitting nearest to the entrance",
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
    mapping = {
        "gemini-2.5-flash": "G2.5-Flash",
        "gemini-2.5-pro": "G2.5-Pro",
        "gemini-3-flash-preview": "G3-Flash-Preview",
        "gemini-3.1-pro-preview": "G3.1-Pro",
        "gemini-3.5-flash": "G3.5-Flash",
    }
    return mapping.get(model_name, model_name)


def format_questions(questions: list[dict]) -> str:
    """Render the 13 questions grouped by category."""
    child = [q for q in questions if q["category"] == "child"]
    teacher = [q for q in questions if q["category"] == "teacher"]
    lines = []
    lines.append("=== CHILD SPOTLIGHT (focus on the one child described above) ===")
    lines.append("")
    for q in child:
        lines.append(f"  {q['id']}. {q['text'].strip()}")
        lines.append("")
    lines.append("=== TEACHER COACHING (about the teacher, not any one child) ===")
    lines.append("")
    for q in teacher:
        lines.append(f"  {q['id']}. {q['text'].strip()}")
        lines.append("")
    return "\n".join(lines)


def build_prompt(template: str, video: dict, questions: list[dict]) -> str:
    """Substitute activity_context, child_descriptor, and questions_list, then
    also literal-substitute every '[CHILD_DESCRIPTOR]' in question text."""
    questions_text = format_questions(questions)
    # Replace [CHILD_DESCRIPTOR] in question text with the actual descriptor
    questions_text = questions_text.replace("[CHILD_DESCRIPTOR]", video["child_descriptor"])
    return (
        template
        .replace("{activity_context}", video["activity_context"])
        .replace("{child_descriptor}", video["child_descriptor"])
        .replace("{questions_list}", questions_text)
    )


def run_one(video: dict, model: str, prompt: str, llm: LLMAdapter, cache_dir: Path) -> tuple[dict, str | None]:
    """Run one (video × model) combo. Idempotent."""
    cache_file = cache_dir / f"{video['short']}__{model_slug(model)}.json"
    if cache_file.exists():
        log.info(f"  → cached: {cache_file.name}")
        return json.loads(cache_file.read_text()), None

    try:
        log.info(f"  → uploading {video['path'].name} (if not already uploaded)…")
        t_up = time.time()
        video_file = llm.upload_video(video["path"])
        log.info(f"  → uploaded in {time.time() - t_up:.0f}s")

        log.info(f"  → calling {model} with 13-question spotlight prompt…")
        t_call = time.time()
        if model.startswith("gemini-2.5"):
            thinking_budget = 0
            max_output_tokens = 15000  # 13 Qs, generous
        else:
            # 3.x requires thinking — pass None for default budget. Allow
            # plenty of headroom since thinking tokens come out of the
            # same output budget.
            thinking_budget = None
            max_output_tokens = 40000

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

        (cache_dir / f"{video['short']}__{model_slug(model)}__raw.txt").write_text(raw)

        parsed = parse_json_lenient(raw)
        if not isinstance(parsed, dict):
            return {}, f"expected dict, got {type(parsed).__name__}"

        answers = {str(k): v for k, v in parsed.items()}
        cache_file.write_text(json.dumps(answers, indent=2, ensure_ascii=False))
        log.info(f"  → saved {cache_file.name}  ({len(answers)} answers)")
        return answers, None
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:300]}"
        log.error(f"  → FAILED: {err}")
        return {}, err


def _flatten_answer_for_cell(raw) -> str:
    if raw == "" or raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        answer = str(raw.get("answer", "")).strip()
        confidence = str(raw.get("confidence", "")).strip()
        evidence = str(raw.get("evidence", "")).strip()
        parts = []
        if answer:
            parts.append(answer)
        meta = []
        if confidence:
            meta.append(f"confidence: {confidence}")
        if evidence:
            meta.append(f"evidence: {evidence}")
        if meta:
            parts.append(f"[{' | '.join(meta)}]")
        return "\n".join(parts)
    return json.dumps(raw, ensure_ascii=False)


def build_csv(
    questions: list[dict],
    results: dict[tuple[str, str], dict],
    videos: list[dict],
    models: list[str],
    csv_path: Path,
) -> None:
    """One row per question; column header includes the focus-child descriptor
    per video so reviewers don't have to keep flipping back to remember which
    child each cell is about."""
    base_headers = ["Q ID", "Category", "Level", "Question (template)"]
    model_headers = []
    for model in models:
        ml = model_short_label(model)
        for video in videos:
            vl = video["label"]
            # Compress the descriptor for the header
            cd = video["child_descriptor"]
            cd_short = (cd[:40] + "…") if len(cd) > 40 else cd
            header_label = f"{ml} | {vl} (focus: {cd_short})"
            model_headers.extend([
                f"{header_label} | Answer",
                f"{header_label} | Score",
                f"{header_label} | Remarks",
            ])
    headers = base_headers + model_headers

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for q in questions:
            row = [q["id"], q["category"], q["level"], q["text"].strip()]
            for model in models:
                for video in videos:
                    answers = results.get((video["label"], model), {})
                    raw = answers.get(q["id"], "")
                    cell = _flatten_answer_for_cell(raw)
                    row.extend([cell, "", ""])
            w.writerow(row)
    log.info(f"Wrote {csv_path}")


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Run the 13-question spotlight diagnostic")
    parser.add_argument(
        "--out-root",
        default=ROOT / "data" / "spotlight_runs" / datetime.now().strftime("%Y-%m-%d_%H%M%S"),
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

    with open(QUESTIONS_YAML) as f:
        data = yaml.safe_load(f)
    template = data["prompt_template"]
    questions = data["questions"]
    log.info(f"Loaded {len(questions)} questions from {QUESTIONS_YAML.name}")

    for video in videos:
        if not video["path"].exists():
            log.error(f"video missing: {video['path']}")
            sys.exit(1)

    log.info(f"Will run {len(videos)} videos × {len(models)} models = {len(videos) * len(models)} combos")
    log.info(f"Models: {models}")
    for v in videos:
        log.info(f"  • {v['label']}  →  focus child: {v['child_descriptor']}")
    log.info(f"Output: {out_dir}")
    log.info("")

    llm = LLMAdapter()
    overall_t0 = time.time()
    results: dict[tuple[str, str], dict] = {}
    combo_status: list[dict] = []

    combo_idx = 0
    total = len(videos) * len(models)
    for video in videos:
        prompt = build_prompt(template, video, questions)
        # Save the rendered prompt per video so it's easy to audit what the
        # model actually saw (descriptor substitution etc.).
        (out_dir / f"prompt__{video['short']}.txt").write_text(prompt)
        for model in models:
            combo_idx += 1
            log.info(f"=== ({combo_idx}/{total}) {video['label']} × {model} ===")
            answers, err = run_one(video, model, prompt, llm, cache_dir)
            results[(video["label"], model)] = answers
            combo_status.append({
                "video": video["label"], "model": model,
                "n_answers": len(answers),
                "expected_keys": [q["id"] for q in questions],
                "missing_keys": [q["id"] for q in questions if q["id"] not in answers],
                "error": err,
            })

    csv_path = out_dir / "answers.csv"
    build_csv(questions, results, videos, models, csv_path)

    total_elapsed = time.time() - overall_t0
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "videos": [
            {"label": v["label"], "path": str(v["path"]), "child_descriptor": v["child_descriptor"]}
            for v in videos
        ],
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
    n_full = sum(1 for s in combo_status if s["error"] is None and not s["missing_keys"])
    log.info(f"Clean runs: {n_full}/{total} combos returned all 13 keys")


if __name__ == "__main__":
    main()
