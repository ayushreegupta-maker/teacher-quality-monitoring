"""
Evidence cache layer (PLAN.md §3.2 / §3.5).

The vision pass is the single most expensive stage in the pipeline (~$20 +
75 min per session). When a rubric run only varies its prompt / rubric
version / reasoner, the vision output should be reused, not regenerated.
This module caches that output keyed on
   (session_id, vision_model, vision_fps, chunking)
so re-runs that don't touch any vision parameter cost ~$0.

Layout::

    data/evidence_cache/<subject>/<cache_key>/
        evidence_bundle.json

  cache_key = f"{session_id}__{vision_model}__{fps_token}__chunk-{chunking}"
  fps_token = "fps-default" when fps=None else f"fps-{fps:.2f}"

Public API::

    cache_dir_for(session_id, subject, vision_model, fps, chunking) -> Path
    load_evidence_bundle(...) -> Optional[EvidenceBundle]
    build_evidence_bundle(session_id, llm, ...) -> EvidenceBundle
    enrich_bundle_with_shape_a(bundle, phases, explanations, disturbances)
        -> EvidenceBundle   # persists in place

`build_evidence_bundle` is idempotent: returns the cached bundle
immediately when it exists; otherwise runs `pipeline.vision.vision_observe`
+ assembles the bundle + writes the JSON.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from adapters.llm import LLMAdapter
from pipeline.session_context import resolve_session_segments
from pipeline.session_video import (
    SessionVideoArtifacts,
    build_session_video,
    video_duration_seconds,
)
from pipeline.types import (
    EvidenceBundle,
    SessionMeta,
    Transcript,
    VisualObservations,
)
from pipeline.vision import vision_observe

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CACHE_ROOT = _PROJECT_ROOT / "data" / "evidence_cache"

DEFAULT_CHUNKING = "5min"


# ─── Cache-key helpers ────────────────────────────────────────────────────


def fps_token(fps: Optional[float]) -> str:
    """Encode the fps cache-key segment. None → 'fps-default'; otherwise
    a 2-decimal slug like 'fps-0.30'."""
    if fps is None:
        return "fps-default"
    return f"fps-{fps:.2f}"


def cache_dir_for(
    session_id: str,
    subject: str,
    vision_model: str,
    fps: Optional[float],
    chunking: str = DEFAULT_CHUNKING,
    cache_root: Path = _DEFAULT_CACHE_ROOT,
) -> Path:
    """Canonical evidence-cache dir for a given key. Doesn't create it."""
    slug = (
        f"{session_id}__{vision_model}__{fps_token(fps)}__chunk-{chunking}"
    )
    return cache_root / subject / slug


# ─── Read / load ──────────────────────────────────────────────────────────


def load_evidence_bundle(
    session_id: str,
    subject: str,
    vision_model: str,
    fps: Optional[float],
    chunking: str = DEFAULT_CHUNKING,
    cache_root: Path = _DEFAULT_CACHE_ROOT,
) -> Optional[EvidenceBundle]:
    """Read a cached EvidenceBundle if present, else None."""
    p = cache_dir_for(
        session_id, subject, vision_model, fps, chunking, cache_root=cache_root
    ) / "evidence_bundle.json"
    if not p.exists():
        return None
    return EvidenceBundle.model_validate_json(p.read_text())


# ─── Build ────────────────────────────────────────────────────────────────


def _chunk_minutes_from_chunking(chunking: str) -> int:
    """Map our chunking token to vision_observe's chunk_minutes kwarg."""
    if chunking == "5min":
        return 5
    if chunking == "10min":
        return 10
    if chunking == "single":
        # vision_observe expects > 0 minutes; "single" means one chunk that
        # spans the whole video. Pass a number big enough to never split.
        return 24 * 60  # 1 day
    raise ValueError(f"unknown chunking token {chunking!r}")


