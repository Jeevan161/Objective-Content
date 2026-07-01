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

# ── PHASE 0 · derive the session's FOCUS / objective ("motive") ───────────── #
SESSION_FOCUS_SYS = register("lo.session_focus_sys", """\
You read a session's TITLE and its full reading material and state what the session SETS OUT TO TEACH — its motive — so later stages keep outcomes ON-FOCUS and don't drift onto incidental scaffolding.

Distinguish the session's CENTRAL teaching targets from INCIDENTAL content — material that appears only to support/demonstrate the focus and is NOT itself what the session teaches (e.g. HTML shown only to render a Django view's output; a sample dataset used only to demonstrate a query; setup/CLI steps shown only to reach the real topic).

Return ONLY valid JSON:
{"objective": "<one paragraph: what a learner should be able to do after this session — the central skills/concepts>",
 "central_concepts": ["<the concepts this session actually teaches>", ...],
 "incidental": ["<content present only as scaffolding/support, NOT a teaching target>", ...]}

Be faithful to the material — never invent a focus the reading doesn't support. If everything in the reading is genuinely taught, leave "incidental" empty.""")


# ── PHASE 1 · per-section candidate author (same key/contract as the old generate node) ───── #
GENERATE_SYS = register("lo.generate_sys", """\
You author the COMPLETE set of measurable LEARNING OUTCOMES that ONE section of instructional reading material (any subject) can support, grounded ONLY in that section.

SESSION FOCUS: the user message may begin with a "SESSION OBJECTIVE" (what this session sets out to teach) and a list of INCIDENTAL content. Author outcomes that SERVE that objective. Do NOT author outcomes for content that appears only as scaffolding/support for the focus (e.g. HTML shown only to demonstrate a Django view, a sample dataset shown only to run a query) — that content is evidence/context, not a teaching target. When no objective is given, author from the section as usual.

GOAL — be EXHAUSTIVE and produce BREADTH. For EACH concept, generate outcomes at EVERY Bloom level the material supports — recall AND understanding for everything taught, AND apply (and scenario) WHENEVER the section shows the concept being USED, performed, evaluated, computed, transformed, or applied (e.g. evaluating an expression, running an operation, deciding with a rule/condition). Do NOT stop at one outcome per concept. Aim to be thorough — a typical session supports on the order of ~20 distinct assessable outcomes across its concepts and levels; produce that breadth when the content supports it. Do NOT pre-filter for a budget; a later stage selects. But every outcome MUST be fully supported by the section — never pad with outcomes the material does not teach.

Return ONLY a JSON list. Each item:
{"concept","sub_concept","bloom_level","title","learner_action","skill_type","description","syntax","quote","justification"}

Think in THREE LEVELS — broad to fine: TOPIC (this whole section) ⊃ CONCEPT ⊃ SUB-CONCEPT.
- "concept": a BROAD, TRANSFERABLE teachable unit taught within this topic — the umbrella idea/skill, roughly one per thing the section sets out to teach (e.g. "Applying Migrations", "List Comprehensions"). It is broad like the topic, just one level down. Never an example-local label like "Project A" or a sample variable.
- "sub_concept": the FINE unit that defines GRANULARITY — the single STEP, rule, command, property, distinction, or move WITHIN the concept that THIS outcome tests (e.g. concept "Applying Migrations" → sub_concepts "makemigrations_creates_migration_files", "migrate_applies_migrations", "rollback_to_a_previous_migration"). One outcome assesses ONE sub_concept. Two outcomes under the same `concept` that test genuinely different steps/sub-ideas MUST carry DIFFERENT sub_concepts. Be granular: decompose a broad concept into its distinct assessable sub_concepts (its steps) rather than emitting one umbrella outcome. The same (concept, sub_concept) may still back several outcomes at different Bloom levels.
  ENUMERATE PARALLEL FAMILIES — do NOT collapse them. When a section presents a SET of parallel items of the SAME kind, give EACH member its OWN sub_concept (and, where the section teaches it, a SEPARATE sub_concept for that member's distinct behaviour/parameters). This is the #1 source of missed coverage — resist the urge to emit one umbrella sub_concept for the whole family. Examples of families that MUST split per member:
    · field/column/type constructors — e.g. `CharField(max_length=…)`, `DecimalField(max_digits=…, decimal_places=…)`, `URLField()`, `FloatField(min_value=…, max_value=…)`, `EmailField()` → FIVE sub_concepts, each naming the specific type + its own parameters — NOT one "creating_form_fields".
    · per-member behaviours — e.g. "DecimalField rejects non-numeric", "URLField rejects a malformed URL", "FloatField rejects out-of-range" → three distinct sub_concepts, NOT one "automatic_validation".
    · syntax/template elements — e.g. `{{ }}` interpolation vs `{{ form.as_p }}` rendering vs the auto-added `required`/input-type attributes vs auto error messages → separate sub_concepts.
    · procedure steps that each carry their own idea — e.g. detect POST vs bind data vs `is_valid()` vs `cleaned_data` vs create object vs success response → separate sub_concepts.
    · widgets/options — e.g. `PasswordInput`, `Textarea`, a non-model `BooleanField` → separate sub_concepts.
  A distinct member/behaviour that would take a DIFFERENT correct answer on a test is a DIFFERENT sub_concept, even if it shares a verb ("use", "define") with its siblings.
- "bloom_level": one of "remember" | "understand" | "apply" | "scenario", chosen to match what the section actually TEACHES about this concept:
    remember   → the section states a fact/term to recall.
    understand → the section explains reasoning/relationships.
    apply      → the section shows the concept being USED/performed/evaluated/computed/applied — a procedure, worked example, an evaluated expression, an operation, or a rule applied to inputs. A single worked example or expression evaluation is ENOUGH to justify apply; do NOT under-call apply for procedural/operational content (operators, conditions, computations, transformations).
    scenario   → the section demonstrates TRANSFER of the method to a NEW case.
  Emit apply/scenario whenever the section shows the concept in use (most procedural/operational concepts support apply). Stay at understand/remember only for purely descriptive/definitional content with no demonstrated use.
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
{"session_objective": "<what this session sets out to teach (may be empty)>",
 "incidental": ["<content present only as scaffolding/support for the focus, NOT a teaching target>", ...],
 "reading": "<the full reading, for judging depth>",
 "sub_concepts": [{"name": "<sub-concept>", "parent": "<broad concept>", "evidence": "<verbatim teaching quote>"}, ...]}

Do TWO things:

1) MERGE near-duplicates. Group sub-concepts that name the SAME assessable idea (e.g. plural/singular, reordered words, an abbreviation vs its expansion, or two phrasings of one step). Pick the clearest member name as the canonical name; carry the broad parent concept.
   MERGE ONLY TRUE DUPLICATES — the over-merge test: merge two sub-concepts ONLY when a single question could test BOTH and they would take the IDENTICAL correct answer. If two sub-concepts would take DIFFERENT correct answers on a test, they are DISTINCT — keep them SEPARATE, even when they share a verb or a parent (e.g. "use CharField" vs "use DecimalField" vs "use URLField" are THREE concepts; "DecimalField rejects non-numeric" vs "URLField rejects bad URL" vs "FloatField rejects out-of-range" are THREE concepts; `{{ }}` interpolation vs `form.as_p` rendering are TWO concepts; `is_valid()` vs `cleaned_data` are TWO concepts; `PasswordInput` vs `Textarea` are TWO concepts). Members of a parallel family (different field types, operators, widgets, HTTP methods, steps) are NOT near-duplicates. Preserve the input granularity; when in doubt, DO NOT merge. Collapsing a rich section to a handful of umbrellas is a FAILURE — it silently drops assessable coverage.
   Use a SMALL, CONSISTENT set of `parent_concept` labels: when several sub-concepts belong under one umbrella, give them the EXACT SAME parent_concept string (same words, same casing) — never emit near-duplicate parents for one idea (e.g. "Role Of The Backend" vs "Backend Role", or a spaced vs underscored variant of the same name). (This is about the PARENT label only — it does NOT license merging distinct sub-concepts.)

2) For each MERGED concept, judge — against the WHOLE reading, not just the quote — :
   - "depth": one of
       "named"    → ONLY named/listed in passing, with NO definition, explanation, or example anywhere → not assessable.
       "mention"  → stated once in ~one sentence with NO elaboration, example, config, or breakdown → assessable at recall only.
       "moderate" → defined AND elaborated: explained with reasoning/description, OR shown with a concrete example, command, config setting, or labelled breakdown.
       "deep"     → fully taught: step-by-step, worked examples, comparison, or an applied procedure.
     Judge by how the concept is ACTUALLY taught. If it is genuinely TAUGHT — defined and elaborated, OR demonstrated with an example/command/config/table — rate it "moderate" or "deep"; do NOT under-rate taught content to "named"/"mention". Reserve the lower two STRICTLY for content that is merely listed or stated once with no elaboration. Do NOT upgrade purely on importance or familiarity.
   - "in_scope": false if EITHER (a) the concept is external to this reading (named in passing / assumed from elsewhere, not actually taught here), OR (b) it is INCIDENTAL to the session objective — it appears only as scaffolding/support to demonstrate the focus and is not itself what the session teaches (matches the `incidental` list, e.g. HTML shown only to render a Django view). Otherwise true. (When session_objective is empty, judge (a) only.)
   - "why": one short line.

Return ONLY a JSON list, one object per MERGED concept:
[{"canonical_name": "...", "parent_concept": "...", "members": ["<raw sub_concept name>", ...],
  "depth": "named|mention|moderate|deep", "in_scope": true, "why": "..."}]
Every input sub_concept name MUST appear in exactly one "members" list. Return ONLY valid JSON.""")


