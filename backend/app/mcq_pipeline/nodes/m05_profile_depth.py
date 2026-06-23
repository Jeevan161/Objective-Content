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
    "You evaluate how COMPLETELY a concept is TAUGHT in the given instructional section.\n"
    "This score determines whether the concept is assessable and what level of learning "
    "outcomes can be generated later.\n\n"

    "Return ONLY JSON:\n"
    '{"depth": "mention|moderate|deep", "why": "<one line>"}\n\n'

    "----------------------------\n"
    "PRIMARY OBJECTIVE\n"
    "----------------------------\n"
    "Decide how well the material TEACHES the concept — not how important it is, not how "
    "complex it is in general.\n\n"

    "----------------------------\n"
    "DEPTH DEFINITIONS (STRICT)\n"
    "----------------------------\n"

    "1. mention\n"
    "- Concept is ONLY named, referenced, or listed\n"
    "- No explanation of how or why it works\n"
    "- No steps, reasoning, or example-based teaching\n"
    "- Cannot be assessed beyond recall (recognize/identify only)\n\n"

    "2. moderate\n"
    "- Concept is explained with SOME reasoning OR description\n"
    "- May include a simple example or partial breakdown\n"
    "- Learner can understand and describe it\n"
    "- But lacks full procedural detail or full coverage\n\n"

    "3. deep\n"
    "- Concept is fully taught with MULTIPLE of the following:\n"
    "  * step-by-step explanation\n"
    "  * worked examples\n"
    "  * comparison or contrast\n"
    "  * applied usage or procedure\n"
    "- Learner can APPLY it, not just explain it\n\n"

    "----------------------------\n"
    "CRITICAL ASSESSMENT RULE\n"
    "----------------------------\n"
    "Judge ONLY what the section teaches explicitly.\n"
    "Do NOT assume understanding from general knowledge.\n\n"

    "If explanation is missing in the text → it is NOT moderate or deep.\n\n"

    "----------------------------\n"
    "BOUNDARY RULE (VERY IMPORTANT)\n"
    "----------------------------\n"
    "- A single sentence definition = mention\n"
    "- A definition + example = usually moderate (only if example is explained)\n"
    "- A procedural walkthrough or multi-step reasoning = deep\n\n"

    "----------------------------\n"
    "ASSESSMENT LINK RULE\n"
    "----------------------------\n"
    "Think in terms of what a learner could be tested on:\n"
    "- mention → recognition only\n"
    "- moderate → explanation questions\n"
    "- deep → application / problem-solving questions\n\n"

    "----------------------------\n"
    "ANTI-OVERCLASSIFICATION RULE\n"
    "----------------------------\n"
    "- Do NOT upgrade depth based on importance or familiarity\n"
    "- Do NOT assume missing steps\n"
    "- If unsure between two levels → choose LOWER depth\n\n"

    "----------------------------\n"
    "OUTPUT CONSTRAINT\n"
    "----------------------------\n"
    "Return ONLY valid JSON. No explanation, no markdown.\n"
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


def profile_depth(state, config) -> dict:
    """Pre-planning breadth+depth profile. Per concept (concurrent): LLM depth score +
    best-effort RAG scope-closure. Attaches depth_category, drops out-of-scope externals
    (in_scope=False), and emits a coverage_profile manifest. Sets each concept's feasibility
    ceiling (via depth + procedural), which plan_outcomes obeys to identify apply-level outcomes."""
    _bind_rag(config)
    prog = _prog(config)
    inv = [dict(c) for c in state["concept_inventory"]]
    sec_text = {s["topic_id"]: s.get("text", "") for s in state.get("sections", [])}
    on_done = prog.counter("profile_depth", len(inv))

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
    name_of = {c["concept_id"]: c["canonical_name"] for c in inv}
    snapshot = {**manifest,
                "dropped_external": [name_of.get(cid, cid) for cid in external],
                "dropped_named_only": [name_of.get(cid, cid) for cid in named_only],
                "concepts": [{"name": c["canonical_name"], "depth": c["depth_category"],
                              "in_scope": c["in_scope"], "procedural": bool(c.get("procedural"))}
                             for c in inv]}
    prog.done("profile_depth",
              detail=f"{manifest['in_scope']} in-scope {dict(cats)}; "
                     f"dropped {len(external)} external, {len(named_only)} named-only",
              snapshot=snapshot)
    return {"concept_inventory": inv, "coverage_profile": manifest,
            "log": [{"node": "profile_depth", **manifest}]}


