"""LO pipeline (LO-first) · Node 2 — generate_outcomes.

Step 1 of the LO-first flow: "Generate ALL learning outcomes possible".

Instead of extracting concepts first and authoring one outcome per planned (concept, tier)
slot, this node goes straight from the topic sections to an EXHAUSTIVE set of candidate learning
outcomes. For every section it asks the author LLM (`lo.generate_sys`) to enumerate every distinct,
assessable outcome the material supports — recall/understand for everything taught, and apply/
scenario wherever the section actually demonstrates a procedure. Each candidate is emitted at
FINAL wording (verb-clamped to the controlled vocabulary, self-contained, grounded with a verbatim
evidence quote) so no separate polish pass is needed.

The candidates carry a raw concept NAME only; `map_concepts` (Node 3) canonicalizes those names
into a stable concept inventory and stamps each outcome with its `concept_id`. Selection toward the
budget happens later in `plan_outcomes` (Node 6) — here we deliberately over-generate.

Input:  state["sections"]  — topic sections from parse_structure.
Output: outcomes = [proto-outcome, ...]  (the full candidate set)  +  per-section logs.

This module also hosts the shared `_coerce_outcome` / `_tier_of` helpers (the repair node reuses
them to clamp a regenerated outcome back onto its assigned concept + tier).
"""
from __future__ import annotations

from collections import Counter

from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.config import SKILL_TYPES, TEMP_AUTHOR, VERBS
from app.mcq_pipeline.utils.concept_graph import ground_quote, slugify
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.nodes._common import _bind_rag, _prog


# ── tier / verb helpers (shared with repair) ──────────────────────────────── #
_DEFAULT_VERB = {"remember": "identify", "understand": "explain",
                 "apply": "apply", "scenario": "apply"}


def _tier_of(item: dict) -> str:
    """Map an LLM item's declared bloom_level to one of the 4 canonical tiers ('' if unknown)."""
    b = str(item.get("bloom_level") or item.get("tier") or "").lower()
    if b.startswith("scen"):
        return "scenario"
    if b.startswith("appl"):
        return "apply"
    if b.startswith("under"):
        return "understand"
    if b.startswith("rem"):
        return "remember"
    return ""


def _coerce_outcome(item: dict, topic: dict, assignment: dict, inv: list) -> dict:
    """Coerce one LLM item onto a FIXED (concept_id, Bloom tier) assignment. Used by the repair
    node to keep a regenerated outcome on its planned concept + tier (verb clamped into that tier's
    controlled vocabulary; evidence re-grounded). Kept here so repair has no import on the old
    author node."""
    tier = assignment["tier"]
    cid = assignment["concept_id"]
    cur = next((c for c in inv if c["concept_id"] == cid), None)
    cname = cur["canonical_name"] if cur else cid[2:].replace("_", " ")
    verb = str(item.get("learner_action", "")).lower().strip()
    if verb not in VERBS[tier]:
        verb = _DEFAULT_VERB[tier]
    title = (item.get("title") or f"{verb.title()} {cname}").strip()
    skill = item.get("skill_type")
    if skill not in SKILL_TYPES:
        skill = "practical_application" if tier in ("apply", "scenario") else "conceptual"
    quote = ground_quote(cname, topic["text"])
    return {"id": slugify(f"{verb}_{cid[2:]}"), "title": title, "topic_id": topic["topic_id"],
            "concept_id": cid, "bloom_level": tier, "scenario": tier == "scenario",
            "skill_type": skill, "learner_action": verb,
            "description": (item.get("description") or title).strip(),
            "syntax": (item.get("syntax") or None),
            "prerequisites": [], "prerequisite_scope": None, "target_questions": 1,
            "source_evidence": {"quote": quote, "section": topic["topic_id"]},
            "justification": (item.get("justification") or "Grounded in section evidence.").strip()}


# ── Node 2 · generate_outcomes (LO-first exhaustive enumeration) ──────────── #
_GENERATE_SYS = register("lo.generate_sys", """\
You author the COMPLETE set of measurable LEARNING OUTCOMES that ONE section of instructional reading material (any subject) can support, grounded ONLY in that section.

GOAL — be EXHAUSTIVE: enumerate every DISTINCT, assessable outcome the material justifies. Cover every teachable concept in the section. Do NOT pre-filter for a budget; a later stage selects. But every outcome MUST be fully supported by the section — never pad with outcomes the material does not teach.

Return ONLY a JSON list. Each item:
{"concept","bloom_level","title","learner_action","skill_type","description","syntax","quote","justification"}

- "concept": the short, TRANSFERABLE concept name this outcome assesses (a generalized idea/skill/rule — never an example-local label like "Project A" or a sample variable). The same concept may back several outcomes at different levels.
- "bloom_level": one of "remember" | "understand" | "apply" | "scenario", chosen to match what the section actually TEACHES about this concept:
    remember   → the section states a fact/term to recall.
    understand → the section explains reasoning/relationships.
    apply      → the section demonstrates a concrete PROCEDURE/method/worked example the learner could carry out.
    scenario   → the section demonstrates TRANSFER of a method to a novel situation.
  Only emit apply/scenario when the section actually shows the method — otherwise stay at understand/remember.
- "learner_action": a verb matching the tier:
    remember   → one of: <REMEMBER_VERBS>
    understand → one of: <UNDERSTAND_VERBS>
    apply      → one of: <APPLY_VERBS>
    scenario   → one of: <SCENARIO_VERBS>
- "skill_type": one of "conceptual" | "practical_application" | "diagnostic".
- "syntax": a code/command reference COPIED VERBATIM from the section (null if none — never invent or guess).
- "quote": a span copied VERBATIM from the section that supports this outcome.
- "justification": one line on what in the section grounds this outcome.

RULES:
- SELF-CONTAINED & TRANSFERABLE: each title/description states the GENERAL concept or skill and stands on its own, independent of this reading. NEVER reference a source-local entity (a scenario label, a sample variable/file/function name, a character, a one-off value). The reading's example is EVIDENCE, not the thing assessed — generalize it. Technologies/tools/commands genuinely taught by name MAY be named.
- CRISP & DISTINCT: each outcome targets ONE concept with ONE unambiguous correct answer. Two outcomes that test the SAME concept at the SAME level under different wording are duplicates — emit only one. Outcomes that test a genuine SUB-STEP, a different facet, or a different Bloom level of the same concept ARE distinct — keep them.
- GROUNDED IN TAUGHT DEPTH: stay within what the section teaches; never require a detail, value, comparison, or sub-topic the material does not cover, and never reference an external resource/tool/dataset absent from the section.
- Return ONLY valid JSON. No markdown, no commentary.""")


