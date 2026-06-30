"""
Session-video cache layer.

Turns a session_id + raw NVR segments into the three derived video artifacts
the rubric pipeline needs:

  data/sessions/<subject>/<session_id>/
      0_segments_used.json     ← which raw segments were stitched, in order
      1a_combined.mp4          ← full-resolution stitch (720p re-encode)
      1b_boundary_input.mp4    ← 0.5 fps / 480p / silent — cheap input for
                                  boundary detection (keeps Gemini under its
                                  1M-token input ceiling for long classes)
      2_boundaries.json        ← class start/end (wall-clock + elapsed)
      3_trimmed.mp4            ← 1a, cropped to the class window only

Each stage is idempotent: if the output already exists, the stage skips.
Re-running the orchestrator on a session whose four outputs already exist
performs zero ffmpeg + zero Gemini work — exactly the property the evidence
cache layer (next step) depends on.

This module is the canonical home for stages 1–3. The legacy script
`scripts/run_art_rubric_test.py` keeps its own copies until `run_rubric.py`
lands and the old script is archived (PLAN.md §3.7 steps 8 + 11).
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from adapters.llm import LLMAdapter, parse_json_lenient, prompt_hash
from pipeline.boundaries import _derive_elapsed_from_walls, _parse_hms
from pipeline.render import _jinja_env, load_prompt, split_system_user
from pipeline.session_context import (
    SegmentEntry,
    parse_session_id,
    resolve_session_segments,
)
from pipeline.types import BoundaryDetection, SessionMeta

log = logging.getLogger(__name__)

# ─── Tuning constants ──────────────────────────────────────────────────────
# 1-min padding on either side of the detected class window for the trim
MARGIN_SECONDS = 60
# Sampling rate Gemini reads the boundary_input at. 0.3fps = 1 frame every
# ~3.3s — enough to read wall-clock and detect child presence, while staying
# well under the 1M input-token ceiling for a 2.5h source video.
BOUNDARY_FPS = 0.3

# ─── Paths ─────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SESSIONS_DIR = _PROJECT_ROOT / "data" / "sessions"


# ─── Artifacts dataclass ───────────────────────────────────────────────────
@dataclass(frozen=True)
class SessionVideoArtifacts:
    """The file paths and metadata produced by build_session_video()."""
    session_id: str
    subject: str
    session_dir: Path
    segments_used: list[SegmentEntry]
    combined: Path
    boundary_input: Path
    boundaries_json: Path
    trimmed: Path

    def all_exist(self) -> bool:
        return (
            self.combined.exists()
            and self.boundary_input.exists()
            and self.boundaries_json.exists()
            and self.trimmed.exists()
        )


# ─── Path conventions ──────────────────────────────────────────────────────
def session_dir_for(
    session_id: str,
    subject: str,
    sessions_root: Path = _DEFAULT_SESSIONS_DIR,
) -> Path:
    """Canonical session-dir path: data/sessions/<subject>/<session_id>/."""
    return sessions_root / subject / session_id


# ─── ffprobe wrapper ──────────────────────────────────────────────────────
def video_duration_seconds(path: Path) -> float:
    """Return container duration in seconds via ffprobe."""
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
    ).decode().strip()
    return float(out)


# ─── Stage 1: stitch + downscale ──────────────────────────────────────────
def stage1_combine_and_downscale(
    segments: list[Path],
    combined: Path,
    boundary_input: Path,
    force: bool = False,
) -> None:
    """Build two outputs from one ffmpeg pipeline:

      combined        — 720p re-encode of all segments concatenated.
                        Used for trim → rubric scoring downstream.
      boundary_input  — 0.5 fps / 480p / silent copy of `combined`.
                        Used ONLY for the boundary-detection Gemini call;
                        small enough to fit the input-token ceiling for
                        long classes.

    Idempotent. Skips when both outputs already exist (or only the missing
    half is built).
    """
    if combined.exists() and boundary_input.exists() and not force:
        log.info("[1] combine: both outputs exist, skipping")
        return
    if any(not s.exists() for s in segments):
        missing = [s for s in segments if not s.exists()]
        raise FileNotFoundError(f"Missing source segments: {missing}")

    combined.parent.mkdir(parents=True, exist_ok=True)

    n = len(segments)
    inputs_args: list[str] = []
    for s in segments:
        inputs_args += ["-i", str(s)]
    concat_streams = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    filter_complex = (
        f"{concat_streams}concat=n={n}:v=1:a=1[cv][a];"
        f"[cv]scale=1280:720[v]"
    )

    # 1a: full-quality combined
    if not combined.exists() or force:
        log.info(
            f"[1a] combine: {n} clip(s) → {combined.name} (5–15 min)"
        )
        log_a = combined.parent / "1a_ffmpeg_combine.log"
        cmd_a = [
            "ffmpeg", "-y",
            *inputs_args,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-crf", "28", "-preset", "fast",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            str(combined),
        ]
        t0 = time.time()
        with open(log_a, "w") as logf:
            res = subprocess.run(cmd_a, stdout=logf, stderr=subprocess.STDOUT)
        if res.returncode != 0:
            tail = log_a.read_text().splitlines()[-40:]
            log.error(
                f"[1a] ffmpeg failed (exit {res.returncode}). Tail of {log_a.name}:"
            )
            for line in tail:
                log.error(f"  {line}")
            raise RuntimeError(f"ffmpeg combine failed; see {log_a}")
        sz_mb = combined.stat().st_size / 1e6
        log.info(f"[1a] done in {(time.time()-t0)/60:.1f} min, {sz_mb:.0f} MB")
    else:
        log.info(f"[1a] combine: {combined.name} exists, skipping")

    # 1b: low-fps low-res silent copy for boundary detection
    if not boundary_input.exists() or force:
        log.info(
            f"[1b] boundary input: re-encoding to 0.5 fps / 480p / silent → "
            f"{boundary_input.name}"
        )
        log_b = combined.parent / "1b_ffmpeg_boundary_input.log"
        cmd_b = [
            "ffmpeg", "-y",
            "-i", str(combined),
            "-vf", "fps=0.5,scale=854:480",
            "-an",
            "-c:v", "libx264", "-crf", "30", "-preset", "fast",
            "-movflags", "+faststart",
            str(boundary_input),
        ]
        t0 = time.time()
        with open(log_b, "w") as logf:
            res = subprocess.run(cmd_b, stdout=logf, stderr=subprocess.STDOUT)
        if res.returncode != 0:
            tail = log_b.read_text().splitlines()[-40:]
            log.error(
                f"[1b] ffmpeg failed (exit {res.returncode}). Tail of {log_b.name}:"
            )
            for line in tail:
                log.error(f"  {line}")
            raise RuntimeError(
                f"ffmpeg boundary-input failed; see {log_b}"
            )
        sz_mb = boundary_input.stat().st_size / 1e6
        log.info(f"[1b] done in {(time.time()-t0)/60:.1f} min, {sz_mb:.0f} MB")
    else:
        log.info(
            f"[1b] boundary input: {boundary_input.name} exists, skipping"
        )


# ─── Stage 2: boundary detection ──────────────────────────────────────────
def stage2_detect_boundaries(
    boundary_input: Path,
    combined: Path,
    session_id: str,
    subject: str,
    llm: LLMAdapter,
    out_path: Path,
    filename_anchor: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Detect class-window boundaries on `boundary_input` via a single
    Gemini call. Writes JSON to `out_path`; returns the parsed dict.

    `filename_anchor` (e.g. "08:31:32") is the wall-clock encoded in the
    FIRST raw segment's filename — used to override a broken model reading
    of `video_start_wall_clock` when the burnt-in clock is illegible at
    480p frame zero. Pass None to trust whatever the model reads.

    Inlined here rather than calling pipeline.boundaries.detect_boundaries()
    because we need to pass fps=BOUNDARY_FPS through to call_gemini_video —
    a long video at 1 fps still blows Gemini's 1M-token input ceiling.
    """
    if out_path.exists() and not force:
        log.info(f"[2] boundaries: {out_path.name} exists, skipping")
        return json.loads(out_path.read_text())

    dur_sec = video_duration_seconds(combined)
    dur_min = int(round(dur_sec / 60))
    log.info(
        f"[2] boundaries: detecting on {boundary_input.name} "
        f"({dur_min} min, fps={BOUNDARY_FPS}) — Gemini call"
    )

    session = SessionMeta(
        session_id=session_id,
        recorded_at=date.today(),
        duration_minutes=dur_min,
        subject=subject,
        video_path=boundary_input,
    )

    template_text = load_prompt("boundaries")
    rendered = _jinja_env().from_string(template_text).render(
        session=session.model_dump(mode="json"),
    )
    system, user = split_system_user(rendered)
    full_prompt = f"{system}\n\n{user}"

    video_file = llm.upload_video(boundary_input)
    raw = llm.call_gemini_video(
        prompt=full_prompt,
        video_file=video_file,
        fps=BOUNDARY_FPS,
        # MEDIA_RESOLUTION_LOW (66 tok/frame) keeps long combined videos
        # under the 1M input-token ceiling. A 226-min combined video at
        # BOUNDARY_FPS=0.5 would otherwise be ~1.75M tokens at MEDIUM
        # (258 tok/frame), busting the limit. Boundary detection is a
        # coarse first-child-visible / last-child-visible signal; LOW
        # is plenty for that.
        media_resolution="low",
    )
    raw_path = out_path.with_name(out_path.stem + "_raw.txt")
    raw_path.write_text(raw)

    parsed = parse_json_lenient(raw)
    result = BoundaryDetection.model_validate(parsed)
    result.session_id = session_id
    result.source_model = llm.vision_model
    result.prompt_hash = prompt_hash(template_text)

    # Filename anchor override (see docstring)
    if filename_anchor and (
        not result.video_start_wall_clock
        or result.video_start_wall_clock in ("00:00:00", "0:00:00")
    ):
        log.warning(
            f"[2] Gemini returned video_start_wall_clock="
            f"{result.video_start_wall_clock!r} — overriding with "
            f"filename anchor {filename_anchor!r}"
        )
        result.video_start_wall_clock = filename_anchor

    _derive_elapsed_from_walls(result, session_id)

    payload = result.model_dump(mode="json")
    out_path.write_text(json.dumps(payload, indent=2))
    log.info(
        f"[2] boundaries: first_child={payload.get('first_child_visible_at')} "
        f"last_child={payload.get('last_child_visible_at')} "
        f"confidence={payload.get('confidence')}"
    )
    return payload


