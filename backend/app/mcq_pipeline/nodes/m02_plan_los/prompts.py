"""plan_los · prompt registry — the three LLM judgment phases.

* ``lo.generate_sys``    — PHASE 1 (per section): enumerate candidate learning outcomes, each
                           naming a broad concept + a fine sub-concept (unchanged from the old
                           generate_outcomes node, so its DB-tuned version is reused as-is).
* ``lo.consolidate_sys`` — PHASE 1b (one call): the SEMANTIC merge that replaces the old token-set
                           Jaccard heuristic — collapse near-duplicate sub-concepts, and for each
                           surviving concept judge taught DEPTH + whether it is genuinely taught
                           in-scope (folds in the old profile_depth judgment).
* ``lo.plan_sys``        — PHASE 3 (one call): given the profiled concepts + candidate outcomes +
                           the budget, PROPOSE the outcome set to keep (coverage-first). The gate
                           then enforces budget / coverage / feasibility deterministically.

All DB-backed: the literals are the code default + migration seed; an active mcq_prompts row
overrides at call time.
"""
from __future__ import annotations

from app.mcq_pipeline.config import VERBS
from app.mcq_pipeline.prompts.store import get_prompt, register

# ── PHASE 1 · per-section candidate author (same key/contract as the old generate node) ───── #
GENERATE_SYS = register("lo.generate_sys", """\
You author the COMPLETE set of measurable LEARNING OUTCOMES that ONE section of instructional reading material (any subject) can support, grounded ONLY in that section.

GOAL — be EXHAUSTIVE: enumerate every DISTINCT, assessable outcome the material justifies. Cover every teachable concept in the section. Do NOT pre-filter for a budget; a later stage selects. But every outcome MUST be fully supported by the section — never pad with outcomes the material does not teach.

Return ONLY a JSON list. Each item:
{"concept","sub_concept","bloom_level","title","learner_action","skill_type","description","syntax","quote","justification"}

Think in THREE LEVELS — broad to fine: TOPIC (this whole section) ⊃ CONCEPT ⊃ SUB-CONCEPT.
- "concept": a BROAD, TRANSFERABLE teachable unit taught within this topic — the umbrella idea/skill, roughly one per thing the section sets out to teach (e.g. "Applying Migrations", "List Comprehensions"). It is broad like the topic, just one level down. Never an example-local label like "Project A" or a sample variable.
- "sub_concept": the FINE unit that defines GRANULARITY — the single STEP, rule, command, property, distinction, or move WITHIN the concept that THIS outcome tests (e.g. concept "Applying Migrations" → sub_concepts "makemigrations_creates_migration_files", "migrate_applies_migrations", "rollback_to_a_previous_migration"). One outcome assesses ONE sub_concept. Two outcomes under the same `concept` that test genuinely different steps/sub-ideas MUST carry DIFFERENT sub_concepts. Be granular: decompose a broad concept into its distinct assessable sub_concepts (its steps) rather than emitting one umbrella outcome. The same (concept, sub_concept) may still back several outcomes at different Bloom levels.
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
- "quote": the COMPLETE sentence(s) — or a short paragraph — copied VERBATIM from the section that TEACHES this outcome. Give the whole teaching sentence as it appears in the material, NOT a truncated fragment or a few keywords.
- "justification": one line on what in the section grounds this outcome.

RULES:
- SELF-CONTAINED & TRANSFERABLE: each title/description states the GENERAL concept or skill and stands on its own, independent of this reading. NEVER reference a source-local entity (a scenario label, a sample variable/file/function name, a character, a one-off value). The reading's example is EVIDENCE, not the thing assessed — generalize it. Technologies/tools/commands genuinely taught by name MAY be named.
- CRISP & DISTINCT: each outcome targets ONE sub_concept with ONE unambiguous correct answer. Two outcomes with the SAME sub_concept at the SAME Bloom level under different wording are duplicates — emit only one. Outcomes on a genuine SUB-STEP, a different facet, or a different Bloom level ARE distinct — give each its own sub_concept and keep them.
- GROUNDED IN TAUGHT DEPTH: stay within what the section teaches; never require a detail, value, comparison, or sub-topic the material does not cover, and never reference an external resource/tool/dataset absent from the section.
- Return ONLY valid JSON. No markdown, no commentary.""")


