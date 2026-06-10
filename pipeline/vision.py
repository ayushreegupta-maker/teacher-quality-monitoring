import logging
import time

from adapters.llm import LLMAdapter, parse_json_lenient, prompt_hash
from adapters.sessions import session_dir
from pipeline.render import load_prompt, render_vision_prompt, split_system_user
from pipeline.types import (
    SessionMeta,
    Transcript,
    TranscriptSegment,
    VisualObservation,
    VisualObservations,
)

log = logging.getLogger(__name__)

CHUNK_MINUTES = 5
# When Gemini silently returns empty observations+transcript, retry up to this
# many times. Real-world observation: ~5% of clips come back blank on first
# call due to transient model load; a single retry almost always fixes it.
CHUNK_MAX_ATTEMPTS = 3
CHUNK_RETRY_BACKOFF_SECONDS = (5, 15)  # backoff before attempts 2 and 3


def _shift_ts(ts: str, offset_seconds: int) -> str:
    """Shift an HH:MM:SS (or MM:SS) timestamp by offset_seconds. Returns HH:MM:SS."""
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(float(parts[2]))
        elif len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(float(parts[1]))
        else:
            return ts
    except ValueError:
        return ts
    total = h * 3600 + m * 60 + s + offset_seconds
    nh, rem = divmod(total, 3600)
    nm, ns = divmod(rem, 60)
    return f"{nh:02d}:{nm:02d}:{ns:02d}"


def _make_chunks(duration_seconds: int, chunk_seconds: int) -> list[tuple[int, int]]:
    return [
        (start, min(start + chunk_seconds, duration_seconds))
        for start in range(0, duration_seconds, chunk_seconds)
    ]


def _dedupe_transcript_loops(segments: list[dict], min_run: int = 5) -> tuple[list[dict], int]:
    """Collapse runs of `min_run`+ consecutive identical (speaker, text) segments
    into a single annotated segment. Returns (deduped_segments, count_collapsed).

    Gemini occasionally falls into transcription loops where it repeats the same
    line at 1-second intervals for tens or hundreds of segments (e.g. "I don't
    know." × 291). This collapses those runs while preserving the signal that a
    loop occurred — the annotation lets downstream scoring treat it as artifact.

    A run of fewer than `min_run` identical segments is left alone (so a teacher
    genuinely saying "yes" three times in a row is preserved).
    """
    if not segments:
        return segments, 0

    out: list[dict] = []
    collapsed_total = 0
    i = 0
    while i < len(segments):
        j = i + 1
        while (
            j < len(segments)
            and segments[j].get("speaker") == segments[i].get("speaker")
            and segments[j].get("text") == segments[i].get("text")
        ):
            j += 1
        run_len = j - i
        if run_len >= min_run:
            merged = dict(segments[i])
            merged["text"] = (
                f"{merged['text']} (repeated {run_len} times — "
                "likely transcription loop artifact)"
            )
            out.append(merged)
            collapsed_total += run_len - 1
        else:
            out.extend(segments[i:j])
        i = j
    return out, collapsed_total


def _collapse_within_segment_repetition(
    text: str, min_run: int = 5
) -> tuple[str, int]:
    """Collapse runs of identical period-delimited fragments within a single
    segment's text. Returns (new_text, fragments_collapsed).

    Gemini sometimes emits a single transcript segment whose `text` field
    contains the SAME sentence repeated dozens of times in a row, separated
    by ". " — e.g. "I don't know. I don't know. I don't know. ..." × 40
    inside one segment. The outer per-segment dedupe doesn't catch this
    because there's only one segment.

    We split on ". ", group identical adjacent fragments, and collapse any
    run of ≥ `min_run` into a single fragment annotated with the count.
    """
    if not text or ". " not in text:
        return text, 0

    fragments = text.split(". ")
    out_frags: list[str] = []
    collapsed = 0
    i = 0
    while i < len(fragments):
        j = i + 1
        while j < len(fragments) and fragments[j].strip() == fragments[i].strip():
            j += 1
        run_len = j - i
        if run_len >= min_run:
            out_frags.append(
                f"{fragments[i]} (repeated {run_len} times — "
                "likely transcription loop artifact)"
            )
            collapsed += run_len - 1
        else:
            out_frags.extend(fragments[i:j])
        i = j
    return ". ".join(out_frags), collapsed


