import logging
from typing import Optional

from adapters.llm import LLMAdapter, parse_json_lenient, prompt_hash
from adapters.sessions import session_dir
from pipeline.render import _jinja_env, load_prompt, split_system_user
from pipeline.types import BoundaryDetection, SessionMeta

log = logging.getLogger(__name__)


def _parse_hms(s: str) -> Optional[int]:
    """Parse 'HH:MM:SS' (or 'H:MM:SS') into total seconds. Returns None on failure."""
    try:
        parts = s.strip().split(":")
        if len(parts) != 3:
            return None
        h, m, sec = (int(p) for p in parts)
        return h * 3600 + m * 60 + sec
    except (ValueError, AttributeError):
        return None


def _format_hms(total_seconds: int) -> str:
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _subtract_clocks(event_clock: str, start_clock: str) -> Optional[str]:
    """Compute elapsed time as (event_clock − start_clock), both in HH:MM:SS.
    Returns HH:MM:SS or None on parse error / negative result.

    Used to convert wall-clock readings (e.g. '09:42:00' minus '09:00:00') into
    video-relative elapsed time ('00:42:00'). Assumes both clocks are on the
    same calendar day; does NOT handle midnight rollovers. Returns None if the
    event time is before the start time (likely indicates the model misread
    one of the two clocks).
    """
    event_total = _parse_hms(event_clock)
    start_total = _parse_hms(start_clock)
    if event_total is None or start_total is None:
        return None
    delta = event_total - start_total
    if delta < 0:
        return None
    return _format_hms(delta)


def detect_boundaries(session: SessionMeta, llm: LLMAdapter) -> BoundaryDetection:
    """One Gemini call on the WHOLE video to identify when the class began and
    ended based on child presence in the play area. Result is persisted as
    boundaries_<hash>.json in the session dir.

    Returns a BoundaryDetection with HH:MM:SS timestamps (or None if no child
    detected at all). The caller uses these timestamps to compute the
    'before' and 'after' windows for segment extraction.

    v0.7.0 behaviour: if the model returns wall-clock readings for the three
    key moments (video start, first child, last child), the pipeline derives
    first_child_visible_at / last_child_visible_at by subtracting in Python
    rather than asking the model to do (and bungle) the conversion. This
    sidesteps the systemic wall-clock leak we observed across CCTV recordings
    where the model strips the hours digit instead of subtracting properly.
    """
    template_text = load_prompt("detect_boundaries")
    rendered = _jinja_env().from_string(template_text).render(
        session=session.model_dump(mode="json"),
    )
    system, user = split_system_user(rendered)
    full_prompt = f"{system}\n\n{user}"

    log.info(f"[{session.session_id}] boundary detection: uploading + analysing whole video")
    video_file = llm.upload_video(session.video_path)
    raw = llm.call_gemini_video(
        prompt=full_prompt,
        video_file=video_file,
        # Whole-video call (no start/end_seconds) — chunking would defeat the purpose
    )

    p_hash = prompt_hash(template_text)
    sd = session_dir(session.session_id)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / f"boundaries_raw_{p_hash}.txt").write_text(raw)

    parsed = parse_json_lenient(raw)
    result = BoundaryDetection.model_validate(parsed)
    result.session_id = session.session_id
    result.source_model = llm.vision_model
    result.prompt_hash = p_hash

    # v0.7.0: if the model reported wall-clock readings, compute elapsed from
    # them. The model is reliable at READING the clock; unreliable at the
    # wall-clock-to-elapsed conversion. So we let the model do the vision and
    # do the math in Python.
    _derive_elapsed_from_walls(result, session.session_id)

    (sd / f"boundaries_{p_hash}.json").write_text(result.model_dump_json(indent=2))
    log.info(
        f"[{session.session_id}] boundaries: "
        f"first_child={result.first_child_visible_at} "
        f"last_child={result.last_child_visible_at} "
        f"confidence={result.confidence}"
    )
    if result.video_start_wall_clock:
        log.info(
            f"[{session.session_id}] wall-clock readings — "
            f"start={result.video_start_wall_clock} "
            f"first_child={result.first_child_wall_clock} "
            f"last_child={result.last_child_wall_clock}"
        )
    if result.first_child_evidence:
        log.info(f"[{session.session_id}] first_child evidence: {result.first_child_evidence}")
    if result.last_child_evidence:
        log.info(f"[{session.session_id}] last_child evidence: {result.last_child_evidence}")
    if result.self_check_passed is False:
        log.warning(
            f"[{session.session_id}] boundary self_check FAILED — model flagged "
            f"its own answer as uncertain. Treat output as low-confidence."
        )
    if result.notes:
        log.info(f"[{session.session_id}] boundary notes: {result.notes}")
    return result


def _derive_elapsed_from_walls(result: BoundaryDetection, session_id: str) -> None:
    """If wall-clock readings are populated, derive first/last_child_visible_at
    by subtracting from video_start_wall_clock. Logs the derivation. Mutates
    `result` in place.

    Three guards:
    1. video_start_wall_clock must be present (anchor for subtraction).
    2. Each event clock that's present produces a derived elapsed value.
    3. If derived elapsed time is BEFORE the model's reported elapsed value
       by >5min, the model's elapsed wasn't a wall-clock leak — log a notice
       but trust the derived value (it's mathematically grounded).
    """
    start = result.video_start_wall_clock
    if not start:
        return  # no anchor — keep whatever the model put in *_visible_at

    if result.first_child_wall_clock:
        derived = _subtract_clocks(result.first_child_wall_clock, start)
        if derived is not None:
            old = result.first_child_visible_at
            result.first_child_visible_at = derived
            if old and old != derived:
                log.info(
                    f"[{session_id}] first_child_visible_at: derived {derived} "
                    f"from wall-clocks (model originally said {old})"
                )
            else:
                log.info(
                    f"[{session_id}] first_child_visible_at: derived {derived} "
                    f"from wall-clocks {result.first_child_wall_clock} − {start}"
                )

    if result.last_child_wall_clock:
        derived = _subtract_clocks(result.last_child_wall_clock, start)
        if derived is not None:
            old = result.last_child_visible_at
            result.last_child_visible_at = derived
            if old and old != derived:
                log.info(
                    f"[{session_id}] last_child_visible_at: derived {derived} "
                    f"from wall-clocks (model originally said {old})"
                )
            else:
                log.info(
                    f"[{session_id}] last_child_visible_at: derived {derived} "
                    f"from wall-clocks {result.last_child_wall_clock} − {start}"
                )
