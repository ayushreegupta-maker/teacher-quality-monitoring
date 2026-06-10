import logging

from adapters.llm import LLMAdapter, prompt_hash
from adapters.sessions import session_dir
from pipeline.render import _jinja_env, load_prompt, render_visual, split_system_user
from pipeline.types import ConsolidatedItems, SessionMeta, VisualObservations

log = logging.getLogger(__name__)


def consolidate_items(
    session: SessionMeta,
    observations: VisualObservations,
    llm: LLMAdapter,
) -> ConsolidatedItems:
    """Extract a consolidated inventory of items at the activity zone from
    merged visual observations. One Claude call per session. Persists the
    result as `items_inventory_<hash>.json` in the session directory.

    Anti-hallucination: if the vision pass produced zero observations, we
    SKIP the Claude call entirely and return an empty inventory. Otherwise
    Claude has been observed to invent plausible items from the activity
    context alone ("crayons or markers — implied by colouring activity"),
    which defeats the point of grounding items in visual evidence.
    """
    template_text = load_prompt("consolidate_items")
    p_hash = prompt_hash(template_text)
    sd = session_dir(session.session_id)
    sd.mkdir(parents=True, exist_ok=True)

    if not observations.observations:
        log.warning(
            f"[{session.session_id}] no visual observations — returning "
            f"empty items inventory (skipping Claude call to avoid "
            f"hallucination from activity_context alone)"
        )
        items = ConsolidatedItems(
            activity_zone_items=[],
            other_items_in_room=[],
            notes="No visual observations were available; items inventory skipped.",
            session_id=session.session_id,
            source_model=llm.scoring_model,
            prompt_hash=p_hash,
        )
        (sd / f"items_inventory_{p_hash}.json").write_text(
            items.model_dump_json(indent=2)
        )
        return items

    rendered = (
        _jinja_env()
        .from_string(template_text)
        .render(
            session=session.model_dump(mode="json"),
            observations_rendered=render_visual(observations),
        )
    )
    system, user = split_system_user(rendered)

    log.info(
        f"[{session.session_id}] consolidating items from "
        f"{len(observations.observations)} observations"
    )
    items = llm.call_claude_json(system=system, user=user, schema=ConsolidatedItems)
    items.session_id = session.session_id
    items.source_model = llm.scoring_model
    items.prompt_hash = p_hash

    (sd / f"items_inventory_{p_hash}.json").write_text(
        items.model_dump_json(indent=2)
    )
    log.info(
        f"[{session.session_id}] items: {len(items.activity_zone_items)} at zone, "
        f"{len(items.other_items_in_room)} elsewhere"
    )
    return items
