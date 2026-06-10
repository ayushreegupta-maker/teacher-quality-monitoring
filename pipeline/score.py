import asyncio
import logging
import time

from adapters.llm import LLMAdapter, prompt_hash
from adapters.sessions import session_dir
from pipeline.render import load_prompt, render_score_prompt, split_system_user
from pipeline.types import (
    DimensionScore,
    Rubric,
    SessionMeta,
    SessionScores,
    Transcript,
    VisualObservations,
)

log = logging.getLogger(__name__)


async def score_one_dimension(
    dim_id: str,
    rubric: Rubric,
    session: SessionMeta,
    transcript: Transcript,
    observations: VisualObservations,
    llm: LLMAdapter,
) -> DimensionScore:
    dimension = rubric.get_dimension(dim_id)
    rendered = render_score_prompt(dimension, rubric, session, transcript, observations)
    system, user = split_system_user(rendered)

    log.info(f"[{session.session_id}] scoring {dim_id}")
    score: DimensionScore = await asyncio.to_thread(
        llm.call_claude_json,
        system=system,
        user=user,
        schema=DimensionScore,
    )
    score.prompt_hash = prompt_hash(load_prompt("score_dimension"))
    return score


async def score_session(
    session: SessionMeta,
    transcript: Transcript,
    observations: VisualObservations,
    rubric: Rubric,
    llm: LLMAdapter,
) -> SessionScores:
    """Fan out per-dimension scoring in parallel, merge results, persist."""
    start = time.time()
    dim_ids = [d.id for d in rubric.all_dimensions()]
    log.info(f"[{session.session_id}] scoring {len(dim_ids)} dimensions in parallel")

    results = await asyncio.gather(
        *[
            score_one_dimension(d_id, rubric, session, transcript, observations, llm)
            for d_id in dim_ids
        ],
        return_exceptions=True,
    )

    scores: dict[str, DimensionScore] = {}
    for d_id, result in zip(dim_ids, results):
        if isinstance(result, Exception):
            log.error(f"[{session.session_id}] {d_id} failed: {result!r}")
            continue
        scores[d_id] = result

    # Average across any numeric score (int or float); exclude "insufficient_evidence".
    # Booleans are excluded explicitly — isinstance(True, int) is True in Python.
    nums = [
        s.score for s in scores.values()
        if isinstance(s.score, (int, float)) and not isinstance(s.score, bool)
    ]
    overall = sum(nums) / len(nums) if nums else None

    session_scores = SessionScores(
        session_id=session.session_id,
        rubric_version=rubric.version,
        scores=scores,
        overall=overall,
        duration_seconds=time.time() - start,
    )

    p_hash = next((s.prompt_hash for s in scores.values() if s.prompt_hash), "na")
    sd = session_dir(session.session_id)
    sd.mkdir(parents=True, exist_ok=True)
    out_path = sd / f"scores_{rubric.version}_{p_hash}.json"
    out_path.write_text(session_scores.model_dump_json(indent=2))
    log.info(f"[{session.session_id}] scores written to {out_path}")

    return session_scores
