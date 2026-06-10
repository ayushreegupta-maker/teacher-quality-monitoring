"""
SQLite-backed metadata + index for the TQM pipeline.

Tables hold structured metadata. Raw artifacts (videos, JSONs) stay on disk;
the DB references them by path. This keeps the DB small (under ~100 MB even
at 100K sessions) while still answering the queries the pipeline needs:

- What's the activity context for camera X on date Y? (per-day lookup)
- Which sessions are queued / failed?
- All scores for classroom Z this week.
- Calibration pairs (model score vs team scores per dim).

Schema is intentionally minimal for Phase 1 pilot — expect to evolve. Every
table has `CREATE TABLE IF NOT EXISTS` for safe re-runs.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "tqm.db"

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS classrooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id TEXT NOT NULL DEFAULT 'default',
    camera_id TEXT NOT NULL,
    name TEXT NOT NULL,
    age_range TEXT DEFAULT '3-5 years',
    default_subject TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(school_id, camera_id)
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    default_activity_context TEXT,
    default_rubric_set TEXT,  -- comma-separated rubric labels
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS camera_day_activity (
    camera_id TEXT NOT NULL,
    recorded_date TEXT NOT NULL,  -- ISO date YYYY-MM-DD
    activity_id INTEGER NOT NULL,
    custom_context TEXT,           -- per-day override of activity's default context
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (camera_id, recorded_date),
    FOREIGN KEY (activity_id) REFERENCES activities(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    school_id TEXT NOT NULL DEFAULT 'default',
    classroom_id INTEGER,
    camera_id TEXT,
    recorded_at TIMESTAMP NOT NULL,
    video_path TEXT NOT NULL,
    activity_id INTEGER,
    activity_context TEXT,         -- resolved context used for this run
    status TEXT NOT NULL DEFAULT 'queued',  -- queued, processing, vision_done, scored, failed
    data_dir TEXT,                 -- where artifacts live on disk
    duration_seconds REAL,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (classroom_id) REFERENCES classrooms(id),
    FOREIGN KEY (activity_id) REFERENCES activities(id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_classroom_date
    ON sessions(school_id, classroom_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_sessions_status
    ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_camera_date
    ON sessions(camera_id, recorded_at);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    rubric_name TEXT NOT NULL,           -- e.g. 'playground', 'toy_design'
    rubric_version TEXT NOT NULL,
    dimension TEXT NOT NULL,
    score TEXT NOT NULL,                 -- numeric as string, or 'insufficient_evidence'
    confidence TEXT,                     -- 'high' | 'medium' | 'low'
    anchor_matched TEXT,
    scorer_notes TEXT,
    evidence_json TEXT,                  -- full evidence array, JSON-encoded
    prompt_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    UNIQUE(session_id, rubric_name, rubric_version, dimension)
);

CREATE INDEX IF NOT EXISTS idx_scores_session_rubric
    ON scores(session_id, rubric_name);
CREATE INDEX IF NOT EXISTS idx_scores_dimension
    ON scores(rubric_name, dimension);

CREATE TABLE IF NOT EXISTS team_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    rater_name TEXT NOT NULL,            -- 'Akshay', 'Pari', 'Ayesha', etc.
    rubric_name TEXT NOT NULL,
    rubric_version TEXT,
    dimension TEXT NOT NULL,
    score TEXT NOT NULL,
    comments TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, rater_name, rubric_name, dimension)
);

CREATE INDEX IF NOT EXISTS idx_team_scores_session
    ON team_scores(session_id);

CREATE TABLE IF NOT EXISTS boundaries (
    session_id TEXT PRIMARY KEY,
    first_child_visible_at TEXT,        -- HH:MM:SS or NULL
    last_child_visible_at TEXT,
    confidence TEXT,
    notes TEXT,
    source_model TEXT,
    prompt_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
"""


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection. Caller is responsible for closing."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_conn(db_path: Path = DEFAULT_DB_PATH):
    """Context manager: auto-commit on success, close always."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Create tables (idempotent) and record the schema version."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with db_conn(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, notes) VALUES (?, ?)",
            (SCHEMA_VERSION, "initial schema"),
        )
    log.info(f"DB initialised at {db_path}")


# ---------- Classrooms ----------

def upsert_classroom(
    school_id: str,
    camera_id: str,
    name: str,
    age_range: str = "3-5 years",
    default_subject: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    """Create-or-find a classroom. Returns its integer id."""
    with db_conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO classrooms "
            "(school_id, camera_id, name, age_range, default_subject) "
            "VALUES (?, ?, ?, ?, ?)",
            (school_id, camera_id, name, age_range, default_subject),
        )
        row = conn.execute(
            "SELECT id FROM classrooms WHERE school_id = ? AND camera_id = ?",
            (school_id, camera_id),
        ).fetchone()
        return row["id"]


def get_classroom_by_camera(
    camera_id: str,
    school_id: str = "default",
    db_path: Path = DEFAULT_DB_PATH,
) -> Optional[dict]:
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM classrooms WHERE school_id = ? AND camera_id = ?",
            (school_id, camera_id),
        ).fetchone()
        return dict(row) if row else None


def list_classrooms(
    school_id: str = "default",
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    with db_conn(db_path) as conn:
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM classrooms WHERE school_id = ? ORDER BY camera_id",
                (school_id,),
            ).fetchall()
        ]


# ---------- Activities ----------

def upsert_activity(
    name: str,
    default_activity_context: Optional[str] = None,
    default_rubric_set: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    with db_conn(db_path) as conn:
        if default_activity_context is not None or default_rubric_set is not None:
            # If caller provided a context/rubric_set, update existing row
            conn.execute(
                """INSERT INTO activities (name, default_activity_context, default_rubric_set)
                   VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       default_activity_context = COALESCE(excluded.default_activity_context, default_activity_context),
                       default_rubric_set = COALESCE(excluded.default_rubric_set, default_rubric_set)""",
                (name, default_activity_context, default_rubric_set),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO activities (name) VALUES (?)",
                (name,),
            )
        row = conn.execute(
            "SELECT id FROM activities WHERE name = ?", (name,),
        ).fetchone()
        return row["id"]


def list_activities(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    with db_conn(db_path) as conn:
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM activities ORDER BY name"
            ).fetchall()
        ]


# ---------- Per-camera-per-day activity ----------

def set_camera_day_activity(
    camera_id: str,
    recorded_date: date,
    activity_name: str,
    custom_context: Optional[str] = None,
    notes: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Record what activity was happening at a given camera on a given date.
    Auto-creates the activity row if it doesn't exist yet."""
    activity_id = upsert_activity(activity_name, db_path=db_path)
    with db_conn(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO camera_day_activity
               (camera_id, recorded_date, activity_id, custom_context, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (camera_id, recorded_date.isoformat(), activity_id, custom_context, notes),
        )


def get_activity_for_camera_day(
    camera_id: str,
    recorded_date: date,
    db_path: Path = DEFAULT_DB_PATH,
) -> Optional[dict]:
    """Look up the activity assigned to (camera_id, date). Returns dict with
    `activity_name`, `activity_context` (custom OR default), `default_rubric_set`,
    `notes`. Returns None if no assignment exists."""
    with db_conn(db_path) as conn:
        row = conn.execute(
            """SELECT a.name AS activity_name,
                      COALESCE(cda.custom_context, a.default_activity_context) AS activity_context,
                      a.default_rubric_set,
                      cda.notes
               FROM camera_day_activity cda
               JOIN activities a ON a.id = cda.activity_id
               WHERE cda.camera_id = ? AND cda.recorded_date = ?""",
            (camera_id, recorded_date.isoformat()),
        ).fetchone()
        return dict(row) if row else None


def list_camera_day_activities(
    recorded_date: Optional[date] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """List per-day assignments; optionally filter to a single date."""
    sql = """SELECT cda.camera_id, cda.recorded_date, a.name AS activity_name,
                    COALESCE(cda.custom_context, a.default_activity_context) AS activity_context,
                    cda.notes
             FROM camera_day_activity cda
             JOIN activities a ON a.id = cda.activity_id"""
    params: list[Any] = []
    if recorded_date is not None:
        sql += " WHERE cda.recorded_date = ?"
        params.append(recorded_date.isoformat())
    sql += " ORDER BY cda.recorded_date DESC, cda.camera_id"
    with db_conn(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------- Sessions ----------

def register_session(
    session_id: str,
    recorded_at: datetime,
    video_path: Path,
    school_id: str = "default",
    classroom_id: Optional[int] = None,
    camera_id: Optional[str] = None,
    activity_id: Optional[int] = None,
    activity_context: Optional[str] = None,
    data_dir: Optional[Path] = None,
    status: str = "queued",
    duration_seconds: Optional[float] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    with db_conn(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, school_id, classroom_id, camera_id, recorded_at,
                video_path, activity_id, activity_context, status, data_dir,
                duration_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, school_id, classroom_id, camera_id,
                recorded_at.isoformat() if hasattr(recorded_at, "isoformat") else str(recorded_at),
                str(video_path), activity_id, activity_context, status,
                str(data_dir) if data_dir else None,
                duration_seconds,
            ),
        )


def update_session_status(
    session_id: str,
    status: str,
    error: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    with db_conn(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE session_id = ?",
            (status, error, session_id),
        )


def list_sessions(
    school_id: str = "default",
    classroom_id: Optional[int] = None,
    status: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    sql = """SELECT s.*, a.name AS activity_name, c.name AS classroom_name
             FROM sessions s
             LEFT JOIN activities a ON a.id = s.activity_id
             LEFT JOIN classrooms c ON c.id = s.classroom_id
             WHERE s.school_id = ?"""
    params: list[Any] = [school_id]
    if classroom_id is not None:
        sql += " AND s.classroom_id = ?"
        params.append(classroom_id)
    if status is not None:
        sql += " AND s.status = ?"
        params.append(status)
    if start_date is not None:
        sql += " AND DATE(s.recorded_at) >= ?"
        params.append(start_date.isoformat())
    if end_date is not None:
        sql += " AND DATE(s.recorded_at) <= ?"
        params.append(end_date.isoformat())
    sql += " ORDER BY s.recorded_at DESC"
    with db_conn(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_queued_sessions(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Sessions that need processing (queued or previously failed)."""
    with db_conn(db_path) as conn:
        return [
            dict(r) for r in conn.execute(
                """SELECT * FROM sessions
                   WHERE status IN ('queued', 'failed')
                   ORDER BY recorded_at"""
            ).fetchall()
        ]


# ---------- Scores ----------

def save_dimension_score(
    session_id: str,
    rubric_name: str,
    rubric_version: str,
    dimension: str,
    dim_score: Any,  # pipeline.types.DimensionScore
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Persist one DimensionScore row."""
    evidence_payload = (
        [e.model_dump(mode="json") if hasattr(e, "model_dump") else e for e in dim_score.evidence]
        if hasattr(dim_score, "evidence") else []
    )
    with db_conn(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO scores
               (session_id, rubric_name, rubric_version, dimension, score, confidence,
                anchor_matched, scorer_notes, evidence_json, prompt_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, rubric_name, rubric_version, dimension,
                str(dim_score.score), dim_score.confidence,
                dim_score.anchor_matched, dim_score.scorer_notes,
                json.dumps(evidence_payload),
                dim_score.prompt_hash,
            ),
        )


def save_session_scores(
    session_id: str,
    rubric_name: str,
    session_scores: Any,  # pipeline.types.SessionScores
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Save every dimension from a SessionScores model into the scores table."""
    for dim_id, dim_score in session_scores.scores.items():
        save_dimension_score(
            session_id, rubric_name, session_scores.rubric_version,
            dim_id, dim_score, db_path,
        )


def get_scores_for_session(
    session_id: str,
    rubric_name: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    sql = "SELECT * FROM scores WHERE session_id = ?"
    params: list[Any] = [session_id]
    if rubric_name is not None:
        sql += " AND rubric_name = ?"
        params.append(rubric_name)
    sql += " ORDER BY rubric_name, dimension"
    with db_conn(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------- Team scores (calibration data) ----------

def save_team_score(
    session_id: str,
    rater_name: str,
    rubric_name: str,
    rubric_version: Optional[str],
    dimension: str,
    score: Any,
    comments: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    with db_conn(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO team_scores
               (session_id, rater_name, rubric_name, rubric_version, dimension, score, comments)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, rater_name, rubric_name, rubric_version, dimension, str(score), comments),
        )


def get_calibration_pairs(
    rubric_name: str,
    dimension: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """For every (session, dimension) pair where BOTH the model and at least
    one team rater have scored, return the model score and the team scores."""
    sql = """
    SELECT
        s.session_id, s.dimension,
        s.score AS model_score, s.confidence AS model_confidence,
        ts.rater_name, ts.score AS team_score, ts.comments AS team_comments
    FROM scores s
    JOIN team_scores ts ON ts.session_id = s.session_id
                       AND ts.rubric_name = s.rubric_name
                       AND ts.dimension = s.dimension
    WHERE s.rubric_name = ?"""
    params: list[Any] = [rubric_name]
    if dimension is not None:
        sql += " AND s.dimension = ?"
        params.append(dimension)
    sql += " ORDER BY s.session_id, s.dimension, ts.rater_name"
    with db_conn(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------- Boundaries ----------

def save_boundaries(
    session_id: str,
    boundary_detection: Any,  # pipeline.types.BoundaryDetection
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    with db_conn(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO boundaries
               (session_id, first_child_visible_at, last_child_visible_at,
                confidence, notes, source_model, prompt_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                boundary_detection.first_child_visible_at,
                boundary_detection.last_child_visible_at,
                boundary_detection.confidence,
                boundary_detection.notes,
                boundary_detection.source_model,
                boundary_detection.prompt_hash,
            ),
        )


def get_boundaries(
    session_id: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> Optional[dict]:
    with db_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM boundaries WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None