def _dedupe_alternating_loops(
    segments: list[dict], min_cycles: int = 5
) -> tuple[list[dict], int]:
    """Collapse A-B-A-B alternating-segment loops into a single A-B pair plus
    an annotation. Returns (deduped_segments, count_collapsed).

    Pattern: Gemini sometimes ping-pongs between two phrases for many
    consecutive segments — e.g.
        teacher: "Look at this."
        child:   "Wow."
        teacher: "Look at this."
        child:   "Wow."
        ... × 30
    The plain per-segment dedupe misses this because no two adjacent
    segments are identical; they're identical at stride 2.

    Detection: at each position i, treat (segments[i], segments[i+1]) as a
    candidate cycle and count how many consecutive cycles match it (same
    speaker AND same text on both halves). If we find ≥ `min_cycles`
    cycles, keep one cycle (2 segments) and annotate the second one with
    the cycle count; drop the rest.
    """
    if len(segments) < min_cycles * 2:
        return segments, 0

    def _same(a: dict, b: dict) -> bool:
        return (
            a.get("speaker") == b.get("speaker") and a.get("text") == b.get("text")
        )

    out: list[dict] = []
    collapsed_total = 0
    i = 0
    n = len(segments)
    while i < n:
        # Try to detect an A-B-A-B... cycle starting at i.
        if i + 1 < n:
            a, b = segments[i], segments[i + 1]
            # Don't treat A==B as an "alternating" loop — the per-segment
            # dedupe owns that case.
            if not _same(a, b):
                cycles = 1
                k = i + 2
                while (
                    k + 1 < n
                    and _same(segments[k], a)
                    and _same(segments[k + 1], b)
                ):
                    cycles += 1
                    k += 2
                if cycles >= min_cycles:
                    out.append(dict(a))
                    merged_b = dict(b)
                    merged_b["text"] = (
                        f"{merged_b.get('text', '')} (A-B pair repeated "
                        f"{cycles} times — likely transcription loop artifact)"
                    )
                    out.append(merged_b)
                    collapsed_total += (cycles - 1) * 2
                    i = k
                    continue
        out.append(segments[i])
        i += 1
    return out, collapsed_total


