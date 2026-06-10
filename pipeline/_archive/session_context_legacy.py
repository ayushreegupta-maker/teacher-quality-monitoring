"""
Archived chunks of pipeline.session_context — 2026-06-10.

Extracted once they had zero live callers:

  parse_camera_and_recorded_at(video_path)
      Filename-based camera + datetime extractor for the OLD CCTV naming
      convention (`D06_YYYYMMDDHHMMSS.mp4`) and the date-prefix style
      (`20250918_activity_5_balloon_dance.mp4`). The new naming
      (`D28_hrbr_art_YYYYMMDD_HHMMSS.mp4`) is handled by
      `pipeline.session_context._SEGMENT_RE` in the live module.

  resolve_session_context(video_path, ...)
      Returns a dict of (camera_id, classroom_id, activity_id,
      activity_context, default_rubric_set, ...) by joining
      cctv_cameras.xlsx + the tqm.db tables. The new flow doesn't use
      these tables — `cctv_cameras.xlsx`'s `subject` column is the
      only camera context the live scoring path needs. Will likely
      come back in some form when task #33 (teacher_schedule) lands.

  _activity_by_name / _activity_id_by_name
      Helpers for resolve_session_context's hint-based fallback.

This module imports `adapters._archive.db` because the live
`adapters.db` was also archived on the same date (its sole live caller
was resolve_session_context).
"""
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from adapters._archive import db

log = logging.getLogger(__name__)

_CAM_DATETIME_RE = re.compile(r"^([A-Za-z]\d+)_(\d{14})")
_DATE_ONLY_RE = re.compile(r"^(\d{8})(?:_|$)")


def parse_camera_and_recorded_at(
    video_path: Path,
) -> tuple[Optional[str], Optional[datetime]]:
    """Extract (camera_id, recorded_at) from the video filename.

    Supports two conventions:
      (a) `CAM_YYYYMMDDHHMMSS` — extracts both camera and timestamp.
      (b) `YYYYMMDD_anything`  — extracts date only (no camera).
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
    """Legacy DB-backed session-context resolver. See module docstring."""
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

    if camera_id is not None:
        classroom = db.get_classroom_by_camera(camera_id, db_path=db_path)
        if classroom:
            out["classroom_id"] = classroom["id"]
            out["classroom_name"] = classroom["name"]
        else:
            log.info(f"no classroom registered for camera={camera_id}")

    activity_row = None
    if camera_id is not None and recorded_date is not None:
        activity_row = db.get_activity_for_camera_day(
            camera_id, recorded_date, db_path=db_path
        )
        if activity_row is None:
            log.info(
                f"no activity assigned for camera={camera_id} on {recorded_date}"
            )

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

    out["activity_name"] = activity_row["activity_name"]
    out["default_rubric_set"] = activity_row.get("default_rubric_set")
    out["notes"] = activity_row.get("notes")
    if out["source"] in ("none", "fallback"):
        out["source"] = "db"

    db_context = activity_row.get("activity_context")
    if db_context:
        out["activity_context"] = db_context
    elif fallback_activity_context:
        out["activity_context"] = fallback_activity_context
        out["source"] = f"{out['source']}_with_fallback_context"

    activity_id = _activity_id_by_name(
        activity_row["activity_name"], db_path=db_path
    )
    out["activity_id"] = activity_id

    return out


def _activity_by_name(
    name: str, db_path: Path = db.DEFAULT_DB_PATH
) -> Optional[dict]:
    """Look up a full activity row by name (case-sensitive)."""
    with db.db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM activities WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None


def _activity_id_by_name(
    name: str, db_path: Path = db.DEFAULT_DB_PATH
) -> Optional[int]:
    row = _activity_by_name(name, db_path=db_path)
    return row["id"] if row else None
