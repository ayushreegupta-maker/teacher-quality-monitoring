"""
One-shot backfill: copy everything in data/tqm_answers.xlsx into Supabase
(runs + answers tables). Idempotent — upserts by primary key.

After this runs, the dashboard can stop reading the xlsx and start reading
from Supabase. The pipeline's run_rubric.py is migrated separately.

Run from the repo root:
    .venv/bin/python scripts/backfill_supabase.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "data" / "tqm_answers.xlsx"

SUBJECT_SHEETS = ("Art", "Public Speaking", "Robotics")
SUBJECT_FROM_SHEET = {
    "Art": "art",
    "Public Speaking": "public_speaking",
    "Robotics": "robotics",
}


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"missing env var {name!r}")
    return v


def _to_py(v):
    """Coerce pandas/openpyxl values into plain JSON-able Python primitives."""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if hasattr(v, "isoformat"):  # date / datetime
        return v.isoformat()
    if isinstance(v, (int, float, bool, str)):
        return v
    return str(v)


def _to_bool(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def _to_int(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return int(v)
    except Exception:
        return None


def _to_float(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except Exception:
        return None


def main():
    _load_env(ROOT / ".env")
    url = _require("SUPABASE_URL")
    key = _require("SUPABASE_SERVICE_KEY")
    if not XLSX.exists():
        sys.exit(f"missing {XLSX}")

    from supabase import create_client
    sb = create_client(url, key)

    # ── Backfill runs ────────────────────────────────────────────────────
    runs_df = pd.read_excel(XLSX, sheet_name="Runs")
    print(f"Backfilling {len(runs_df)} run rows...")

    runs_rows = []
    for _, r in runs_df.iterrows():
        row = {
            "run_id": _to_py(r.get("run_id")),
            "session_id": _to_py(r.get("session_id")),
            "subject": _to_py(r.get("subject")),
            "config_slug": _to_py(r.get("config_slug")),
            "rubric_version": _to_py(r.get("rubric_version")),
            "vision_model": _to_py(r.get("vision_model")),
            "shape": _to_py(r.get("shape")),
            "reasoner": _to_py(r.get("reasoner")),
            "run_n": _to_int(r.get("run_n")),
            "started_at": _to_py(r.get("started_at")),
            "finished_at": _to_py(r.get("finished_at")),
            "wall_clock_seconds": _to_float(r.get("wall_clock_seconds")),
            "questions_answered": _to_int(r.get("questions_answered")),
            "questions_insufficient": _to_int(r.get("questions_insufficient")),
            "prompt_hash": _to_py(r.get("prompt_hash")),
            "cost_usd_estimate": _to_float(r.get("cost_usd_estimate")),
            "evidence_bundle_path": _to_py(r.get("evidence_bundle_path")),
            "trimmed_video_duration_seconds": _to_float(r.get("trimmed_video_duration_seconds")),
            "boundaries_detected": _to_bool(r.get("boundaries_detected")),
        }
        # materials_seen: parse JSON column → real list for jsonb column
        ms_raw = r.get("materials_seen_json")
        if isinstance(ms_raw, str) and ms_raw.strip():
            try:
                row["materials_seen"] = json.loads(ms_raw)
            except Exception:
                row["materials_seen"] = None
        else:
            row["materials_seen"] = None
        # session_date + camera derived from session_id when present
        sid = row["session_id"] or ""
        try:
            parts = sid.split("__")
            row["session_date"] = parts[0]
            row["camera"] = parts[1] if len(parts) > 1 else None
        except Exception:
            row["session_date"] = None
            row["camera"] = None
        # Skip rows without a primary key
        if not row["run_id"]:
            continue
        runs_rows.append(row)

    # Batched upserts to stay under request-size limits
    BATCH = 200
    for i in range(0, len(runs_rows), BATCH):
        chunk = runs_rows[i:i + BATCH]
        sb.table("runs").upsert(chunk, on_conflict="run_id").execute()
        print(f"  runs: upserted {i + len(chunk)}/{len(runs_rows)}")

    # ── Backfill answers (all three subject sheets, concatenated) ──────────
    all_answer_rows = []
    for sheet in SUBJECT_SHEETS:
        subj = SUBJECT_FROM_SHEET[sheet]
        try:
            df = pd.read_excel(XLSX, sheet_name=sheet)
        except Exception:
            continue
        print(f"Reading {sheet}: {len(df)} rows")
        for _, r in df.iterrows():
            run_id = _to_py(r.get("run_id"))
            qid = _to_py(r.get("question_id"))
            if not run_id or not qid:
                continue
            all_answer_rows.append({
                "run_id": run_id,
                "question_id": qid,
                "session_id": _to_py(r.get("session_id")),
                "subject": subj,
                "section": _to_py(r.get("section")),
                "question_text": _to_py(r.get("question_text")),
                "answer": _to_py(r.get("answer")),
                "confidence": _to_py(r.get("confidence")),
                "evidence_timestamps": _to_py(r.get("evidence_timestamps")),
                "rationale": _to_py(r.get("rationale")),
                "insufficient_information": _to_bool(r.get("insufficient_information")),
                "had_evidence": _to_bool(r.get("had_evidence")),
                "evidence_parse_ok": _to_bool(r.get("evidence_parse_ok")),
                "answer_type": _to_py(r.get("answer_type")),
                "answer_type_valid": _to_bool(r.get("answer_type_valid")),
            })

    print(f"\nBackfilling {len(all_answer_rows)} answer rows...")
    for i in range(0, len(all_answer_rows), BATCH):
        chunk = all_answer_rows[i:i + BATCH]
        sb.table("answers").upsert(chunk, on_conflict="run_id,question_id").execute()
        print(f"  answers: upserted {i + len(chunk)}/{len(all_answer_rows)}")

    print("\n✓ backfill complete")


if __name__ == "__main__":
    main()
