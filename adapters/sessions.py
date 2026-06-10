"""
Path helper for legacy session directories.

The single live entry point is `session_dir(session_id) → Path`.
register_session / load_session / list_sessions archived 2026-06-10 to
adapters/_archive/sessions_legacy.py (zero live callers).
"""
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
SESSIONS_DIR = DATA_ROOT / "sessions"


def session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id