def build_evidence_bundle(
    session_id: str,
    llm: LLMAdapter,
    *,
    subject: Optional[str] = None,
    vision_model: Optional[str] = None,
    fps: Optional[float] = None,
    chunking: str = DEFAULT_CHUNKING,
    activity_context: Optional[str] = None,
    teacher_id: Optional[str] = None,
    force: bool = False,
    cache_root: Path = _DEFAULT_CACHE_ROOT,
    phase_extraction: bool = True,
    tightened_rules: bool = True,
    sva: Optional[SessionVideoArtifacts] = None,
) -> EvidenceBundle:
    """Idempotent: returns the cached bundle if present; otherwise runs
    `vision_observe` + assembles + persists the bundle.

    `subject` defaults to the session camera's subject (via
    `resolve_session_segments`). `vision_model` defaults to
    `llm.vision_model`.

    `activity_context` + `teacher_id` are per-session metadata that flows
    into the vision-pass prompt (vision.md already has Jinja hooks for
    `session.activity_context`). They're recorded on the bundle for
    traceability but are NOT part of the cache key — pass `--force` when
    they change for the same session.

    The function requires a built session-video cache to read boundaries
    from, so it first calls `build_session_video(session_id, llm)` —
    which is itself idempotent and cheap when the cache is hot.
    """
    # Always resolve segments — we need at minimum the date for SessionMeta.
    # When subject wasn't supplied, also use the segments to derive it.
    # Skip when `sva` is provided AND we already know subject (caller is on
    # the --trimmed-video fast path that bypasses raw-segment discovery).
    segs = None
    if sva is None or subject is None:
        segs = resolve_session_segments(session_id)
        if not segs:
            raise ValueError(f"no segments for {session_id!r}")
        if subject is None:
            subject = segs[0].subject

    if vision_model is None:
        vision_model = llm.vision_model

    cdir = cache_dir_for(
        session_id, subject, vision_model, fps, chunking, cache_root=cache_root
    )
    bundle_path = cdir / "evidence_bundle.json"

    if bundle_path.exists() and not force:
        log.info(
            f"[{session_id}] evidence cache HIT — loading from "
            f"{bundle_path.relative_to(_PROJECT_ROOT) if str(bundle_path).startswith(str(_PROJECT_ROOT)) else bundle_path}"
        )
        cached = EvidenceBundle.model_validate_json(bundle_path.read_text())
        # Warn if the caller passed context that doesn't match the cached
        # values — the cache key doesn't include context, so a mismatch
        # is silent unless we surface it here.
        if activity_context is not None and cached.activity_context != activity_context:
            log.warning(
                f"[{session_id}] cached bundle has "
                f"activity_context={cached.activity_context!r}, but caller "
                f"passed {activity_context!r}. Pass --force to rebuild."
            )
        if teacher_id is not None and cached.teacher_id != teacher_id:
            log.warning(
                f"[{session_id}] cached bundle has teacher_id="
                f"{cached.teacher_id!r}, but caller passed {teacher_id!r}. "
                "Pass --force to rebuild."
            )
        return cached

    log.info(
        f"[{session_id}] evidence cache MISS — building "
        f"(vision_model={vision_model}, fps={fps}, chunking={chunking})"
    )

    # Ensure the session-video cache is built (idempotent + cheap on hit).
    # When the caller passed a pre-built `sva` (--trimmed-video fast path),
    # use it directly and don't re-run the combine/boundary stages.
    if sva is None:
        sva = build_session_video(session_id, llm=llm)

    # Read boundaries (already produced by build_session_video). On the
    # fast path there's no boundaries.json — derive duration from the
    # trimmed video itself via ffprobe.
    if sva.boundaries_json.exists():
        boundaries = json.loads(sva.boundaries_json.read_text())
        duration_min = boundaries_class_duration_minutes(boundaries, sva)
    else:
        boundaries = {"source": "trimmed-video fast path; no boundary detection"}
        duration_min = max(1, int(round(video_duration_seconds(sva.trimmed) / 60)))

    # Run vision_observe on the TRIMMED video (class window only). Uses a
    # SessionMeta scoped to this session so observe-prompt metadata is right.
    recorded_at = (
        segs[0].starts_at.date() if segs else _date_from_session_id(session_id)
    )
    sess_meta_kwargs = dict(
        session_id=session_id,
        recorded_at=recorded_at,
        duration_minutes=duration_min,
        subject=subject,
        video_path=sva.trimmed,
    )
    if activity_context is not None:
        sess_meta_kwargs["activity_context"] = activity_context
    if teacher_id is not None:
        sess_meta_kwargs["teacher_id"] = teacher_id
    sess_meta = SessionMeta(**sess_meta_kwargs)
    transcript, observations, phases, explanations, disturbances = vision_observe(
        sess_meta, llm,
        chunk_minutes=_chunk_minutes_from_chunking(chunking),
        phase_extraction=phase_extraction,
        tightened_rules=tightened_rules,
    )

    bundle = EvidenceBundle(
        session_id=session_id,
        subject=subject,
        vision_model=vision_model,
        vision_fps=fps_token(fps),
        chunking=chunking,
        boundaries=boundaries,
        transcript=[s.model_dump() for s in transcript.segments],
        observations=[o.model_dump() for o in observations.observations],
        phases=phases or None,
        explanations=explanations or None,
        disturbances=disturbances or None,
        transcript_source_model=transcript.source_model,
        observations_source_model=observations.source_model,
        activity_context=activity_context,
        teacher_id=teacher_id,
        built_at=datetime.utcnow().isoformat(timespec="seconds"),
        source_run_dir=str(sva.session_dir),
    )
    cdir.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(bundle.model_dump_json(indent=2))
    log.info(
        f"[{session_id}] evidence cache POPULATED — "
        f"{len(bundle.transcript)} transcript segs, "
        f"{len(bundle.observations)} observations"
    )
    return bundle


def _date_from_session_id(session_id: str):
    """Parse the YYYY-MM-DD prefix of a session_id into a `date`."""
    from datetime import date
    return date.fromisoformat(session_id.split("__", 1)[0])


def boundaries_class_duration_minutes(boundaries: dict, sva) -> int:
    """Approximate the trimmed video duration in minutes from boundaries.
    Falls back to a generous 120 if the boundary fields are missing —
    matches the legacy script's behaviour."""
    first = boundaries.get("first_child_visible_at")
    last = boundaries.get("last_child_visible_at")
    if first and last:
        try:
            from pipeline.boundaries import _parse_hms
            first_s = _parse_hms(first) or 0
            last_s = _parse_hms(last) or 0
            mins = max(1, int(round((last_s - first_s) / 60)))
            return mins
        except Exception:
            pass
    return 120


# ─── Enrich with Shape-A-derived data ─────────────────────────────────────


# enrich_bundle_with_shape_a archived 2026-06-10 →
# pipeline/_archive/evidence_legacy.py (zero live callers; restore when
# the Shape A → enrich → Shape B flow gets wired up).


def _parse_fps_token(token: str) -> Optional[float]:
    """Reverse of fps_token(). 'fps-default' → None; 'fps-0.30' → 0.30."""
    if token == "fps-default":
        return None
    if token.startswith("fps-"):
        try:
            return float(token[len("fps-"):])
        except ValueError:
            pass
    raise ValueError(f"can't parse fps token {token!r}")
