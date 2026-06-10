"""
Resolve session metadata from a video file: parse `CAM_YYYYMMDDHHMMSS.mp4`-style
filenames, look up the activity context for that (camera, date) from the SQLite
DB, and produce a single dict the pipelines can lean on.

This is the bridge between the on-disk video files and the structured metadata
in `data/tqm.db`. The pipelines (`score_long_video.py`, `batch_score.py`) call
into this module rather than parsing filenames or hitting the DB directly.
"""

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import adapters.db as db

log = logging.getLogger(__name__)

# Filename patterns we recognise:
#   (a) CCTV style: D06_20250919105616.mp4 → camera="D06", ts=2025-09-19 10:56:16
#   (b) Date-prefix style: 20250918_activity_5_balloon_dance.mp4 → camera=None, ts=2025-09-18
_CAM_DATETIME_RE = re.compile(r"^([A-Za-z]\d+)_(\d{14})")
_DATE_ONLY_RE = re.compile(r"^(\d{8})(?:_|$)")


def parse_camera_and_recorded_at(video_path: Path) -> tuple[Optional[str], Optional[datetime]]:
    """Extract (camera_id, recorded_at) from the video filename.

    Supports two conventions:
      (a) `CAM_YYYYMMDDHHMMSS` — extracts both camera and timestamp.
      (b) `YYYYMMDD_anything`  — extracts date only (no camera).

    Returns (None, None) if neither matches. Defensive: the rest of the
    pipeline must keep working when filenames don't follow either convention.
    """
    stem = video_path.stem

    m = _CAM_DATETIME_RE.match(stem)
    if m:
        camera_id = m.group(1)
        try:
            ts = datetime.strptime(m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            ts = None
        return camera_id, ts

    m2 = _DATE_ONLY_RE.match(stem)
    if m2:
        try:
            ts = datetime.strptime(m2.group(1), "%Y%m%d")
            return None, ts
        except ValueError:
            return None, None

    return None, None


def resolve_session_context(
    video_path: Path,
    camera_id: Optional[str] = None,
    recorded_at: Optional[date] = None,
    fallback_activity_context: Optional[str] = None,
    activity_name_hint: Optional[str] = None,
    db_path: Path = db.DEFAULT_DB_PATH,
) -> dict:
    """Resolve everything the pipeline needs to register a session.

    Resolution order:
    1. Use explicit `camera_id` / `recorded_at` if provided by the caller.
    2. Otherwise, parse them from the video filename.
    3. Look up the classroom for `camera_id` (if any).
    4. Look up the per-day activity assignment for (camera_id, recorded_date).
    5. If step 4 missed AND `activity_name_hint` was provided, look up the
       activity by name directly (useful when the filename doesn't carry a
       camera id, e.g. `20250918_activity_5_balloon_dance.mp4`).
    6. If no activity is registered, fall back to `fallback_activity_context`.

    Returns a dict with keys:
        camera_id, recorded_at (datetime|None), recorded_date (date|None),
        classroom_id (int|None), classroom_name (str|None),
        activity_id (int|None), activity_name (str|None),
        activity_context (str|None), default_rubric_set (str|None),
        notes (str|None), source (str — describes how the activity was resolved)
    """
    # 1+2: derive camera/recorded_at if missing
    parsed_camera, parsed_dt = parse_camera_and_recorded_at(video_path)
    camera_id = camera_id or parsed_camera
    recorded_dt = (
        datetime.combine(recorded_at, datetime.min.time())
        if isinstance(recorded_at, date) and not isinstance(recorded_at, datetime)
        else (recorded_at or parsed_dt)
    )
    recorded_date = recorded_dt.date() if isinstance(recorded_dt, datetime) else None

    out: dict = {
        "camera_id": camera_id,
        "recorded_at": recorded_dt,
        "recorded_date": recorded_date,
        "classroom_id": None,
        "classroom_name": None,
        "activity_id": None,
        "activity_name": None,
        "activity_context": fallback_activity_context,
        "default_rubric_set": None,
        "notes": None,
        "source": "fallback" if fallback_activity_context else "none",
    }

    # 3: classroom lookup (only meaningful if we have a camera_id)
    if camera_id is not None:
        classroom = db.get_classroom_by_camera(camera_id, db_path=db_path)
        if classroom:
            out["classroom_id"] = classroom["id"]
            out["classroom_name"] = classroom["name"]
        else:
            log.info(f"no classroom registered for camera={camera_id}")

    # 4: per-day activity lookup (only when both camera_id and date are known)
    activity_row = None
    if camera_id is not None and recorded_date is not None:
        activity_row = db.get_activity_for_camera_day(
            camera_id, recorded_date, db_path=db_path
        )
        if activity_row is None:
            log.info(
                f"no activity assigned for camera={camera_id} on {recorded_date}"
            )

    # 5: fall back to activity_name_hint lookup if camera/date didn't yield one
    if activity_row is None and activity_name_hint:
        hint_row = _activity_by_name(activity_name_hint, db_path=db_path)
        if hint_row is not None:
            activity_row = {
                "activity_name": hint_row["name"],
                "activity_context": hint_row.get("default_activity_context"),
                "default_rubric_set": hint_row.get("default_rubric_set"),
                "notes": None,
            }
            log.info(
                f"resolved activity via name hint: '{activity_name_hint}' "
                f"(camera/date lookup missed)"
            )
            out["source"] = "db_name_hint"
        else:
            log.info(
                f"activity_name_hint '{activity_name_hint}' not found in DB"
            )

    if activity_row is None:
        if camera_id is None:
            log.info(
                f"could not infer camera_id from '{video_path.name}' and "
                f"no activity_name_hint matched; using "
                f"{'fallback context' if fallback_activity_context else 'no context'}"
            )
        return out

    # Look up the activity_id (the join in get_activity_for_camera_day strips it
    # — re-fetch from activities table for FK linkage)
    out["activity_name"] = activity_row["activity_name"]
    out["default_rubric_set"] = activity_row.get("default_rubric_set")
    out["notes"] = activity_row.get("notes")
    # Preserve the source marker set above (e.g. "db_name_hint"); only set to
    # the camera/date default if it's still the initial value.
    if out["source"] in ("none", "fallback"):
        out["source"] = "db"

    # Prefer DB-resolved context; only fall back if DB context is empty
    db_context = activity_row.get("activity_context")
    if db_context:
        out["activity_context"] = db_context
    elif fallback_activity_context:
        out["activity_context"] = fallback_activity_context
        out["source"] = f"{out['source']}_with_fallback_context"

    # Re-fetch activity_id from the activities table (the join SELECT'd name+context
    # but not the id; cheaper to add a second small query than to alter the join)
    activity_id = _activity_id_by_name(activity_row["activity_name"], db_path=db_path)
    out["activity_id"] = activity_id

    return out


def _activity_by_name(name: str, db_path: Path = db.DEFAULT_DB_PATH) -> Optional[dict]:
    """Look up a full activity row by name (case-sensitive). Returns None if missing."""
    with db.db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM activities WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None


def _activity_id_by_name(name: str, db_path: Path = db.DEFAULT_DB_PATH) -> Optional[int]:
    row = _activity_by_name(name, db_path=db_path)
    return row["id"] if row else None
