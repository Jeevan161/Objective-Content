"""LO pipeline · Node 4.5 — profile_coverage (taught depth + scope-closure)."""
from __future__ import annotations

from collections import Counter

from app.mcq_pipeline.utils import rag_api
from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.config import DEPTH_CATEGORIES, DROP_NAMED_ONLY
from app.mcq_pipeline.utils.concept_graph import concept_depth, loosen_text
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
    "You evaluate how COMPLETELY a concept is TAUGHT in the instructional reading.\n"
    "This score determines whether the concept is assessable and what level of learning "
    "outcomes can be generated later.\n\n"

    "Judge against the WHOLE reading provided, not just one section — a concept may be NAMED in one "
    "place but EXPLAINED in another; use the deepest treatment found anywhere in the reading.\n\n"

    "Return ONLY JSON:\n"
    '{"depth": "named|mention|moderate|deep", '
    '"evidence": "<the exact verbatim sentence(s) from the reading that TEACH this concept; '
    'empty string if the concept is only named with no explanation>", '
    '"why": "<one line>"}\n\n'

    "----------------------------\n"
    "PRIMARY OBJECTIVE\n"
    "----------------------------\n"
    "Decide how well the material TEACHES the concept — not how important it is, not how "
    "complex it is in general.\n\n"

    "----------------------------\n"
    "DEPTH DEFINITIONS (STRICT)\n"
    "----------------------------\n"

    "0. named\n"
    "- Concept is ONLY named, referenced, or listed in passing (e.g. 'tools like X, Y, Z')\n"
    "- NO definition and NO explanation ANYWHERE in the reading — not even one sentence\n"
    "- Cannot be assessed at all (there is nothing to ask a grounded question about)\n"
    "- 'evidence' MUST be an empty string\n\n"

    "1. mention\n"
    "- Concept is DEFINED or stated in about ONE sentence (a definition or a single explanatory "
    "statement) somewhere in the reading\n"
    "- No deeper reasoning, steps, or example-based teaching\n"
    "- Assessable at RECALL only (recognize/identify/define)\n"
    "- 'evidence' = the defining sentence, verbatim\n\n"

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
    "- A name with NO definition anywhere = named\n"
    "- A single sentence definition = mention\n"
    "- A definition + example = usually moderate (only if example is explained)\n"
    "- A procedural walkthrough or multi-step reasoning = deep\n\n"

    "----------------------------\n"
    "ASSESSMENT LINK RULE\n"
    "----------------------------\n"
    "Think in terms of what a learner could be tested on:\n"
    "- named → nothing (not assessable)\n"
    "- mention → recognition / recall only\n"
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


def _profile_one(concept: dict, section_text: str, source_text: str) -> dict:
    """Score taught DEPTH for one concept against the WHOLE reading (the section it was drawn from is
    the anchor; the rest of the reading is supporting context — a concept may be named in one section
    and explained in another). Also returns the verbatim span that TEACHES it (for evidence)."""
    name = concept.get("canonical_name", "")
    ev = (concept.get("evidence") or {}).get("quote", "")
    usr = (f'CONCEPT: {name}\n\nEVIDENCE (where it was first drawn from):\n"{ev}"\n\n'
           f"SECTION it was drawn from (anchor):\n{(section_text or '')[:6000]}\n\n"
           f"WHOLE READING (consider the concept may be explained elsewhere here):\n"
           f"{(source_text or '')[:12000]}")
    data = {}
    try:
        data = parse_json(chat([{"role": "system", "content": get_prompt("lo.depth_profile", _DEPTH_PROFILE_SYS)},
                                {"role": "user", "content": usr}], temperature=0)) or {}
    except Exception:  # noqa: BLE001 — LLM down: fall back to the deterministic depth heuristic
        data = {}
    depth = str(data.get("depth", "")).strip().lower()
    if depth not in DEPTH_CATEGORIES:
        d = concept_depth(name, source_text)   # count over the WHOLE reading now
        depth = "named" if d == 0 else ("mention" if d <= 1 else ("moderate" if d <= 3 else "deep"))
    # the verbatim teaching span (kept only if it actually resolves to the reading, so it can later
    # serve as grounded evidence). Empty for a bare 'named' reference.
    span = str(data.get("evidence", "") or "").strip()
    span_ok = bool(span) and loosen_text(span)[:60] in loosen_text(source_text)
    return {"depth_category": depth, "depth_why": str(data.get("why", ""))[:200],
            "depth_evidence": span if span_ok else ""}


def profile_depth(state, config) -> dict:
    """Pre-planning breadth+depth profile. Per concept (concurrent): LLM depth score +
    best-effort RAG scope-closure. Attaches depth_category, drops out-of-scope externals
    (in_scope=False), and emits a coverage_profile manifest. Sets each concept's feasibility
    ceiling (via depth + procedural), which plan_outcomes obeys to identify apply-level outcomes."""
    _bind_rag(config)
    prog = _prog(config)
    inv = [dict(c) for c in state["concept_inventory"]]
    sec_text = {s["topic_id"]: s.get("text", "") for s in state.get("sections", [])}
    source_text = state.get("source_text", "")
    on_done = prog.counter("profile_depth", len(inv))

    def _one(c):
        prof = _profile_one(c, sec_text.get(c.get("topic_id"), ""), source_text)
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
        prof, ext = by_id.get(c["concept_id"],
                              ({"depth_category": "moderate", "depth_why": "", "depth_evidence": ""}, False))
        c["depth_category"] = prof["depth_category"]
        c["depth_why"] = prof["depth_why"]
        # Quality model: taught_depth + explained describe HOW the session teaches the concept.
        # "explained" = taught with real reasoning (moderate/deep); a one-sentence "mention" is
        # assessable at recall but not "explained" in depth; "named" is a bare reference.
        c["taught_depth"] = prof["depth_category"]
        c["explained"] = prof["depth_category"] in ("moderate", "deep")
        # Attach the verbatim teaching span as grounded evidence (the "how it was explained" capture).
        span = prof.get("depth_evidence", "")
        if span:
            c["evidence"] = {"quote": span, "section": c.get("topic_id", "")}
            if span not in c.setdefault("evidence_quotes", []):
                c["evidence_quotes"].append(span)
        if ext:
            c["in_scope"] = False
            c["out_of_scope_reason"] = "named in passing; not taught in course scope (external)"
        elif DROP_NAMED_ONLY and c["depth_category"] == "named" and c["in_scope"]:
            # BARE reference: named/listed with no definition anywhere → nothing to ground a question
            # on, so drop it. (A one-sentence "mention" is KEPT and assessed at recall — we downgrade
            # the tier / reframe the LO downstream rather than drop it.)
            c["in_scope"] = False
            c["out_of_scope_reason"] = "named in passing; no definition or explanation in the reading"
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


