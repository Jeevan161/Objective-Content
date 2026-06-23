"""LO pipeline · Node 6 — author_outcomes (one call per topic, assignment-driven)."""
from __future__ import annotations

from collections import Counter

from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.config import SKILL_TYPES, TEMP_AUTHOR, VERBS, allowed_verbs_for
from app.mcq_pipeline.utils.concept_graph import ground_quote, slugify
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.nodes._common import _bind_rag, _prog


# ── Node 6 · author_outcomes (A · 1 call per topic) ───────────────────────── #
_DEFAULT_VERB = {"remember": "identify", "understand": "explain",
                 "apply": "apply", "scenario": "apply"}


def _tier_of(item: dict) -> str:
    """Map an LLM item's declared bloom_level to one of the 4 canonical tiers."""
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
    """Coerce one LLM item to its planned ASSIGNMENT — the (concept_id, Bloom tier) the Planner
    fixed. The verb is clamped into that tier's controlled vocabulary; evidence is re-grounded."""
    tier = assignment["tier"]
    cid = assignment["concept_id"]
    cur = next((c for c in inv if c["concept_id"] == cid), None)
    cname = cur["canonical_name"] if cur else cid[2:].replace("_", " ")
    verb = str(item.get("learner_action", "")).lower().strip()
    if verb not in VERBS[tier]:
        verb = _DEFAULT_VERB[tier]
    title = (item.get("title") or f"{verb.title()} {cname}").strip()   # concept-specific, not topic-wide
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


def _synthesize_outcome(topic: dict, assignment: dict, inv: list) -> dict:
    """A GROUNDED placeholder LO at the planned tier, used ONLY when the author under-produces.
    Not a throwaway 'Identify X' — it targets the assigned concept+tier and is then scored by
    the Judge and improved by the regenerate-repair loop."""
    cid = assignment["concept_id"]
    cur = next((c for c in inv if c["concept_id"] == cid), None)
    name = cur["canonical_name"] if cur else cid[2:].replace("_", " ")
    verb = _DEFAULT_VERB[assignment["tier"]]
    item = {"learner_action": verb, "title": f"{verb.title()} {name}",
            "description": f"{verb.title()} {name}.",
            "justification": "Authored to the planned tier assignment."}
    return _coerce_outcome(item, topic, assignment, inv)


def _match_items(items: list, topic: dict, assignments: list, inv: list, out: list) -> None:
    """Staged match of LLM `items` onto the still-empty slots of `out` (aligned 1:1 to
    `assignments`): pass 1 — exact (concept_id, tier); pass 2 — same concept_id, any tier. Each
    item is consumed at most once. Staging (all of pass 1 before any of pass 2) ensures a weaker
    match never steals an item an exact match needs, and NO arbitrary leftover is force-bound to
    an unrelated concept — unfilled slots stay None for the caller to gap-fill (LLM) or, as a last
    resort, synthesize. Mutates `out` in place."""
    pool = list(items)

    def _take(pred):
        for i, a in enumerate(assignments):
            if out[i] is not None:
                continue
            m = next((it for it in pool if pred(it, a)), None)
            if m is not None:
                pool.remove(m)
                out[i] = _coerce_outcome(m, topic, a, inv)

    _take(lambda it, a: it.get("concept_id") == a["concept_id"] and _tier_of(it) == a["tier"])
    _take(lambda it, a: it.get("concept_id") == a["concept_id"])


# DB-overridable (sentinel placeholders substituted in code — NOT str.format, because the
# prompt contains literal JSON braces).
_AUTHOR_SYS = register("lo.author_sys", """\
You author measurable LEARNING OUTCOMES for one topic, grounded ONLY in the given concepts/evidence. You are given a numbered list of ASSIGNMENTS — each fixes the concept_id and the Bloom TIER to author. Produce EXACTLY ONE outcome per assignment, in the same order, ECHOING that assignment's concept_id and bloom_level.
Return ONLY a JSON list. Each item: {"concept_id","bloom_level","title","learner_action","skill_type","description","syntax","source_evidence":{"quote","section"},"justification"}.

The four Bloom tiers and their REQUIRED learner_action verbs:
- "remember"   -> one of: <REMEMBER_VERBS>   (recall a stated fact/term)
- "understand" -> one of: <UNDERSTAND_VERBS> (explain the reasoning/relationship the material gives)
- "apply"      -> one of: <APPLY_VERBS>      (carry out a taught procedure on a concrete input)
- "scenario"   -> one of: <SCENARIO_VERBS>   (APPLY in a NOVEL, self-contained SITUATION you describe fully in the title/description — transfer/judgement, NOT recall)
learner_action MUST be from the assigned tier's list. Do NOT change the assigned tier or concept_id.

SELF-CONTAINED & TRANSFERABLE — each "title"/"description" MUST state the GENERAL concept or skill so it stands on its own, independent of this reading. NEVER reference a source-local entity: a scenario label ("Project A"/"Project B"), a sample variable/file/function name, a character, or a one-off value. The reading's example is supporting EVIDENCE, not the thing assessed — GENERALIZE it. Technologies/tools/commands genuinely taught by name MAY be named.

CRISP & DISTINCT — each outcome targets ONE concept with a SINGLE unambiguous correct answer. No two outcomes may be interchangeable (the same idea under a different verb).

GROUNDED IN TAUGHT DEPTH — stay within what the material actually teaches; never require a detail, value, comparison, or sub-topic the material does not cover. Each concept is annotated below with [taught depth=...; allowed verbs: ...]; never exceed that ceiling. NEVER reference an external resource/tool/framework/dataset/terminology absent from THIS material.

'syntax' is a code/command reference COPIED VERBATIM from the section (null if none — never invent or guess). 'quote' is copied verbatim from the evidence. No commentary.""")


