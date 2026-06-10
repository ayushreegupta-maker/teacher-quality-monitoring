"""
Wall-clock <-> elapsed-time helpers used by pipeline.session_video.

The original public `detect_boundaries(session, llm)` was inlined into
`session_video.stage2_detect_boundaries` so it could pass fps=0.3 to
`call_gemini_video`. Archived 2026-06-10 to
`pipeline/_archive/boundaries_legacy.py`. The internal helpers below
(`_parse_hms`, `_format_hms`, `_subtract_clocks`,
`_derive_elapsed_from_walls`) stayed live because session_video imports
them directly.
"""
import logging
from typing import Optional

from pipeline.types import BoundaryDetection

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