def compute_trim_window(
    boundaries: dict, combined_dur_sec: float
) -> tuple[float, float]:
    """Return (start_sec, end_sec) to trim, with MARGIN_SECONDS padding.
    Falls back to a safe whole-video window if boundaries are missing or
    degenerate."""
    first = boundaries.get("first_child_visible_at")
    last = boundaries.get("last_child_visible_at")

    first_sec = _parse_hms(first) if first else None
    last_sec = _parse_hms(last) if last else None

    if first_sec is None and last_sec is None:
        log.warning("[2] no boundary info — trimming whole video (no margins)")
        return 0.0, combined_dur_sec

    start = max(
        0.0, (first_sec - MARGIN_SECONDS) if first_sec is not None else 0.0
    )
    end = min(
        combined_dur_sec,
        (last_sec + MARGIN_SECONDS) if last_sec is not None else combined_dur_sec,
    )

    if end <= start:
        log.warning(
            f"[2] computed window degenerate (start={start}, end={end}) — "
            "using whole video"
        )
        return 0.0, combined_dur_sec
    return start, end


# ─── Stage 3: trim to class window ────────────────────────────────────────
def stage3_trim_to_class(
    combined: Path,
    boundaries: dict,
    trimmed: Path,
    force: bool = False,
) -> None:
    """Crop `combined` to the class window from `boundaries`. Re-encodes
    at 1 fps / 720p / AAC 96k — small enough for Gemini's input limit on
    a 100-min class but still legible + still has full audio for tone Qs."""
    if trimmed.exists() and not force:
        log.info(f"[3] trim: {trimmed.name} exists, skipping")
        return

    combined_dur_sec = video_duration_seconds(combined)
    start, end = compute_trim_window(boundaries, combined_dur_sec)

    log.info(
        f"[3] trim: {start:.0f}s → {end:.0f}s ({(end-start)/60:.1f} min) → "
        f"{trimmed.name}"
    )
    log_path = trimmed.parent / "3_ffmpeg_trim.log"
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start}", "-to", f"{end}",
        "-i", str(combined),
        "-vf", "fps=1,scale=1280:720",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        str(trimmed),
    ]
    t0 = time.time()
    with open(log_path, "w") as logf:
        result = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        tail = log_path.read_text().splitlines()[-40:]
        log.error(
            f"[3] ffmpeg failed (exit {result.returncode}). Tail of {log_path.name}:"
        )
        for line in tail:
            log.error(f"  {line}")
        raise RuntimeError(f"ffmpeg trim failed; see {log_path}")
    sz_mb = trimmed.stat().st_size / 1e6
    log.info(f"[3] trim: done in {(time.time()-t0)/60:.1f} min, {sz_mb:.0f} MB")