def vision_observe(
    session: SessionMeta,
    llm: LLMAdapter,
    chunk_minutes: int = CHUNK_MINUTES,
) -> tuple[Transcript, VisualObservations]:
    """Chunked Gemini vision pass. Uploads video once, analyses N-min slices via
    video_metadata offsets, shifts timestamps in Python, and merges results.

    Resilient to per-chunk failures: a failed chunk is logged and skipped; remaining
    chunks still contribute. Raw responses are saved per chunk for debugging.
    """
    log.info(f"[{session.session_id}] vision pass starting on {session.video_path}")

    duration_seconds = session.duration_minutes * 60
    chunk_seconds = chunk_minutes * 60
    chunks = _make_chunks(duration_seconds, chunk_seconds)
    log.info(
        f"[{session.session_id}] splitting {session.duration_minutes}min video "
        f"into {len(chunks)} chunks of up to {chunk_minutes}min"
    )

    video_file = llm.upload_video(session.video_path)

    template = render_vision_prompt(session)
    system, user = split_system_user(template)
    full_prompt = f"{system}\n\n{user}"
    p_hash = prompt_hash(template)

    sd = session_dir(session.session_id)
    sd.mkdir(parents=True, exist_ok=True)

    all_observations: list[dict] = []
    all_segments: list[dict] = []

    for i, (start, end) in enumerate(chunks):
        chunk_label = f"chunk {i + 1}/{len(chunks)} ({start}s-{end}s)"
        log.info(f"[{session.session_id}] {chunk_label} starting")

        # Retry the chunk on three transient failure modes:
        #   (a) call_gemini_video raises (network / rate limit / etc.)
        #   (b) parse_json_lenient raises (truncated / malformed response)
        #   (c) parse succeeds but BOTH arrays are empty (Gemini silently
        #       returned a placeholder — the failure mode we saw on the
        #       D06 colouring 'after' clip)
        chunk_obs: list = []
        chunk_segs: list = []
        succeeded = False
        for attempt in range(1, CHUNK_MAX_ATTEMPTS + 1):
            if attempt > 1:
                backoff = CHUNK_RETRY_BACKOFF_SECONDS[
                    min(attempt - 2, len(CHUNK_RETRY_BACKOFF_SECONDS) - 1)
                ]
                log.warning(
                    f"[{session.session_id}] {chunk_label} retry "
                    f"{attempt}/{CHUNK_MAX_ATTEMPTS} after {backoff}s backoff"
                )
                time.sleep(backoff)

            attempt_suffix = "" if attempt == 1 else f"_attempt{attempt:02d}"
            raw_path = sd / f"vision_raw_chunk{i:02d}{attempt_suffix}_{p_hash}.txt"

            try:
                raw = llm.call_gemini_video(
                    prompt=full_prompt,
                    video_file=video_file,
                    start_seconds=start,
                    end_seconds=end,
                )
            except Exception as e:
                log.error(
                    f"[{session.session_id}] {chunk_label} attempt {attempt} "
                    f"call failed: {e!r}"
                )
                continue

            raw_path.write_text(raw)

            try:
                parsed = parse_json_lenient(raw)
            except Exception as e:
                log.error(
                    f"[{session.session_id}] {chunk_label} attempt {attempt} "
                    f"parse failed: {e!r} (raw at {raw_path})"
                )
                continue

            chunk_obs = parsed.get("observations") or []
            chunk_segs = parsed.get("transcript") or []

            if not chunk_obs and not chunk_segs:
                log.warning(
                    f"[{session.session_id}] {chunk_label} attempt {attempt} "
                    f"returned empty observations and transcript "
                    f"(raw at {raw_path})"
                )
                continue

            succeeded = True
            break

        if not succeeded:
            log.error(
                f"[{session.session_id}] {chunk_label} FAILED after "
                f"{CHUNK_MAX_ATTEMPTS} attempts — skipping chunk"
            )
            continue

        # Shift chunk-local timestamps -> absolute
        for o in chunk_obs:
            if "ts_start" in o:
                o["ts_start"] = _shift_ts(o["ts_start"], start)
            if "ts_end" in o:
                o["ts_end"] = _shift_ts(o["ts_end"], start)
        for s in chunk_segs:
            if "ts_start" in s:
                s["ts_start"] = _shift_ts(s["ts_start"], start)

        all_observations.extend(chunk_obs)
        all_segments.extend(chunk_segs)
        log.info(
            f"[{session.session_id}] {chunk_label} done: "
            f"+{len(chunk_obs)} observations, +{len(chunk_segs)} transcript segments"
        )

    # Three-pass dedupe of Gemini transcription loop artifacts:
    #   1. Adjacent-segment runs: N copies of the same (speaker, text) in a row.
    #   2. Within-segment text repetition: ONE segment whose .text contains the
    #      same period-delimited fragment N times.
    #   3. Alternating A-B-A-B cycles: two segments that ping-pong N times.
    # All three patterns have been observed in real Gemini output on the
    # D28 art-class run (2026-06-04).
    all_segments, collapsed = _dedupe_transcript_loops(all_segments, min_run=5)
    if collapsed:
        log.warning(
            f"[{session.session_id}] dedupe pass 1 (adjacent runs): collapsed "
            f"{collapsed} repeated transcript segments"
        )

    within_collapsed_total = 0
    for s in all_segments:
        new_text, within_collapsed = _collapse_within_segment_repetition(
            s.get("text", ""), min_run=5
        )
        if within_collapsed:
            s["text"] = new_text
            within_collapsed_total += within_collapsed
    if within_collapsed_total:
        log.warning(
            f"[{session.session_id}] dedupe pass 2 (within-segment): collapsed "
            f"{within_collapsed_total} repeated text fragments inside segments"
        )

    all_segments, alt_collapsed = _dedupe_alternating_loops(all_segments, min_cycles=5)
    if alt_collapsed:
        log.warning(
            f"[{session.session_id}] dedupe pass 3 (alternating A-B): collapsed "
            f"{alt_collapsed} segments from A-B-A-B loop cycles"
        )

    # Build typed artifacts (skip malformed entries quietly)
    transcript_segments = []
    for s in all_segments:
        try:
            transcript_segments.append(TranscriptSegment(**s))
        except Exception as e:
            log.warning(f"[{session.session_id}] dropping malformed transcript segment: {e!r}")

    observation_objects = []
    for o in all_observations:
        try:
            observation_objects.append(VisualObservation(**o))
        except Exception as e:
            log.warning(f"[{session.session_id}] dropping malformed observation: {e!r}")

    transcript = Transcript(
        session_id=session.session_id,
        segments=transcript_segments,
        source_model=llm.vision_model,
        prompt_hash=p_hash,
    )
    observations = VisualObservations(
        session_id=session.session_id,
        observations=observation_objects,
        source_model=llm.vision_model,
        prompt_hash=p_hash,
    )

    (sd / f"transcript_{p_hash}.json").write_text(transcript.model_dump_json(indent=2))
    (sd / f"vision_{p_hash}.json").write_text(observations.model_dump_json(indent=2))

    log.info(
        f"[{session.session_id}] vision pass done across {len(chunks)} chunks: "
        f"{len(transcript.segments)} transcript segments, "
        f"{len(observations.observations)} observations"
    )
    return transcript, observations


