"""
app/mcq_pipeline/prompt_catalog.py
----------------------------------
Maps the pipeline's stages (`progress.STAGE_DEFS`) to the prompt keys that drive
each one, so the UI can show "the pipeline and its prompts" together.

New prompt keys are assigned to a stage by their key PREFIX, so this stays in
sync automatically as prompts are added — only the handful of `lo.*` keys (which
span three different stages) need an explicit mapping.
"""

from __future__ import annotations

from app.mcq_pipeline.utils.progress import STAGE_DEFS

# `lo.*` keys don't share a stage-derivable prefix — map them explicitly to the
# deterministic 10-node LO pipeline's agent stages.
_LO_STAGE = {
    "lo.segment_sys": "parse_structure",
    "lo.extract_sys": "extract_concepts",
    "lo.topic_desc_sys": "extract_concepts",
    "lo.depth_profile": "profile_coverage",
    "lo.graph_sys": "build_dependency_graph",
    "lo.author_sys": "author_outcomes",
    "lo.repair_sys": "repair",
}

# Short explanatory notes shown under a stage in the UI. The LO pipeline's
# non-agent nodes are pure deterministic code — the rules below them are read-only
# REFERENCE documentation (no editable prompt), so say that explicitly.
_DETERMINISTIC_NOTE = ("Deterministic stage — driven by code, not an LLM. The rules "
                       "below are read-only reference documentation.")
_STAGE_NOTES = {
    "parse_structure": ("Hybrid — an LLM (lo.segment_sys) proposes the topic boundaries; a "
                        "deterministic line-split enforces them losslessly and falls back to "
                        "the heading rules below if the LLM is unavailable."),
    "canonicalize_concepts": _DETERMINISTIC_NOTE,
    "plan_allocation": _DETERMINISTIC_NOTE,
    "resolve_prerequisites": _DETERMINISTIC_NOTE,
    "validate": _DETERMINISTIC_NOTE,
    "finalize": _DETERMINISTIC_NOTE,
    "lo_to_legacy": _DETERMINISTIC_NOTE,
    "review_questions": "Also re-applies the generation guideline blocks (gen.*) to "
                        "validate each question against the same rules it was written by.",
}


def stage_for_key(key: str) -> str | None:
    """The pipeline stage a prompt key drives, or None if it maps to no stage."""
    if key in _LO_STAGE:
        return _LO_STAGE[key]
    # Read-only reference docs for the deterministic stages: `lo.rules.<stage_key>`
    # maps straight onto its STAGE_DEFS key (e.g. lo.rules.validate -> validate).
    if key.startswith("lo.rules."):
        return key[len("lo.rules."):]
    if key.startswith("qtype."):
        return "recommend_question_types"
    if key.startswith("gen."):
        return "generate_questions"
    if key.startswith("review."):
        return "review_questions"
    return None


def build_catalog(keys: list[str]) -> tuple[list[dict], list[str]]:
    """Group `keys` under the ordered pipeline stages, preserving the given order
    within each stage. Returns (stages, unassigned) where each stage carries its
    label / parallel_group / note / prompt_keys, and `unassigned` lists any keys
    that map to no stage (so nothing is ever silently hidden)."""
    by_stage: dict[str, list[str]] = {d["key"]: [] for d in STAGE_DEFS}
    unassigned: list[str] = []
    for key in keys:
        stage = stage_for_key(key)
        if stage in by_stage:
            by_stage[stage].append(key)
        else:
            unassigned.append(key)

    stages = [
        {
            "key": d["key"],
            "label": d["label"],
            "parallel_group": d.get("parallel_group"),
            "note": _STAGE_NOTES.get(d["key"], ""),
            "prompt_keys": by_stage[d["key"]],
        }
        for d in STAGE_DEFS
    ]
    return stages, unassigned