def _verb_subbed_sys() -> str:
    return (get_prompt("lo.generate_sys", _GENERATE_SYS)
            .replace("<REMEMBER_VERBS>", str(sorted(VERBS["remember"])))
            .replace("<UNDERSTAND_VERBS>", str(sorted(VERBS["understand"])))
            .replace("<APPLY_VERBS>", str(sorted(VERBS["apply"])))
            .replace("<SCENARIO_VERBS>", str(sorted(VERBS["scenario"]))))


def _proto_outcome(item: dict, topic: dict) -> dict | None:
    """Build a candidate (proto) outcome from one LLM item. Tier + verb are clamped to the
    controlled vocabulary; the evidence quote is grounded against the section. concept_id and the
    final id slug are assigned later by map_concepts. Returns None if the item names no concept."""
    cname = (item.get("concept") or "").strip()
    if not cname:
        return None
    tier = _tier_of(item) or "understand"
    verb = str(item.get("learner_action", "")).lower().strip()
    if verb not in VERBS[tier]:
        verb = _DEFAULT_VERB[tier]
    title = (item.get("title") or f"{verb.title()} {cname}").strip()
    skill = item.get("skill_type")
    if skill not in SKILL_TYPES:
        skill = "practical_application" if tier in ("apply", "scenario") else "conceptual"
    # Prefer the LLM's own verbatim quote when it actually grounds in the section; else recover one.
    llm_quote = (item.get("quote") or "").strip()
    quote = llm_quote if (llm_quote and llm_quote in topic["text"]) else ground_quote(cname, topic["text"])
    return {"_concept_name": cname, "title": title, "topic_id": topic["topic_id"],
            "bloom_level": tier, "scenario": tier == "scenario",
            "skill_type": skill, "learner_action": verb,
            "description": (item.get("description") or title).strip(),
            "syntax": (item.get("syntax") or None),
            "prerequisites": [], "prerequisite_scope": None, "target_questions": 1,
            "source_evidence": {"quote": quote, "section": topic["topic_id"]},
            "justification": (item.get("justification") or "Grounded in section evidence.").strip()}


def _generate_topic(sys: str, topic: dict) -> tuple[list, dict]:
    data = parse_json(chat([{"role": "system", "content": sys},
                            {"role": "user", "content":
                             f"SECTION: {topic['title']} ({topic['topic_id']})\n\n{topic['text'][:6000]}"}],
                           temperature=TEMP_AUTHOR)) or []
    items = data if isinstance(data, list) else []
    protos = [p for it in items if isinstance(it, dict) and (p := _proto_outcome(it, topic))]
    log = {"node": "generate_outcomes", "section": topic["topic_id"], "candidates": len(protos)}
    return protos, log


def generate_outcomes(state, config) -> dict:
    """Exhaustively enumerate candidate learning outcomes per section (one author call per section,
    concurrent). Over-generates on purpose — Node 6 selects toward the budget."""
    _bind_rag(config)
    prog = _prog(config)
    sections = state["sections"]
    on_done = prog.counter("generate_outcomes", len(sections))
    sys = _verb_subbed_sys()

    def _one(topic):
        protos, log = _generate_topic(sys, topic)
        on_done()
        return protos, log

    results = pmap(_one, sections)
    outcomes = [p for protos, _ in results for p in protos]
    logs = [log for _, log in results]
    # Give each candidate a provisional unique id (final concept-anchored id is set in map_concepts).
    seen = Counter()
    for i, o in enumerate(outcomes):
        base = slugify(f'{o["learner_action"]}_{o["_concept_name"]}') or f"lo_{i}"
        seen[base] += 1
        o["id"] = base if seen[base] == 1 else f"{base}_{seen[base]}"
    snapshot = {"candidates": len(outcomes),
                "per_section": [{"section": l["section"], "candidates": l["candidates"]} for l in logs],
                "sample": [{"title": o["title"], "concept": o["_concept_name"],
                            "bloom": o["bloom_level"], "verb": o["learner_action"]}
                           for o in outcomes[:15]]}
    prog.done("generate_outcomes", detail=f"{len(outcomes)} candidate outcomes", snapshot=snapshot)
    return {"outcomes": outcomes, "log": logs}