# ─── Re-apply the dedupe to an existing transcript ────────────────────────


def redo_dedupe_for_run(run_dir) -> dict:
    """Re-apply the 3-pass dedupe to <run_dir>/6_transcript.json — useful
    when the dedupe rules have been tightened and we want the cleaner
    output without re-paying for the vision pass.

    Writes:
        <run_dir>/6_transcript_deduped_v2.json
        <run_dir>/6_transcript_readable_v2.txt

    Returns a summary dict::

        {
            "loaded": int,                # input segment count
            "pass1_adjacent_collapsed": int,
            "pass2_within_segment_collapsed": int,
            "pass3_alternating_collapsed": int,
            "final": int,                 # output segment count
            "json_out": str,
            "text_out": str,
        }

    Absorbed from scripts/redo_transcript_dedupe.py in step 11 of the TQM
    consolidation migration. CLI entry point at the bottom of this module
    so `python -m pipeline.vision <run_dir>` works.
    """
    import json
    from pathlib import Path

    run_dir = Path(run_dir).resolve()
    src = run_dir / "6_transcript.json"
    if not src.exists():
        raise FileNotFoundError(f"missing: {src}")

    data = json.loads(src.read_text())
    segments = [dict(s) for s in data.get("segments", [])]
    n_in = len(segments)
    log.info(f"redo_dedupe: loaded {n_in} segments from {src}")

    # Pass 1: adjacent-run dedupe. Harmless on already-deduped input; lets
    # us report one consistent set of counters end-to-end.
    segments, p1 = _dedupe_transcript_loops(segments, min_run=5)
    log.info(f"redo_dedupe: pass 1 (adjacent runs) collapsed: {p1}")

    # Pass 2: within-segment text repetition (single segment carrying the
    # same period-delimited fragment N times in a row).
    p2 = 0
    for s in segments:
        new_text, c = _collapse_within_segment_repetition(s.get("text", ""), min_run=5)
        if c:
            s["text"] = new_text
            p2 += c
    log.info(f"redo_dedupe: pass 2 (within-segment fragments) collapsed: {p2}")

    # Pass 3: A-B-A-B alternating cycles.
    segments, p3 = _dedupe_alternating_loops(segments, min_cycles=5)
    log.info(f"redo_dedupe: pass 3 (alternating A-B cycles) collapsed: {p3}")

    n_out = len(segments)
    log.info(f"redo_dedupe: segments: {n_in} -> {n_out}  (removed {n_in - n_out})")

    data["segments"] = segments
    out_json = run_dir / "6_transcript_deduped_v2.json"
    out_json.write_text(json.dumps(data, indent=2))

    lines = []
    for s in segments:
        ts = s.get("ts_start") or "--:--:--"
        spk = s.get("speaker") or "?"
        txt = s.get("text") or ""
        lines.append(f"[{ts}] {spk}: {txt}")
    out_txt = run_dir / "6_transcript_readable_v2.txt"
    out_txt.write_text("\n".join(lines) + "\n")

    return {
        "loaded": n_in,
        "pass1_adjacent_collapsed": p1,
        "pass2_within_segment_collapsed": p2,
        "pass3_alternating_collapsed": p3,
        "final": n_out,
        "json_out": str(out_json),
        "text_out": str(out_txt),
    }


if __name__ == "__main__":
    # CLI:  python -m pipeline.vision <run_dir>
    # Re-applies the 3-pass dedupe to <run_dir>/6_transcript.json without
    # re-running the Gemini vision pass. Useful after dedupe-rule updates.
    import sys
    if len(sys.argv) != 2:
        sys.exit("usage: python -m pipeline.vision <run_dir>")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    result = redo_dedupe_for_run(sys.argv[1])
    print(
        f"\ndone. {result['loaded']} -> {result['final']} segments.\n"
        f"  pass1 (adjacent): {result['pass1_adjacent_collapsed']}\n"
        f"  pass2 (within):   {result['pass2_within_segment_collapsed']}\n"
        f"  pass3 (alt A-B):  {result['pass3_alternating_collapsed']}\n"
        f"wrote: {result['json_out']}\n"
        f"       {result['text_out']}"
    )
