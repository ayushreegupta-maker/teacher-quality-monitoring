"""
Archived `detect_boundaries` — 2026-06-10.

The original public entrypoint for the boundary-detection stage. The new
`pipeline.session_video.stage2_detect_boundaries` inlined this logic so
it could pass `fps=BOUNDARY_FPS` through to `call_gemini_video` — long
videos at the default fps would otherwise blow Gemini's 1M input-token
ceiling. Zero live callers remain.

Internal helpers (`_parse_hms`, `_format_hms`, `_subtract_clocks`,
`_derive_elapsed_from_walls`) STAYED in the live `pipeline/boundaries.py`
because `session_video.stage2_detect_boundaries` imports them directly.
"""
import logging

from adapters.llm import LLMAdapter, parse_json_lenient, prompt_hash
from adapters.sessions import session_dir
from pipeline.boundaries import _derive_elapsed_from_walls
from pipeline.render import _jinja_env, load_prompt, split_system_user
from pipeline.types import BoundaryDetection, SessionMeta

log = logging.getLogger(__name__)


def detect_boundaries(session: SessionMeta, llm: LLMAdapter) -> BoundaryDetection:
    """One Gemini call on the WHOLE video to identify when the class began
    and ended based on child presence in the play area."""
    template_text = load_prompt("boundaries")
    rendered = _jinja_env().from_string(template_text).render(
        session=session.model_dump(mode="json"),
    )
    system, user = split_system_user(rendered)
    full_prompt = f"{system}\n\n{user}"

    log.info(
        f"[{session.session_id}] boundary detection: uploading + analysing whole video"
    )
    video_file = llm.upload_video(session.video_path)
    raw = llm.call_gemini_video(prompt=full_prompt, video_file=video_file)

    p_hash = prompt_hash(template_text)
    sd = session_dir(session.session_id)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / f"boundaries_raw_{p_hash}.txt").write_text(raw)

    parsed = parse_json_lenient(raw)
    result = BoundaryDetection.model_validate(parsed)
    result.session_id = session.session_id
    result.source_model = llm.vision_model
    result.prompt_hash = p_hash

    _derive_elapsed_from_walls(result, session.session_id)

    (sd / f"boundaries_{p_hash}.json").write_text(result.model_dump_json(indent=2))
    log.info(
        f"[{session.session_id}] boundaries: "
        f"first_child={result.first_child_visible_at} "
        f"last_child={result.last_child_visible_at} "
        f"confidence={result.confidence}"
    )
    return result