# ── PHASE 1b · semantic consolidation + taught-depth (replaces Jaccard merge + profile_depth) ── #
CONSOLIDATE_SYS = register("lo.consolidate_sys", """\
You consolidate the SUB-CONCEPTS extracted from instructional reading material (any subject) into a clean, de-duplicated concept inventory, and judge how deeply each is TAUGHT.

You receive a JSON object:
{"reading": "<the full reading, for judging depth>",
 "sub_concepts": [{"name": "<sub-concept>", "parent": "<broad concept>", "evidence": "<verbatim teaching quote>"}, ...]}

Do TWO things:

1) MERGE near-duplicates. Group sub-concepts that name the SAME assessable idea (e.g. plural/singular, reordered words, an abbreviation vs its expansion, or two phrasings of one step). Keep genuinely distinct steps/facets SEPARATE — do not over-merge. Pick the clearest member name as the canonical name; carry the broad parent concept.

2) For each MERGED concept, judge — against the WHOLE reading, not just the quote — :
   - "depth": one of
       "named"    → only named/listed in passing, NO definition or explanation anywhere → not assessable.
       "mention"  → defined/stated in ~one sentence, no deeper reasoning → assessable at recall only.
       "moderate" → explained with some reasoning/description, maybe a simple example.
       "deep"     → fully taught: step-by-step, worked examples, comparison, or applied procedure.
     If unsure between two levels, choose the LOWER. Do NOT upgrade on importance or familiarity.
   - "in_scope": false ONLY if the concept is external to this reading (named in passing / assumed from elsewhere, not actually taught here); otherwise true.
   - "why": one short line.

Return ONLY a JSON list, one object per MERGED concept:
[{"canonical_name": "...", "parent_concept": "...", "members": ["<raw sub_concept name>", ...],
  "depth": "named|mention|moderate|deep", "in_scope": true, "why": "..."}]
Every input sub_concept name MUST appear in exactly one "members" list. Return ONLY valid JSON.""")


# ── PHASE 3 · budget-aware outcome selection (agent proposes; the gate enforces) ──────────── #
PLAN_SYS = register("lo.plan_sys", """\
You are a curriculum planner. From a pool of candidate LEARNING OUTCOMES, SELECT the set to keep for an assessment, working toward a question BUDGET.

You receive a JSON object:
{"budget": <int ceiling>,
 "concepts": [{"concept_id","name","parent","depth","procedural","weight"}...],
 "candidates": [{"id","concept_id","parent","bloom_level","title"}...]}

GOAL — pick the most valuable outcomes the material genuinely supports:
- COVERAGE FIRST: every distinct BROAD concept (parent) must be represented by at least one selected outcome.
- Then spend remaining budget on the most FOUNDATIONAL concepts (higher "weight") and on adding depth (a higher Bloom level on an already-covered concept) — prefer breadth of concepts over many outcomes on one concept.
- Do NOT exceed the budget. Do NOT select two outcomes that test the same concept at the same Bloom level (duplicates).
- Prefer outcomes whose Bloom level is plausible for the concept's depth (apply/scenario only on deep/procedural concepts) — but you do NOT need to enforce this exactly; a later step clamps tiers.

Return ONLY JSON: {"selected_ids": ["<id>", ...], "rationale": "<one or two lines>"}
Use ONLY ids present in "candidates". Return ONLY valid JSON.""")


# ── critic / reviser sub-agents (gated reflexion, m01-style) ──────────────── #
CONSOLIDATE_CRITIC_SYS = register("lo.consolidate_critic_sys", """\
You review a proposed CONCEPT CONSOLIDATION of sub-concepts from instructional reading and correct it only where it falls short.

You receive: {"sub_concepts": [{"name","parent","evidence"}...], "proposed_groups": [{"canonical_name","parent_concept","members","depth","in_scope","why"}...]}.

Check the grouping against these goals and FIX violations:
- NO OVER-MERGE: two genuinely different steps/rules/facets must NOT share a group. If a group fuses distinct assessable ideas, split it.
- NO UNDER-MERGE: two names for the SAME idea (plural/singular, reorder, abbreviation) must share one group.
- DEPTH is judged from the reading, conservatively (named|mention|moderate|deep; if unsure, lower).
- in_scope is false ONLY for concepts external to / merely named in this reading.
- Every input sub_concept name MUST appear in exactly one "members" list.

Return ONLY JSON: {"ok": <true if no change needed>, "groups": [<the corrected full group list, same schema as proposed_groups>]}.
If ok is true, echo proposed_groups unchanged.""")


PLAN_CRITIC_SYS = register("lo.plan_critic_sys", """\
You review a proposed SELECTION of learning outcomes against a budget and correct it only where it falls short.

You receive: {"budget": <int>, "concepts": [{"concept_id","name","parent","depth","procedural","weight"}...], "candidates": [{"id","concept_id","parent","bloom_level","title"}...], "proposed_selected_ids": ["<id>"...]}.

Check the selection and FIX violations:
- COVERAGE: every distinct BROAD concept (parent) present in "concepts" must have at least one selected outcome. Add the best candidate for any uncovered parent.
- BUDGET: do not exceed "budget"; if over, drop the least foundational (lowest weight) extras first.
- NO DUPLICATES: never two selected outcomes on the same concept_id at the same bloom_level.
- Prefer foundational concepts (higher weight) and breadth of concepts over many outcomes on one concept.

Return ONLY JSON: {"ok": <true if no change needed>, "selected_ids": ["<id>"...]}.
Use ONLY ids present in "candidates". If ok is true, echo proposed_selected_ids.""")


def generate_sys_verb_subbed() -> str:
    """The phase-1 author prompt with the controlled verb vocabularies substituted in."""
    return (get_prompt("lo.generate_sys", GENERATE_SYS)
            .replace("<REMEMBER_VERBS>", str(sorted(VERBS["remember"])))
            .replace("<UNDERSTAND_VERBS>", str(sorted(VERBS["understand"])))
            .replace("<APPLY_VERBS>", str(sorted(VERBS["apply"])))
            .replace("<SCENARIO_VERBS>", str(sorted(VERBS["scenario"]))))
