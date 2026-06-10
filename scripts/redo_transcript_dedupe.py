"""Re-apply the new 3-pass transcript dedupe to an already-saved 6_transcript.json
without re-running the (slow, paid) Gemini vision pipeline.

Reads:  <run_dir>/6_transcript.json   (existing — original dedupe output)
Writes: <run_dir>/6_transcript_deduped_v2.json
        <run_dir>/6_transcript_readable_v2.txt

Usage:
    python3 scripts/redo_transcript_dedupe.py \
        data/art_rubric_runs/2026-06-04_122724
"""
import json
import sys
from pathlib import Path

# Make pipeline.* importable when run from project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.vision import (
    _collapse_within_segment_repetition,
    _dedupe_alternating_loops,
    _dedupe_transcript_loops,
)


def main(run_dir: Path) -> None:
    src = run_dir / "6_transcript.json"
    if not src.exists():
        sys.exit(f"missing: {src}")

    data = json.loads(src.read_text())
    segments = [dict(s) for s in data.get("segments", [])]
    n_in = len(segments)
    print(f"loaded {n_in} segments from {src}")

    # Pass 1: adjacent-run dedupe (same pattern the pipeline already ran, but
    # apply it again — harmless on already-deduped input, and lets us report
    # one consistent set of counters).
    segments, p1 = _dedupe_transcript_loops(segments, min_run=5)
    print(f"  pass 1 (adjacent runs) collapsed: {p1}")

    # Pass 2: within-segment text repetition.
    p2 = 0
    for s in segments:
        new_text, c = _collapse_within_segment_repetition(s.get("text", ""), min_run=5)
        if c:
            s["text"] = new_text
            p2 += c
    print(f"  pass 2 (within-segment fragments) collapsed: {p2}")

    # Pass 3: A-B-A-B alternating cycles.
    segments, p3 = _dedupe_alternating_loops(segments, min_cycles=5)
    print(f"  pass 3 (alternating A-B cycles) collapsed: {p3}")

    n_out = len(segments)
    print(f"segments: {n_in} -> {n_out}  (removed {n_in - n_out})")

    data["segments"] = segments
    out_json = run_dir / "6_transcript_deduped_v2.json"
    out_json.write_text(json.dumps(data, indent=2))
    print(f"wrote {out_json}")

    # Human-readable.
    lines = []
    for s in segments:
        ts = s.get("ts_start") or "--:--:--"
        spk = s.get("speaker") or "?"
        txt = s.get("text") or ""
        lines.append(f"[{ts}] {spk}: {txt}")
    out_txt = run_dir / "6_transcript_readable_v2.txt"
    out_txt.write_text("\n".join(lines) + "\n")
    print(f"wrote {out_txt}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 scripts/redo_transcript_dedupe.py <run_dir>")
    main(Path(sys.argv[1]).resolve())
