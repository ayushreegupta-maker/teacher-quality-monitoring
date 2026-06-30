"""
One-off backfill: walk data/rubric_runs/<subject>/<run>/5_answers.json,
extract materials_seen, and write it into the Runs sheet of
data/tqm_answers.xlsx as a new `materials_seen_json` column.

Idempotent: re-running won't duplicate the column. If the column already
exists, existing values are overwritten with the latest JSON value for
each (run_id, config_slug) match.

Run once:
    .venv/bin/python scripts/testing/backfill_materials_seen.py
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent.parent
XLSX = ROOT / "data" / "tqm_answers.xlsx"
RUBRIC_RUNS = ROOT / "data" / "rubric_runs"


def _norm_run_id(run_id: str) -> str:
    """'2026-06-29T12:54:16' → '2026-06-29T125416'."""
    return str(run_id).replace(":", "")


def materials_index_from_disk() -> dict[str, str]:
    """{config_slug → json-encoded materials_seen}. Only includes runs that
    have a non-empty materials_seen list in their 5_answers.json."""
    out: dict[str, str] = {}
    if not RUBRIC_RUNS.exists():
        return out
    for subj_dir in RUBRIC_RUNS.iterdir():
        if not subj_dir.is_dir():
            continue
        for run_dir in subj_dir.iterdir():
            if not run_dir.is_dir():
                continue
            p = run_dir / "5_answers.json"
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text())
            except Exception as e:
                print(f"  warn: bad JSON at {p.relative_to(ROOT)}: {e}")
                continue
            ms = data.get("materials_seen")
            if not ms:
                continue
            out[run_dir.name] = json.dumps(ms)
    return out


def main():
    if not XLSX.exists():
        print(f"missing: {XLSX}")
        sys.exit(1)

    backup = XLSX.with_name(
        f"tqm_answers.backup_{datetime.now().strftime('%Y%m%dT%H%M%S')}.xlsx"
    )
    shutil.copy2(XLSX, backup)
    print(f"backup → {backup.name}")

    materials_by_slug = materials_index_from_disk()
    print(f"loaded materials_seen for {len(materials_by_slug)} run(s) from disk")

    wb = openpyxl.load_workbook(XLSX)
    if "Runs" not in wb.sheetnames:
        print("ERROR: 'Runs' sheet missing from workbook")
        sys.exit(1)
    ws = wb["Runs"]

    headers = [c.value for c in ws[1]]
    if "materials_seen_json" not in headers:
        new_col = ws.max_column + 1
        ws.cell(row=1, column=new_col, value="materials_seen_json")
        headers.append("materials_seen_json")
        print(f"added column 'materials_seen_json' at position {new_col}")
    ms_col_idx = headers.index("materials_seen_json") + 1  # 1-based

    slug_col_idx = headers.index("config_slug") + 1

    n_patched = 0
    n_total = 0
    for row in ws.iter_rows(min_row=2, max_col=ws.max_column):
        n_total += 1
        slug_cell = row[slug_col_idx - 1]
        slug = (slug_cell.value or "").strip()
        if not slug:
            continue
        materials = materials_by_slug.get(slug)
        if materials:
            ws.cell(row=slug_cell.row, column=ms_col_idx, value=materials)
            n_patched += 1

    wb.save(XLSX)
    print(f"patched {n_patched}/{n_total} Runs rows with materials_seen_json")


if __name__ == "__main__":
    main()
