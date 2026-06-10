"""
Archived chunks of adapters.sessions — 2026-06-10.

Three functions extracted because they had zero live callers:

  register_session(meta)        — create data/sessions/<id>/meta.json
  load_session(session_id)      — read it back
  list_sessions()               — list all sessions with meta.json

The new session-video cache layer (pipeline.session_video) builds
data/sessions/<subject>/<session_id>/ with rich artifacts but doesn't
write a meta.json — session identity is the dir name itself.

The live `adapters.sessions.session_dir(session_id)` is still used by
6 callers (point of-truth path-helper).
"""
from pathlib import Path

from pipeline.types import SessionMeta

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
SESSIONS_DIR = DATA_ROOT / "sessions"


def _session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def register_session(meta: SessionMeta) -> Path:
    """Create the session dir and write meta.json. Returns the dir path."""
    sd = _session_dir(meta.session_id)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "meta.json").write_text(meta.model_dump_json(indent=2))
    return sd


def load_session(session_id: str) -> SessionMeta:
    return SessionMeta.model_validate_json(
        (_session_dir(session_id) / "meta.json").read_text()
    )


def list_sessions() -> list[SessionMeta]:
    if not SESSIONS_DIR.exists():
        return []
    out = []
    for sd in sorted(SESSIONS_DIR.iterdir()):
        meta_path = sd / "meta.json"
        if sd.is_dir() and meta_path.exists():
            out.append(SessionMeta.model_validate_json(meta_path.read_text()))
    return out