def _author_items(sys: str, topic: dict, concept_lines: str, assignments: list, name_of: dict) -> list:
    """One author LLM call for `assignments`; returns the raw JSON item list ([] on miss/garbage).
    Reused for the initial pass and the gap-fill retry — the retry simply passes the subset of
    assignments that the first pass failed to produce."""
    assignment_lines = "\n".join(
        f'{i + 1}. concept_id={a["concept_id"]} ({name_of.get(a["concept_id"], a["concept_id"])}) '
        f'— Bloom tier: {a["tier"]}' for i, a in enumerate(assignments))
    usr = (f"TOPIC: {topic['title']} ({topic['topic_id']}, section_text_len={len(topic['text'])})\n\n"
           f"CONCEPTS:\n{concept_lines}\n\n"
           f"AUTHOR EXACTLY {len(assignments)} OUTCOMES — ONE PER ASSIGNMENT, IN ORDER:\n{assignment_lines}")
    data = parse_json(chat([{"role": "system", "content": sys},
                            {"role": "user", "content": usr}], temperature=TEMP_AUTHOR)) or []
    return data if isinstance(data, list) else []


def _author_topic(state: dict, topic: dict, plan_row: dict) -> list:
    # Author ONLY against in-scope (substantively explained) concepts — a named-only or
    # external concept was dropped by profile_coverage and must not seed an outcome.
    topic_concepts = [c for c in state["concept_inventory"] if c["topic_id"] == topic["topic_id"]]
    inv = [c for c in topic_concepts if c.get("in_scope", True)] or topic_concepts \
        or [c for c in state["concept_inventory"] if c.get("in_scope", True)] \
        or state["concept_inventory"]
    assignments = plan_row.get("assignments", [])
    if not assignments:
        return []
    name_of = {c["concept_id"]: c["canonical_name"] for c in state["concept_inventory"]}
    concept_lines = "\n".join(
        f'{c["concept_id"]}: {c["canonical_name"]} '
        f'[taught depth={c.get("depth_category", "moderate")}; allowed verbs: '
        f'{sorted(allowed_verbs_for(c.get("depth_category", "moderate"), bool(c.get("procedural"))))}] '
        f'— {(c.get("description") or "").strip()[:240]} '
        f'(evidence: "{c["evidence"]["quote"][:120]}")' for c in inv)
    sys = (get_prompt("lo.author_sys", _AUTHOR_SYS)
           .replace("<REMEMBER_VERBS>", str(sorted(VERBS["remember"])))
           .replace("<UNDERSTAND_VERBS>", str(sorted(VERBS["understand"])))
           .replace("<APPLY_VERBS>", str(sorted(VERBS["apply"])))
           .replace("<SCENARIO_VERBS>", str(sorted(VERBS["scenario"]))))

    out: list = [None] * len(assignments)
    # Pass 1 — author every assignment, then staged-match the items onto their planned slots.
    _match_items(_author_items(sys, topic, concept_lines, assignments, name_of),
                 topic, assignments, inv, out)
    # Pass 2 — LLM GAP-FILL: re-author ONLY the assignments pass 1 left unfilled (real outcomes,
    # not placeholders). Still clamped by _coerce_outcome, so tier/verb/grounding are guaranteed.
    # One extra call, fired only when pass 1 under-produced (rare at TEMP_AUTHOR).
    missing = [a for i, a in enumerate(assignments) if out[i] is None]
    if missing:
        _match_items(_author_items(sys, topic, concept_lines, missing, name_of),
                     topic, assignments, inv, out)
    # Last resort — deterministic grounded synthesis for anything the LLM never produced.
    for i, a in enumerate(assignments):
        if out[i] is None:
            out[i] = _synthesize_outcome(topic, a, inv)
    return out


def author_outcomes(state, config) -> dict:
    _bind_rag(config)
    prog = _prog(config)
    plan = state["allocation_plan"]["by_topic"]
    work = [t for t in state["sections"] if plan[t["topic_id"]]["slots"] > 0]
    on_done = prog.counter("author_outcomes", len(work))

    def _one(topic):
        rows = _author_topic(state, topic, plan[topic["topic_id"]])
        on_done()
        return rows

    results = pmap(_one, work)
    outcomes = [o for rows in results for o in rows]
    seen = Counter()
    for o in outcomes:
        seen[o["id"]] += 1
        if seen[o["id"]] > 1:
            o["id"] = f'{o["id"]}_{seen[o["id"]]}'
    prog.done("author_outcomes", detail=f"{len(outcomes)} outcomes")
    return {"outcomes": outcomes}