# ─── Orchestrator ─────────────────────────────────────────────────────────
def build_session_video(
    session_id: str,
    llm: Optional[LLMAdapter] = None,
    force: bool = False,
    sessions_root: Path = _DEFAULT_SESSIONS_DIR,
) -> SessionVideoArtifacts:
    """End-to-end stages 1–3 for one session. Idempotent.

    Returns a SessionVideoArtifacts pointing at all four output files. If
    every output already exists, performs zero ffmpeg + zero Gemini work and
    just returns the artifacts handle.

    `llm` may be omitted only when all four outputs already exist; otherwise
    a real LLMAdapter is required for stage 2's Gemini call.
    """
    segments_entries = resolve_session_segments(session_id)
    if not segments_entries:
        raise ValueError(
            f"no raw segments resolved for session_id {session_id!r} "
            "— check cctv_cameras.xlsx + data/raw/<subject>/ layout"
        )
    subject = segments_entries[0].subject

    sdir = session_dir_for(session_id, subject, sessions_root=sessions_root)
    sdir.mkdir(parents=True, exist_ok=True)

    combined = sdir / "1a_combined.mp4"
    boundary_input = sdir / "1b_boundary_input.mp4"
    boundaries_json = sdir / "2_boundaries.json"
    trimmed = sdir / "3_trimmed.mp4"

    # Always write segments_used.json — cheap, useful for debugging.
    segments_used_path = sdir / "0_segments_used.json"
    segments_used_path.write_text(
        json.dumps(
            [
                {
                    "path": str(e.path),
                    "starts_at": e.starts_at.isoformat(),
                    "camera_id": e.camera_id,
                    "subject": e.subject,
                }
                for e in segments_entries
            ],
            indent=2,
        )
    )

    # Anchor wall-clock from the FIRST segment's filename for stage 2's
    # override (works even when Gemini misreads the burnt-in clock at frame 0).
    first_starts = segments_entries[0].starts_at
    filename_anchor = first_starts.strftime("%H:%M:%S")

    # If all outputs already exist, the orchestrator is a no-op and we can
    # return without requiring an LLM (useful for cache hits + tests).
    artifacts = SessionVideoArtifacts(
        session_id=session_id,
        subject=subject,
        session_dir=sdir,
        segments_used=segments_entries,
        combined=combined,
        boundary_input=boundary_input,
        boundaries_json=boundaries_json,
        trimmed=trimmed,
    )
    if artifacts.all_exist() and not force:
        log.info(
            f"[session_video] {session_id}: all 4 outputs already exist, "
            "no ffmpeg/Gemini work needed"
        )
        return artifacts

    if llm is None:
        raise ValueError(
            f"[session_video] {session_id}: not all outputs exist and llm=None"
            " — pass an LLMAdapter (stage 2 requires Gemini)"
        )

    stage1_combine_and_downscale(
        segments=[e.path for e in segments_entries],
        combined=combined,
        boundary_input=boundary_input,
        force=force,
    )
    boundaries = stage2_detect_boundaries(
        boundary_input=boundary_input,
        combined=combined,
        session_id=session_id,
        subject=subject,
        llm=llm,
        out_path=boundaries_json,
        filename_anchor=filename_anchor,
        force=force,
    )
    stage3_trim_to_class(
        combined=combined,
        boundaries=boundaries,
        trimmed=trimmed,
        force=force,
    )
    return artifacts
