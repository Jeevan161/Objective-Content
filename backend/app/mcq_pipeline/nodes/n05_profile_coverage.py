"""LO pipeline · Node 4.5 — profile_coverage (taught depth + scope-closure)."""
from __future__ import annotations

from collections import Counter

from app.mcq_pipeline.utils import rag_api
from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.config import DEPTH_CATEGORIES, DROP_NAMED_ONLY
from app.mcq_pipeline.utils.concept_graph import concept_depth
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.nodes._common import _bind_rag, _prog


# ── Node 4.5 · profile_coverage (A · breadth x depth, pre-authoring) ──────── #
# Builds the coverage profile BEFORE authoring so LOs are bounded by what the material
# actually teaches: per concept it scores taught DEPTH (LLM, deterministic fallback) and
# confirms the concept is genuinely taught in-course (RAG scope-closure drops external /
# named-in-passing terms). depth_category then sets each concept's allowed-verb ceiling
# (lo_config.allowed_verbs_for), which plan_allocation and author_outcomes obey — so depth
# is established ONCE here instead of being reverse-engineered by V12/coverage_gate repairs.
_DEPTH_PROFILE_SYS = register("lo.depth_profile", (
    "You score how DEEPLY one concept is taught in the given section, for assessment "
    "planning. Read the concept and its section, then classify the taught DEPTH as exactly "
    "one of:\n"
    "- mention: only named or stated, in passing or a single sentence, with no real "
    "explanation (supports only recall: identify/list/define).\n"
    "- moderate: explained with some reasoning or detail across a few sentences (supports "
    "understand: explain/describe), but not thoroughly developed.\n"
    "- deep: thoroughly developed — explanation PLUS examples, steps, or an explicit "
    "contrast/comparison (supports compare/differentiate and, if procedural, apply).\n"
    "Judge ONLY by what THIS material actually teaches; never credit outside knowledge.\n"
    'Return ONLY JSON: {"depth": "mention|moderate|deep", "why": "<one line>"}.'
))


def _profile_one(concept: dict, section_text: str) -> dict:
    name = concept.get("canonical_name", "")
    ev = (concept.get("evidence") or {}).get("quote", "")
    usr = (f'CONCEPT: {name}\n\nEVIDENCE (where it was drawn from):\n"{ev}"\n\n'
           f"SECTION TEXT:\n{(section_text or '')[:8000]}")
    data = {}
    try:
        data = parse_json(chat([{"role": "system", "content": get_prompt("lo.depth_profile", _DEPTH_PROFILE_SYS)},
                                {"role": "user", "content": usr}], temperature=0)) or {}
    except Exception:  # noqa: BLE001 — LLM down: fall back to the deterministic depth heuristic
        data = {}
    depth = str(data.get("depth", "")).strip().lower()
    if depth not in DEPTH_CATEGORIES:
        d = concept_depth(name, section_text)
        depth = "mention" if d <= 1 else ("moderate" if d <= 3 else "deep")
    return {"depth_category": depth, "depth_why": str(data.get("why", ""))[:200]}


def profile_coverage(state, config) -> dict:
    """Pre-authoring breadth+depth profile. Per concept (concurrent): LLM depth score +
    best-effort RAG scope-closure. Attaches depth_category, drops out-of-scope externals
    (in_scope=False), and emits a coverage_profile manifest."""
    _bind_rag(config)
    prog = _prog(config)
    inv = [dict(c) for c in state["concept_inventory"]]
    sec_text = {s["topic_id"]: s.get("text", "") for s in state.get("sections", [])}
    on_done = prog.counter("profile_coverage", len(inv))

    def _one(c):
        prof = _profile_one(c, sec_text.get(c.get("topic_id"), ""))
        external = False
        try:  # scope-closure: is this concept actually taught in the course scope?
            verdict = (rag_api.check_concept(c["canonical_name"]).get("verdict") or "").split("\n", 1)[0].upper()
            external = "NOT EXPLAINED" in verdict
        except Exception:  # noqa: BLE001 — RAG down: never drop a concept on a failed check
            external = False
        on_done()
        return (c["concept_id"], prof, external)

    by_id = {cid: (prof, ext) for cid, prof, ext in pmap(_one, inv)}
    named_only = []
    for c in inv:
        prof, ext = by_id.get(c["concept_id"], ({"depth_category": "moderate", "depth_why": ""}, False))
        c["depth_category"] = prof["depth_category"]
        c["depth_why"] = prof["depth_why"]
        # Quality model: taught_depth + explained describe HOW the session teaches the
        # concept. "mention" = named/stated only, no real explanation → not explained.
        c["taught_depth"] = prof["depth_category"]
        c["explained"] = prof["depth_category"] != "mention"
        if ext:
            c["in_scope"] = False
            c["out_of_scope_reason"] = "named in passing; not taught in course scope (external)"
        elif DROP_NAMED_ONLY and not c["explained"] and c["in_scope"]:
            # Identify-but-don't-explain: a bare mention is not assessable, so it must NOT
            # seed an outcome. Drop it from scope rather than mint a recall LO on a name.
            c["in_scope"] = False
            c["out_of_scope_reason"] = "named in passing; not substantively explained in this session"
            named_only.append(c["concept_id"])

    cats = Counter(c["depth_category"] for c in inv if c["in_scope"])
    external = [c["concept_id"] for c in inv if not c["in_scope"] and c["concept_id"] not in named_only]
    manifest = {"by_depth": dict(cats), "in_scope": sum(1 for c in inv if c["in_scope"]),
                "dropped_external": external, "dropped_named_only": named_only}
    prog.done("profile_coverage",
              detail=f"{manifest['in_scope']} in-scope {dict(cats)}; "
                     f"dropped {len(external)} external, {len(named_only)} named-only")
    return {"concept_inventory": inv, "coverage_profile": manifest,
            "log": [{"node": "profile_coverage", **manifest}]}