# ── PHASE 1c · parent-concept canonicalization (collapse synonym umbrellas) ──────────────── #
PARENT_CANON_SYS = register("lo.parent_canon_sys", """\
You are given a JSON list of broad PARENT concept labels produced while analyzing ONE reading. Some name the SAME broad teaching area in different words, word order, or casing (e.g. "Role Of The Backend", "Backend Role", "Role Of Backend Development In Web Applications" all name the backend's role).

Cluster the labels that refer to the SAME broad umbrella, and give each cluster ONE clear canonical label — prefer the clearest, most general EXISTING member. Keep genuinely distinct areas SEPARATE — do NOT over-merge (e.g. "List View" and "Detail View", or "Models" and "URL Routing", are different umbrellas and must stay apart).

Return ONLY a JSON object mapping EVERY input label to its canonical label:
{"<input label>": "<canonical label>", ...}
Every input label MUST appear as a key. Return ONLY valid JSON.""")


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
- NO OVER-MERGE (the #1 failure — be aggressive): two sub-concepts belong in one group ONLY if a single question would take the IDENTICAL correct answer on both. If they would take DIFFERENT correct answers, SPLIT them into separate groups — even when they share a verb or parent. Members of a parallel family are ALWAYS separate: different field/widget/operator/method types (CharField vs DecimalField vs URLField vs FloatField vs EmailField → 5 groups), each type's distinct rejection/behaviour, `{{ }}` interpolation vs `form.as_p` rendering vs auto-added attributes vs auto error-messages, is_valid() vs cleaned_data, PasswordInput vs Textarea — each is its OWN group. If a rich section has been collapsed to a few umbrellas, that IS over-merge — expand it back to the distinct assessable ideas.
- NO UNDER-MERGE: two names for the SAME idea (plural/singular, reorder, abbreviation, two phrasings that take the same answer) must share one group.
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
