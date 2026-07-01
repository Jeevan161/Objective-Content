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

from app.mcq_pipeline.utils.progress import (
    STAGE_DEFS, CQ_BASE_STAGE_DEFS, CQ_VARIANT_STAGE_DEFS,
)

# The Classroom Quiz pipeline REUSES the full MCQ LO+question pipeline, wrapped by two
# CQ-only stages: reading_material (m00) up front, generate_variants (m10) at the end.
CQ_STAGE_DEFS = CQ_BASE_STAGE_DEFS + CQ_VARIANT_STAGE_DEFS
_STAGE_DEFS_BY_FAMILY = {"mcq": STAGE_DEFS, "cq": CQ_STAGE_DEFS}

# `lo.*` keys don't share a stage-derivable prefix — map them explicitly to the
# deterministic 10-node LO pipeline's agent stages.
_LO_STAGE = {
    "lo.segment_sys": "parse_structure",
    "lo.segment_critique_sys": "parse_structure",
    "lo.generate_sys": "author_outcomes",
    "lo.consolidate_sys": "consolidate_concepts",
    "lo.consolidate_critic_sys": "consolidate_concepts",
    "lo.graph_sys": "graph_outcomes",
    "lo.plan_sys": "select_outcomes",
    "lo.plan_critic_sys": "select_outcomes",
    "lo.coverage_plan": "resolve_prerequisites",
    "lo.coverage_judge": "resolve_prerequisites",
    "lo.rubric": "review_and_validate",
    "lo.repair_sys": "repair",
    "lo.sequence_sys": "sequence_outcomes",
}

# Short explanatory notes shown under a stage in the UI. The LO pipeline's
# non-agent nodes are pure deterministic code — the rules below them are read-only
# REFERENCE documentation (no editable prompt), so say that explicitly.
_DETERMINISTIC_NOTE = ("Deterministic stage — driven by code, not an LLM. The rules "
                       "below are read-only reference documentation.")
_STAGE_NOTES = {
    "parse_structure": ("Hybrid — an LLM (lo.segment_sys) proposes the topic boundaries; when "
                        "the split looks off, a reviewer (lo.segment_critique_sys) rechecks and "
                        "corrects it; a deterministic line-split then enforces the result "
                        "losslessly and falls back to the heading rules below if the LLM is "
                        "unavailable."),
    "consolidate_concepts": ("Agentic — an LLM semantically merges sub-concepts (replacing the old "
                             "Jaccard heuristic) and judges taught depth; a gated critic rechecks "
                             "the merge. Deterministic code keys the ids and applies the scope rule."),
    "graph_outcomes": ("LLM K-sample majority voting builds the concept dependency DAG; weights + "
                       "dag_depth + procedurality are derived deterministically from it."),
    "select_outcomes": ("Agentic — the LLM proposes which outcomes to keep toward the budget (a "
                        "gated critic rechecks coverage); deterministic code enforces the "
                        "invariants (feasibility clamp, budget ceiling, coverage floor, allocation "
                        "plan the repair loop reconciles against)."),
    "validate": _DETERMINISTIC_NOTE,
    "finalize": _DETERMINISTIC_NOTE,
    "lo_to_legacy": _DETERMINISTIC_NOTE,
    "review_questions": "Also re-applies the generation guideline blocks (gen.*) to "
                        "validate each question against the same rules it was written by.",
    # Classroom-Quiz-only stages.
    "reading_material": ("LLM (cq.reading_material) turns the quiz's slide span into standalone "
                         "reading material — the source text the LO/question pipeline then runs on."),
    "generate_variants": ("For each APPROVED base question, an LLM derives its assessment objective "
                          "(cq.variants.objective), steers the m08 generator to the same objective via "
                          "a new angle/format (cq.variants.directive), and a fidelity judge "
                          "(cq.variants.fidelity) admits only faithful, valid, distinct variants into "
                          "the random-pick pool."),
}


def stage_for_key(key: str, family: str = "mcq") -> str | None:
    """The pipeline stage a prompt key drives within `family` ('mcq' | 'cq'), or None if it maps
    to no stage in that family. The two CQ-only wrapper stages exist only in the 'cq' family."""
    if family == "cq":
        if key == "cq.reading_material":
            return "reading_material"
        if key.startswith("cq.variants."):
            return "generate_variants"
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


def build_catalog(keys: list[str], family: str = "mcq") -> tuple[list[dict], list[str]]:
    """Group `keys` under the ordered pipeline stages for `family` ('mcq' | 'cq'), preserving the
    given order within each stage. Returns (stages, unassigned) where each stage carries its
    label / parallel_group / note / prompt_keys, and `unassigned` lists any keys that map to no
    stage in this family (so nothing is ever silently hidden — e.g. cq.* keys are unassigned in
    the mcq family, and mcq-only keys never appear as CQ stages)."""
    stage_defs = _STAGE_DEFS_BY_FAMILY.get(family, STAGE_DEFS)
    by_stage: dict[str, list[str]] = {d["key"]: [] for d in stage_defs}
    unassigned: list[str] = []
    for key in keys:
        stage = stage_for_key(key, family)
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
        for d in stage_defs
    ]
    return stages, unassigned
