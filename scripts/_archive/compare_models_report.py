"""
Build a comparison report from a compare_models.py run.

Reads:
- data/model_comparison/<timestamp>/scores/*.json (one per video×vision×reasoning×rubric)
- team_scores DB table (populated by scripts/import_team_scores.py)

Writes (into the same run directory):
- comparison_report.md      — human-readable summary with the key tables
- comparison_scores.csv     — flat data: one row per (video, dim, model_combo, rater)
- rmse_summary.csv          — RMSE per (vision_model, reasoning_model, rubric, dim) vs each rater

Sections in the markdown report:
  1. Quick verdict — which model combo wins overall (lowest RMSE vs Pari)
  2. RMSE per dimension per model vs each rater
  3. Pairwise agreement matrix (which models agree most)
  4. Per-video drill-down (every model's score side-by-side)
"""

import argparse
import csv
import json
import logging
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import adapters.db as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("compare_report")


# Map from team-score session_id (e.g. "manifest_floorpainting") to the
# video_stem slug used by the comparison runner.
def team_session_id(video_stem: str) -> str:
    return f"manifest_{video_stem}"


def to_float_or_none(s):
    """Parse a score value; return None if it's "insufficient_evidence" or non-numeric."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s) if not isinstance(s, bool) else None
    if isinstance(s, str):
        s_clean = s.strip().lower()
        if s_clean in ("insufficient_evidence", "ie", "n/a", ""):
            return None
        try:
            return float(s_clean)
        except ValueError:
            return None
    return None


def load_combo_scores(run_dir: Path) -> dict:
    """Load every scored JSON in {run_dir}/scores/ into a nested dict.

    Returns: results[video_stem][vision_model][reasoning_model][rubric] = {dim: score}
    """
    scores_dir = run_dir / "scores"
    results: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    for f in scores_dir.glob("*.json"):
        d = json.loads(f.read_text())
        v = d["video_stem"]
        vm = d["vision_model"]
        rm = d["reasoning_model"]
        rb = d["rubric_name"]
        dim_scores = {
            dim_id: to_float_or_none(s.get("score"))
            for dim_id, s in d.get("scores", {}).items()
        }
        results[v][vm][rm][rb] = dim_scores
    return results


def load_team_scores() -> dict:
    """Load team_scores from the DB into a nested dict.

    Returns: team[video_stem][rubric][dim][rater] = score
    """
    team: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, rater_name, rubric_name, dimension, score FROM team_scores"
        ).fetchall()
    for r in rows:
        if not r["session_id"].startswith("manifest_"):
            continue
        v = r["session_id"].removeprefix("manifest_")
        score = to_float_or_none(r["score"])
        if score is not None:
            team[v][r["rubric_name"]][r["dimension"]][r["rater_name"]] = score
    return team


def compute_rmse(values: list[tuple[float, float]]) -> float | None:
    """Given a list of (predicted, target) pairs, compute RMSE. None if empty."""
    if not values:
        return None
    sq = [(p - t) ** 2 for p, t in values]
    return (sum(sq) / len(sq)) ** 0.5


def compute_mae(values: list[tuple[float, float]]) -> float | None:
    """Mean absolute error. Sometimes more interpretable than RMSE on 0/0.5/1 scales."""
    if not values:
        return None
    return sum(abs(p - t) for p, t in values) / len(values)


# ─── Report sections ────────────────────────────────────────────────────────


def section_rmse_per_combo(model_results, team_scores) -> tuple[list[dict], str]:
    """Compute (vision_model, reasoning_model) → RMSE/MAE/n vs each rater.

    Returns (rows_for_csv, markdown_table).
    """
    rows = []  # for CSV: one row per (vm, rm, rubric, rater)
    md_lines = ["## RMSE per (vision × reasoning) combo, by rater", "", "Lower = closer to that rater. Computed across all 7 videos × dimensions where both model and rater scored.", ""]
    raters = ["Akshay", "Pari", "Ayesha"]
    rubrics = sorted({rb for v in model_results.values() for vm in v.values() for rm in vm.values() for rb in rm.keys()})

    for rubric in rubrics:
        md_lines.append(f"### Rubric: {rubric}")
        md_lines.append("")
        md_lines.append("| Vision model | Reasoning model | vs Akshay (RMSE) | vs Pari (RMSE) | vs Ayesha (RMSE) | n pairs |")
        md_lines.append("|---|---|---|---|---|---|")

        # Gather combos
        all_combos = sorted({(vm, rm) for v in model_results.values() for vm, by_rm in v.items() for rm in by_rm.keys()})
        for vm, rm in all_combos:
            rater_pairs = {r: [] for r in raters}
            for video, by_vm in model_results.items():
                if vm not in by_vm:
                    continue
                if rm not in by_vm[vm]:
                    continue
                if rubric not in by_vm[vm][rm]:
                    continue
                dim_scores = by_vm[vm][rm][rubric]
                for dim_id, model_score in dim_scores.items():
                    if model_score is None:
                        continue
                    for r in raters:
                        target = team_scores.get(video, {}).get(rubric, {}).get(dim_id, {}).get(r)
                        if target is None:
                            continue
                        rater_pairs[r].append((model_score, target))

            n_pairs = max(len(p) for p in rater_pairs.values()) if rater_pairs else 0
            rmse_vals = {r: compute_rmse(rater_pairs[r]) for r in raters}
            mae_vals = {r: compute_mae(rater_pairs[r]) for r in raters}

            def fmt(v):
                return f"{v:.3f}" if v is not None else "—"

            md_lines.append(
                f"| `{vm}` | `{rm}` | "
                f"{fmt(rmse_vals['Akshay'])} | {fmt(rmse_vals['Pari'])} | {fmt(rmse_vals['Ayesha'])} | "
                f"{n_pairs} |"
            )
            for r in raters:
                rows.append({
                    "rubric": rubric,
                    "vision_model": vm,
                    "reasoning_model": rm,
                    "rater": r,
                    "n_pairs": len(rater_pairs[r]),
                    "rmse": rmse_vals[r],
                    "mae": mae_vals[r],
                })
        md_lines.append("")

    return rows, "\n".join(md_lines)


def section_per_video_drilldown(model_results, team_scores) -> str:
    """Per-video table: every dimension × every model combo + every rater."""
    lines = ["## Per-video drill-down", "", "Each cell shows the model's score. Right-most columns show team raters' scores for the same (video, dim).", ""]
    for video in sorted(model_results):
        lines.append(f"### {video}")
        lines.append("")
        # Collect all dimensions across all rubrics
        rubrics_in_video = set()
        for vm in model_results[video].values():
            for rm in vm.values():
                rubrics_in_video.update(rm.keys())

        for rubric in sorted(rubrics_in_video):
            lines.append(f"**Rubric: {rubric}**")
            lines.append("")
            # Header: combos as columns, dimensions as rows
            combos = sorted({(vm, rm) for vm, by_rm in model_results[video].items() for rm in by_rm if rubric in model_results[video][vm].get(rm, {})})
            if not combos:
                lines.append("_(no scores)_")
                lines.append("")
                continue

            # Determine all dimensions present
            all_dims = set()
            for vm, rm in combos:
                all_dims.update(model_results[video][vm][rm][rubric].keys())
            # add team-rated dims too
            all_dims.update(team_scores.get(video, {}).get(rubric, {}).keys())
            dim_ids = sorted(all_dims)

            header_combos = " | ".join(f"{vm[:8]}+{rm[:10]}" for vm, rm in combos)
            lines.append(f"| Dimension | {header_combos} | Akshay | Pari | Ayesha |")
            lines.append("|" + "|".join(["---"] * (len(combos) + 4)) + "|")
            for d in dim_ids:
                row = [d]
                for vm, rm in combos:
                    v = model_results[video][vm][rm].get(rubric, {}).get(d)
                    row.append(f"{v:.2f}" if isinstance(v, (int, float)) else "—")
                for r in ["Akshay", "Pari", "Ayesha"]:
                    v = team_scores.get(video, {}).get(rubric, {}).get(d, {}).get(r)
                    row.append(f"{v:.2f}" if isinstance(v, (int, float)) else "—")
                lines.append("| " + " | ".join(row) + " |")
            lines.append("")
    return "\n".join(lines)


def section_pairwise_agreement(model_results) -> str:
    """How often do (vm_a, rm_a) and (vm_b, rm_b) agree on the same (video, dim) score?

    Agreement = exact same score (within 0.05). Reported as % of dimensions
    where both combos produced a score.
    """
    # Flatten into a dict combo_key → {(video, rubric, dim) → score}
    by_combo: dict = defaultdict(dict)
    for video, by_vm in model_results.items():
        for vm, by_rm in by_vm.items():
            for rm, by_rb in by_rm.items():
                for rb, by_dim in by_rb.items():
                    for d, s in by_dim.items():
                        if s is None:
                            continue
                        by_combo[(vm, rm)][(video, rb, d)] = s

    combos = sorted(by_combo.keys())
    lines = ["## Pairwise model-combo agreement", "", "% of shared (video, dimension) pairs where two combos gave the same score (within 0.05).", ""]
    lines.append("| Combo | " + " | ".join(f"{vm[:6]}+{rm[:8]}" for vm, rm in combos) + " |")
    lines.append("|" + "|".join(["---"] * (len(combos) + 1)) + "|")
    for vm_a, rm_a in combos:
        row = [f"`{vm_a[:6]}+{rm_a[:8]}`"]
        for vm_b, rm_b in combos:
            if (vm_a, rm_a) == (vm_b, rm_b):
                row.append("—")
                continue
            shared_keys = set(by_combo[(vm_a, rm_a)]) & set(by_combo[(vm_b, rm_b)])
            if not shared_keys:
                row.append("—")
                continue
            agree = sum(
                1 for k in shared_keys
                if abs(by_combo[(vm_a, rm_a)][k] - by_combo[(vm_b, rm_b)][k]) <= 0.05
            )
            pct = 100.0 * agree / len(shared_keys)
            row.append(f"{pct:.0f}%")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return "\n".join(lines)


def section_quick_verdict(rmse_rows) -> str:
    """Pick the best combo by mean RMSE vs Pari (the recommended anchor rater)."""
    if not rmse_rows:
        return ""
    # Aggregate per combo across both rubrics, anchor on Pari
    by_combo: dict = defaultdict(list)
    for r in rmse_rows:
        if r["rater"] == "Pari" and r["rmse"] is not None:
            by_combo[(r["vision_model"], r["reasoning_model"])].append(r["rmse"])
    if not by_combo:
        return ""
    means = [(k, statistics.mean(v)) for k, v in by_combo.items()]
    means.sort(key=lambda x: x[1])

    lines = ["## Quick verdict — closest to Pari (anchor rater)", ""]
    lines.append("| Rank | Vision model | Reasoning model | Mean RMSE vs Pari |")
    lines.append("|---|---|---|---|")
    for rank, ((vm, rm), score) in enumerate(means, start=1):
        marker = " 🏆" if rank == 1 else ""
        lines.append(f"| {rank}{marker} | `{vm}` | `{rm}` | {score:.3f} |")
    lines.append("")
    return "\n".join(lines)


# ─── Main ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Build comparison report from a compare_models run")
    parser.add_argument(
        "--run-dir", type=Path, required=True,
        help="Path to the data/model_comparison/<timestamp>/ directory",
    )
    args = parser.parse_args()
    run_dir: Path = args.run_dir
    if not run_dir.exists():
        log.error(f"Run dir not found: {run_dir}")
        sys.exit(1)

    log.info(f"Loading combo scores from {run_dir}/scores/…")
    model_results = load_combo_scores(run_dir)
    log.info(f"  → {len(model_results)} videos")

    log.info("Loading team scores from DB…")
    team_scores = load_team_scores()
    log.info(f"  → {len(team_scores)} videos with team scores")

    # Compute report sections
    log.info("Computing RMSE per combo per rater…")
    rmse_rows, rmse_md = section_rmse_per_combo(model_results, team_scores)
    verdict_md = section_quick_verdict(rmse_rows)
    log.info("Computing pairwise agreement…")
    agreement_md = section_pairwise_agreement(model_results)
    log.info("Computing per-video drill-down…")
    drilldown_md = section_per_video_drilldown(model_results, team_scores)

    # Build markdown report
    md = [
        "# Model Comparison Report",
        "",
        f"Run directory: `{run_dir}`",
        f"Videos: {len(model_results)}",
        "",
        "**How to read this report:** ",
        "- RMSE / MAE compare each model combo's scores against the human raters (Akshay = strict, Pari = moderate/anchor, Ayesha = generous).",
        "- Lower RMSE = closer agreement with that rater.",
        "- Pari is the recommended anchor — middle-ground rater per the calibration notes.",
        "",
        verdict_md,
        rmse_md,
        agreement_md,
        drilldown_md,
    ]
    report_file = run_dir / "comparison_report.md"
    report_file.write_text("\n".join(md))
    log.info(f"Wrote {report_file}")

    # Write CSVs
    csv_file = run_dir / "rmse_summary.csv"
    with open(csv_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["rubric", "vision_model", "reasoning_model", "rater", "n_pairs", "rmse", "mae"])
        writer.writeheader()
        for r in rmse_rows:
            writer.writerow(r)
    log.info(f"Wrote {csv_file}")

    # Flat scores CSV
    flat_csv = run_dir / "comparison_scores.csv"
    with open(flat_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["video", "rubric", "dimension", "vision_model", "reasoning_model", "model_score", "rater", "rater_score"])
        for video, by_vm in model_results.items():
            for vm, by_rm in by_vm.items():
                for rm, by_rb in by_rm.items():
                    for rb, by_dim in by_rb.items():
                        for d, s in by_dim.items():
                            if s is None:
                                continue
                            # For each rater that scored this (video, rb, d), write a row
                            raters_for_dim = team_scores.get(video, {}).get(rb, {}).get(d, {})
                            if not raters_for_dim:
                                writer.writerow([video, rb, d, vm, rm, s, "", ""])
                            else:
                                for rater, target in raters_for_dim.items():
                                    writer.writerow([video, rb, d, vm, rm, s, rater, target])
    log.info(f"Wrote {flat_csv}")


if __name__ == "__main__":
    main()
