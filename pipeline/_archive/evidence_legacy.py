"""
Archived `enrich_bundle_with_shape_a` — 2026-06-10.

Designed for the future Shape A → enrich → Shape B reasoning flow:
after a Shape A rubric run produces phases / explanations /
disturbances, this attaches them to the cached EvidenceBundle so a
subsequent Shape B run reads them as part of its evidence.

Imported (but never called) in scripts/run_rubric.py from when the
step-9 verification was prototyping the enrichment path. Archived
because the actual wire-up isn't done — when we wire Shape A → enrich
→ Shape B, restore via:
    from pipeline._archive.evidence_legacy import enrich_bundle_with_shape_a
"""
from pathlib import Path
from typing import Optional

from pipeline.evidence import (
    _DEFAULT_CACHE_ROOT,
    _parse_fps_token,
    cache_dir_for,
    log,
)
from pipeline.types import EvidenceBundle


def enrich_bundle_with_shape_a(
    bundle: EvidenceBundle,
    *,
    phases: Optional[list[dict]] = None,
    explanations: Optional[list[dict]] = None,
    disturbances: Optional[list[dict]] = None,
    cache_root: Path = _DEFAULT_CACHE_ROOT,
) -> EvidenceBundle:
    """Attach Shape-A-derived enrichment to a cached EvidenceBundle and
    re-persist. Existing values overwritten by any non-None argument."""
    if phases is not None:
        bundle.phases = phases
    if explanations is not None:
        bundle.explanations = explanations
    if disturbances is not None:
        bundle.disturbances = disturbances

    cdir = cache_dir_for(
        session_id=bundle.session_id,
        subject=bundle.subject,
        vision_model=bundle.vision_model,
        fps=None if bundle.vision_fps == "fps-default"
            else _parse_fps_token(bundle.vision_fps),
        chunking=bundle.chunking,
        cache_root=cache_root,
    )
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "evidence_bundle.json").write_text(bundle.model_dump_json(indent=2))
    log.info(
        f"[{bundle.session_id}] enriched bundle with "
        f"phases={'+' if bundle.phases else '-'} "
        f"explanations={'+' if bundle.explanations else '-'} "
        f"disturbances={'+' if bundle.disturbances else '-'}"
    )
    return bundle
