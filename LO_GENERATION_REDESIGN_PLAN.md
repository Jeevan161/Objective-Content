# LO Generation Pipeline — Redesign (v2)
### Hybrid agentic flow · feasibility-driven Bloom · scenario tier · **human-in-the-loop** · RAG-anywhere

**Status:** Draft for review
**Location:** project root (this file) — the canonical, viewable plan.
**Scope:** the LO-generation stage of `backend/app/mcq_pipeline` (FastAPI backend; Django `courses/`
is legacy). The question-generation stage is touched only at its LO interface (bloom vocabulary +
scenario hint); its programming coupling is a separate effort.

---

## ★ The human is in the loop — two approval gates

This is the headline change, so it's stated first:

```
 …Planner ─▶ ⛔ GATE 1: a human APPROVES/REJECTS the proposed LO DIVISION ─▶ Author…
 …Judge   ─▶ ⛔ GATE 2: a human APPROVES/REJECTS every LO by its CONCEPT MAPPING ─▶ questions…
```

- **Nothing is authored** until a human approves *how many* LOs, *which Bloom tiers*, and *which
  concepts are in/out of scope* (Gate 1).
- **No questions are generated** until a human approves the final LOs and their concept mapping
  (Gate 2).
- The run literally **pauses** at each gate (LangGraph `interrupt` on the durable Postgres
  checkpointer) and **resumes** on the human's decision. Approve → continue; Reject + note →
  regenerate. Full UX in **§11**.

---

## 0. Executive summary

The LO pipeline is a LangGraph flow that turns a session's reading material into a frozen set of
Learning Outcomes, which then drive question generation. The backbone is solid (self-consistency
voting, evidence-binding, durable checkpointer, per-run isolation), but it has four
product-readiness gaps:

1. **Muddled flow** — the same judgment is computed 2–3 times in different ways (procedurality in 3
   places, taught-depth in 3 places).
2. **Domain coupling** — programming/English heuristics (`pip/venv/mkdir` regex, `debug/trace` cue
   words, English contrast cues, code-fence gating) are baked into "rules," biasing every
   non-programming subject.
3. **Non-uniform rubrics** — the strict coverage rubric is applied selectively; self-containment is
   only an authoring *instruction*; repair *downgrades* failing LOs to `"Identify X"` instead of
   re-authoring.
4. **No human checkpoint, and a fixed Bloom split** — the 12/8 split is hard-coded; the run is fully
   autonomous with no approval gate.

This redesign delivers:
- **★ two human-in-the-loop gates** (approve/reject) — one on the LO *division*, one on the
  LO↔concept *mapping* — via the existing durable checkpointer for clean pause/resume;
- a **hybrid agent architecture** — the deterministic graph keeps orchestrating, but every LLM stage
  becomes a named, contracted **sub-agent**, plus a new **Planner** sub-agent;
- a **feasibility-driven 4-tier Bloom division** (Remember / Understand / Apply / Scenario) derived
  from per-concept taught-depth + procedurality, replacing the fixed split;
- a **scenario tier** (situation-framed questions) for `deep & procedural` concepts;
- **one unified LLM Judge** (R1–R8) scoring *every* LO against *every* criterion, atop a thin
  deterministic structural validator;
- a **regenerate-with-feedback repair loop**;
- **prerequisite RAG available to any stage** (see Design Principle P5).

### Design principles
- **P1 — One judgment per concern.** Procedurality = the LLM `applied_skill` vote, full stop.
  Taught-depth = the DepthProfiler, full stop. No duplicate heuristics.
- **P2 — Deterministic only for true invariants.** Counts, splits, acyclicity, verbatim-quote
  grounding, verb-in-vocabulary. Everything judgmental is LLM.
- **P3 — No domain-specific constraints.** Remove programming/English heuristics from the LO
  pipeline. Every LO follows every rubric criterion uniformly (no gating).
- **P4 — Human approves division and final mapping.** Two gates, approve/reject only.
- **P5 — Prerequisite RAG is available everywhere.** Any stage may call `rag_api.check_concept` (and
  related RAG) over the course + prerequisite-course scope. Used especially by the **Judge**
  (answerability/no-beyond-scope must credit prerequisite knowledge, not just this page) and the
  **Planner** (feasibility may credit foundations taught earlier). See §12.

---

## 1. Decisions locked (from discussion)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Hybrid** orchestration: deterministic graph + named sub-agents (+ a Planner agent). NOT a free-form LLM orchestrator. | Keeps reproducibility (`spec_hash`), evidence-grounding, auditable validators; the durable checkpointer makes HITL pause/resume nearly free. A free orchestrator regresses all of these and costs more. |
| D2 | **Four Bloom tiers**: Remember / Understand / Apply / Scenario, each gated by feasibility. | Expressive enough to express "scenario = higher-order apply"; division reflects what the material can actually support. |
| D3 | **HITL = approve / reject only** (reject → regenerate with a note; no inline editing). | Smaller API/state surface than inline edit; still gives humans control over division and final LOs. |
| D4 | **Prerequisite RAG anywhere** (P5). | A learner is assumed to have the prerequisites; judging must reflect that, and feasibility can credit prior-course foundations. |
| D5 | **Carried from v1:** one unified Judge, material-driven budget, regenerate repair, strip domain-coupling, keep structural invariants. | Already approved; v2 extends them. |

---

## 2. Current flow (as-is, for reference)

```
parse_structure → extract_concepts → canonicalize_concepts → build_dependency_graph
→ profile_coverage → plan_allocation → author_outcomes → resolve_prerequisites
→ coverage_gate → validate ──(fail & retries left)──> repair ──> resolve_prerequisites…(loop)
        └─(pass / retries exhausted)─> finalize → lo_to_legacy → sequence_outcomes
        → recommend_question_types → generate_questions → review_questions → END
```
Entry: `runner.run_mcq_pipeline(...)` builds a `RagAdapter` + `ProgressReporter`, registers a
`RunContext` keyed by `thread_id`, and calls `graph.invoke(state0, cfg)` straight to completion (no
pause). Validators V1–V14 live in `lo_nodes.validate`; deterministic helpers in `lo_concept_graph.py`.

---

## 3. Target flow (v2) — human gates highlighted

```
  parse_structure                         # D  split source into topic sections (markdown headings)
  → Extractor                             # A  concepts per section (K-sample self-consistency)
  → canonicalize_concepts                 # D  canonical ids, dedup, evidence-bind descriptions
  → DependencyMapper                      # A  prereq DAG + applied_skill (procedurality) + assumed_prior
  → DepthProfiler                         # A  taught depth {mention|moderate|deep} + RAG scope-closure
  → Planner                               # A  feasibility (+prereq RAG) → budget + 4-tier division + scope
  ══════════════════════════════════════════════════════════════════════════════════════════
  → review_division        ⛔ GATE 1 (H)  ── interrupt: human APPROVES/REJECTS the DIVISION
  ══════════════════════════════════════════════════════════════════════════════════════════
  → Author + ScenarioAuthor               # A  author LOs within the approved division (+ scenario LOs)
  → resolve_prerequisites                 # D  prereq closure + RAG-verified provenance
  → Judge                                 # A  unified R1–R8 rubric, EVERY LO (+prereq RAG for R3/R4)
  → validate                              # D  structural invariants + composite rubric gate
        │  ──(fail & retries left)──> Repairer ──> resolve_prerequisites ──> Judge ──> validate
        └─(clean / retries exhausted)
  ══════════════════════════════════════════════════════════════════════════════════════════
  → review_outcomes        ⛔ GATE 2 (H)  ── interrupt: human APPROVES/REJECTS LOs by CONCEPT MAPPING
  ══════════════════════════════════════════════════════════════════════════════════════════
        │  ──(reject + note)──> Repairer (rejected LOs only) ──> Judge ──> review_outcomes
        └─(approve)
  → finalize → lo_to_legacy → sequence_outcomes
  → recommend_question_types → generate_questions (scenario framing) → review_questions → END
```
`D` = deterministic plumbing · `A` = LLM sub-agent · `H` = **human gate**.

---

## 4. Sub-agent roster (contracts)

Each sub-agent = a DB-overridable prompt (`prompt_store.register`/`get_prompt`) + structured I/O.
Existing drift-suppression (K-sample voting, controlled enums) is retained.

| Sub-agent | Node | Input | Output | Notes |
|---|---|---|---|---|
| **Extractor** | `extract_concepts` | section text | `[{name, description, quote}]` | unchanged; K-sample majority vote |
| **DependencyMapper** | `build_dependency_graph` | target concept + others | `{prerequisites[], applied_skill, assumed_prior[]}` | **sole** source of procedurality |
| **DepthProfiler** | `profile_coverage` | concept + section (+ scope RAG) | `{depth, why}` | **sole** owner of taught-depth |
| **Planner** *(new)* | `plan_allocation` | feasibility table + requested budget (+ prereq RAG) | division proposal (§6) | LLM proposes *within* deterministic ceilings |
| **Author** | `author_outcomes` | approved division + topic concepts | LOs per topic | verb clamped to ceiling; **no filler synthesis** |
| **ScenarioAuthor** *(new)* | `author_outcomes` (parallel) | `deep & procedural` concepts | scenario-tier LOs | target ~2, feasibility-gated, 0 allowed |
| **Judge** | `judge_outcomes` (was `coverage_gate`) | one LO + section + source (+ prereq RAG) | R1–R8 verdict (§9) | sequential, temp 0, signature-cached |
| **Repairer** | `repair` (LLM half) | failing LO + judge reasons + depth ceiling | rewritten LO | regenerate, not downgrade |

---

## 5. Feasibility-driven 4-tier Bloom division

### 5.1 Per-concept feasibility ceiling
From DepthProfiler `depth` + DependencyMapper `applied_skill`:

| taught depth | not procedural | procedural |
|---|---|---|
| **mention** | Remember | Remember |
| **moderate** | Remember, Understand | Remember, Understand, Apply |
| **deep** | Remember, Understand | Remember, Understand, Apply, **Scenario** |

`mention` concepts not substantively explained are dropped from scope by `DROP_NAMED_ONLY` (today's
behavior). **Prerequisite RAG (P5):** the Planner may *raise* feasibility for a concept whose
foundations are confirmed taught in a prerequisite course (so an apply LO that builds on a prior
course isn't blocked for "missing prerequisite in this session").

### 5.2 Verb vocabulary (4-tier) — `lo_config.py`
- `REMEMBER_VERBS = {identify, list, label, recognize, match, name, define, state}`
- `UNDERSTAND_VERBS = {explain, describe, summarize, interpret, classify, outline, compare, distinguish, differentiate, illustrate}`
- `APPLY_VERBS = {execute, implement, apply, write, compute, solve, construct, use, modify, calculate, debug, trace, develop, build, perform, produce}`
- `SCENARIO_VERBS = {apply, solve, determine, diagnose, predict, recommend, choose, decide, evaluate}`
- `VERBS = {remember, understand, apply, scenario}`; `allowed_verbs_for(depth, procedural)` returns
  the union of the permitted tiers' verbs.

### 5.3 Capacity & division math (Planner + deterministic guardrails)
- `requested = question_budget or 20` (user-supplied; default 20).
- per-tier capacity = in-scope concepts whose ceiling includes that tier × `MAX_LOS_PER_CONCEPT` (=2).
- `capacity = total in-scope concepts × MAX_LOS_PER_CONCEPT`.
- `final_budget = max(5, (min(requested, capacity) // 5) * 5)` → tiers 20/15/10/5; floor 5.
- `budget_reduced = final_budget < requested` (recorded in `overrides`); **NEEDS_REVIEW** when
  `final_budget ≤ 10`.
- **Planner** proposes per-tier counts summing to `final_budget`, respecting capacity + pedagogy
  (foundations first), scenario capped ~2. Theory-heavy → apply=scenario=0 (flagged); hands-on →
  more apply + scenario.
- `MIN_BUDGET = 5` (was 4); `QUESTION_BUDGET = 20` = default *ceiling* only; `DEFAULT_SPLIT` removed.

### 5.4 Schema — `lo_schemas.py`
- `bloom_category`/`bloom_level` enum → `remember | understand | apply | scenario`.
- add `scenario: bool = False`; keep `syntax` as optional metadata.

---

## 6. The LO division proposal (Gate-1 payload)

Produced by Planner; shown to the human; persisted in state + artifact:
```json
{
  "requested_budget": 20, "final_budget": 15, "budget_reduced": true, "capacity": 16,
  "tier_counts": {"remember": 5, "understand": 6, "apply": 3, "scenario": 1},
  "per_topic": [{"topic_id": "...", "title": "...", "slots": 4,
                 "tiers": {"remember": 2, "understand": 1, "apply": 1, "scenario": 0}}],
  "in_scope":  [{"concept_id": "...", "name": "...", "depth": "deep", "procedural": true,
                 "ceiling": ["remember","understand","apply","scenario"],
                 "prereq_support": "taught_earlier"}],
  "dropped":   [{"concept_id": "...", "name": "...", "reason": "named in passing; not explained"}],
  "flags":     ["budget_reduced", "no_scenario_feasible"]
}
```

---

## 7. Scenario tier — questions (grounded in the real question stage)

**Reality check from the code:** the platform has **9 fixed question types**
(`MULTIPLE_CHOICE, TRUE_OR_FALSE, MORE_THAN_ONE_MULTIPLE_CHOICE, TEXTUAL, CODE_ANALYSIS_*×3,
FIB_CODING, REARRANGE`). **"Scenario" is not a type — it is a stem *framing*.**
- **LO layer:** `ScenarioAuthor` writes scenario-tier LOs answerable as a transferable situation.
- **Question layer:** `question_type_agent.recommend_one` maps a scenario LO to an MCQ-family type
  (or `CODE_ANALYSIS_*` for code) + a `scenario: true` hint; `qgen_agents` frames a self-contained,
  generic situation in the stem (it already bans source-local scenario labels — `qgen_agents.py:147`).
- **Bloom plumbing:** `_DIFFICULTY` (`qgen_agents.py:39`) `remember→EASY, scenario→HARD`;
  `_fallback_type` (`question_type_agent.py:89`) handles new tiers; `_BLOOM_TO_LEGACY`
  `remember→remember, understand→understand, apply→apply, scenario→apply` + `is_scenario`.
- **Target:** ~2 scenario questions overall, feasibility-gated (0 allowed; flagged at Gate 1).

---

## 8. Unified Judge (R1–R8) — replaces `coverage_gate`/V13, absorbs V8/V11/V12

One LLM call per LO (sequential, temp 0, signature-cached so the loop re-scores only changed LOs).
Extends the current `lo.coverage_rubric` → `lo.rubric`. **Every LO scored on every criterion.**
```json
{"R1_present":bool,"R2_depth":bool,"R3_answerable":bool,"R4_in_scope":bool,
 "R5_answer_key":bool,"R6_self_contained":bool,"R7_distinct":bool,"R8_apply_valid":bool,
 "fail_reasons":{"R4":"...","R6":"..."}, "suggested_fix":"...", "supported_depth":"moderate"}
```
- **R1 PRESENT** — concept explicitly taught (not named/alluded/assumed).
- **R2 DEPTH MATCHES DEMAND** — verb demand ≤ taught depth; comparison verbs need explicit contrast.
  *(Replaces V12 + `_CONTRAST_CUE`.)*
- **R3 ANSWERABLE** — a learner **who has the prerequisites** can answer. **Prerequisite RAG (P5):**
  knowledge taught in this session OR confirmed (via `rag_api.check_concept`) in a prerequisite
  course counts as available; only genuinely-untaught outside knowledge fails R3.
- **R4 NO BEYOND-SCOPE LEAP** — the LO doesn't reach past what's covered *here or in prerequisites*
  (RAG-checked). The key failure: concept covered, but the LO reaches past it.
- **R5 ANSWER KEY DERIVABLE.**
- **R6 SELF-CONTAINED & TRANSFERABLE** *(NEW — judged, not just instructed)*.
- **R7 DISTINCT & SINGLE-ANSWER** *(NEW)*.
- **R8 APPLY-VALIDITY** *(NEW, domain-agnostic; replaces V8 regex + V11 + `is_setup_or_cli`)* — for
  apply/scenario LOs: the material shows *how* (steps/worked example/method).

Degrades to "all pass" if the LLM is unavailable (never blocks a run), as today.

---

## 9. Slim structural validator (`validate`)

| Rule | Keep? | Meaning |
|---|---|---|
| V1 | ✅ | `count == final_budget` |
| V2 | ✅ (4-tier) | realized tier counts == approved division |
| V3 | ✅ | per-topic slot counts |
| V4 | ✅ | every in-scope concept covered |
| V5 / V6 | ✅ | apply/scenario LOs have an in-scope (or prereq-RAG-confirmed) prereq closure |
| V7 | ✅ | DAG acyclicity |
| V8 | ✅ *de-coupled* | apply/scenario verb + LLM-flagged procedural concept (no regex) |
| V9 | ✅ | `source_evidence.quote` appears **verbatim** in source (anti-hallucination) |
| V10 | ✅ (4-tier) | `learner_action ∈ VERBS[tier]` |
| V11 | ❌ delete | code-syntax grounding → R8/R3 own it |
| V12 | ❌ delete | depth over-reach heuristic → R2 owns it |
| V13 | → composite | "fail if any LO fails any R" composite rubric gate |
| V14 | ✅ | no LO targets an out-of-scope concept |

---

## 10. Regenerate-with-feedback repair (`repair`)

- **Rubric failures (R1–R8):** one Repairer call per failing LO with `{LO, fail_reasons,
  suggested_fix, supported_depth, section text, valid concept ids}` → rewrite to be answerable at the
  supported depth. Re-coerce (`_coerce_outcome`) then re-judge (cache re-scores only the change).
- **Structural failures:** keep cheap deterministic fixes (V4 coverage-gap retarget; V9 re-ground).
- **Terminal fallback only:** deterministic downgrade-to-recall runs *only* at `MAX_RETRIES`
  exhaustion → shippable set flagged NEEDS_REVIEW. The loop body never downgrades.
- `MAX_RETRIES`: **3** (was 2). *(open — §15)*

---

## 11. Human-in-the-loop (two gates, approve/reject only)

**Mechanism:** LangGraph dynamic `interrupt()` in two new gate nodes, on the **existing durable
Postgres checkpointer** — state persists across the pause; resume continues from the exact checkpoint.

### 11.1 What the reviewer sees and does

| | **GATE 1 — review_division** | **GATE 2 — review_outcomes** |
|---|---|---|
| **When** | after Planner, before any LO is authored | after the validate/repair loop converges, before questions |
| **Shown** | the §6 division proposal: budget (requested vs final + why), 4-tier counts, per-topic split, in-scope concepts w/ depth & feasibility, dropped concepts w/ reason, flags | every LO with its concept mapping, Bloom tier, evidence quote, and R1–R8 verdicts; flags (NEEDS_REVIEW, scenario=0) |
| **Approve** | proceed to Author/ScenarioAuthor | proceed to finalize → questions |
| **Reject + note** | Planner re-proposes the division using the note (loop to `plan_allocation`) | Repairer regenerates the **rejected LOs only** with the note → Judge → back to Gate 2 |
| **Auto** | if `hitl_enabled=False` (tests/headless), auto-approve | same |

### 11.2 Resumability plumbing
- `runner.py`: split `run_mcq_pipeline` into
  - `start_run(...) -> {status, review_payload, thread_id}` — invoke until the first interrupt;
  - `resume_run(thread_id, decision) -> {...}` — rebuild `RagAdapter`/`ProgressReporter` (reuse
    `build_adapter`) and **re-register the `RunContext`** by `thread_id`, then
    `graph.invoke(Command(resume=decision), cfg)`; continues to the next gate or completion.
- `lo_state.RunContext` gains: `question_budget`, `hitl_enabled` (default `False`), review payloads.
- `services/jobs.py`: lifecycle `running → awaiting_review:gate1 → running → awaiting_review:gate2 →
  done`; store payload + thread_id on the job.
- `api/courses.py`: return the review payload when paused; add an approve/reject endpoint →
  `resume_run`; accept `question_budget`.
  *(jobs.py + the course route not yet read in detail — confirm exact shapes during implementation.)*

---

## 12. Prerequisite RAG — used anywhere (P5)

The RAG adapter is already scoped to the **course + prerequisite courses**; `rag_api.check_concept`
returns whether a concept is EXPLAINED in that scope. Today it's used only in `profile_coverage`
(scope-closure) and `resolve_prerequisites` (provenance). Per your direction, it becomes a shared
capability any stage may call:
- **Judge (R3/R4):** confirm that knowledge an LO relies on is taught *somewhere accessible* (this
  session or a prerequisite course) before failing it for "outside knowledge / beyond scope."
- **Planner:** raise a concept's feasibility when its foundations are confirmed taught earlier.
- **Repairer:** when rewriting, prefer phrasings whose prerequisites RAG confirms are in scope.
- **Caching:** keep the per-run `cover_cache` pattern from `resolve_prerequisites` (and the
  signature cache) so liberal RAG use doesn't blow up call volume; all calls degrade gracefully when
  RAG is unavailable (treat as unknown → never hard-fail an LO on a RAG outage).

---

## 13. Strip domain-coupling (precise)

**Stop *using* (in the LO pipeline):** `_SETUP_CLI_RE`/`is_setup_or_cli` (drop from `_apply_suitable`);
`_STRONG_PROC`/regex body of `is_procedural`; `has_explicit_contrast`/`_CONTRAST_CUE`; `named_in_fence`
code-fence gating; `_RECAP` content-deletion; `_reconcile_counts` filler synthesis.
**Keep/demote:** `concept_depth` → DepthProfiler fallback-only (removed from validation); **keep the
`is_setup_or_cli` function** (imported by `question_type_agent.py:18,96,125` — question stage stays
programming-coupled and out of scope); keep `ground_quote`, `syntax_grounded`, `loosen_text`,
`largest_remainder`, `reachable`, `slugify`, `canonical_name`, `graph_find_prerequisites`.

---

## 14. File-by-file change list

| File | Changes |
|---|---|
| `lo_graph.py` | Add `review_division` + `review_outcomes` interrupt nodes + conditional approve/reject edges; rename `coverage_gate`→`judge_outcomes`. |
| `lo_nodes.py` | Planner + ScenarioAuthor; `judge_outcomes` (R1–R8, + prereq RAG); slim `validate` (drop V11/V12, 4-tier V2/V10, de-couple V8, composite gate); Repairer regenerates; drop `_reconcile_counts`/`_RECAP`/`is_procedural` call/`named_in_fence`/`is_setup_or_cli` use; `apply_suitable = procedural(LLM) ∧ depth≠mention`. |
| `lo_concept_graph.py` | Demote `concept_depth`; remove `_STRONG_PROC`, `has_explicit_contrast`/`_CONTRAST_CUE`. Keep `is_setup_or_cli`. |
| `lo_config.py` | 4-tier verb sets + `VERBS`; `allowed_verbs_for(depth, procedural)`; feasibility table; budget knobs (`MIN_BUDGET=5`, drop `DEFAULT_SPLIT`). |
| `lo_schemas.py` | 4-tier `bloom_*` enum + `scenario` flag. |
| `lo_state.py` | `RunContext`: `question_budget`, `hitl_enabled`, review payloads; `LOState`: `division_proposal`, gate decisions/notes. |
| `lo_artifact.py` | Surface `division_proposal`, per-LO R1–R8 verdicts, scenario flag, budget flag, gate-approval audit; keep non-binding `syntax` null-out. |
| `runner.py` | `start_run`/`resume_run` split; re-register RunContext on resume; thread `question_budget` + `hitl_enabled`. |
| `services/jobs.py` | `awaiting_review` states + stored payload/thread_id. *(read first)* |
| `api/courses.py` | Return review payload when paused; approve/reject endpoint → `resume_run`; accept `question_budget`. *(read first)* |
| `question_type_agent.py`, `qgen_agents.py` | Scenario style hint + 4-tier bloom mappings. |
| Prompts (DB) | `reset_prompt` for `lo.planner`, `lo.scenario_author`, unified `lo.rubric`; update `lo_rules.json` docs. FastAPI env: `backend/.venv`. |

---

## 15. Implementation phases

1. **Schema + config foundation** — 4-tier enum, verb sets, feasibility table, budget knobs. (No
   behavior change; build stays green.)
2. **De-couple + consolidate** — procedurality = LLM only; depth = DepthProfiler only; drop domain
   heuristics + filler synthesis; slim `validate` (delete V11/V12, de-couple V8).
3. **Unified Judge** — R1–R8 rubric (+ prereq RAG); composite gate; regenerate Repairer.
4. **Planner + feasibility division** — replace the fixed split with the Planner proposal + budget
   quantization.
5. **Scenario tier** — ScenarioAuthor + question-stage scenario hint + bloom plumbing.
6. **HITL** — gate nodes + interrupts; `start_run`/`resume_run`; jobs + API states (behind
   `hitl_enabled`). Largest piece; built last on a stable core.
7. **Prompts live** — `reset_prompt`; `lo_rules.json` docs.

Each phase is independently testable; the build stays runnable between phases.

### Open decisions (proposed defaults — confirm)
| # | Question | Proposed default |
|---|---|---|
| O1 | Scenario count when material supports 0 | Proceed with 0 + flag at Gate 1. |
| O2 | Gate-2 rejection granularity | Per-LO reject + note → regenerate just those. |
| O3 | `MAX_RETRIES` | Bump 2 → 3. |
| O4 | Question-stage de-coupling | Out of scope this pass. |
| O5 | Migration | Forward-only. |

---

## 16. Verification & test plan

- **Unit:** feasibility ceilings; Planner respects ceilings + per-tier capacity, sums to
  `final_budget`; budget quantization (cap 3→5; req 15 & cap 40→15; req 20 & cap 7→5 +
  `budget_reduced`+NEEDS_REVIEW); slim `validate` (no V11/V12; 4-tier; composite gate).
- **HITL:** `start_run` pauses at Gate 1 with payload; `resume_run(approve)` proceeds;
  `resume_run(reject, note)` re-proposes; same for Gate 2; `hitl_enabled=False` runs end-to-end.
- **Prereq RAG:** an LO relying on a prior-course concept passes R3/R4 when `check_concept` confirms
  it; fails when it's genuinely untaught; RAG outage never hard-fails an LO.
- **Domain-agnosticism:** a non-programming session (biology/history) → no LO dropped/penalized by a
  code/CLI heuristic; apply-suitability only from the LLM signal.
- **End-to-end** (FastAPI, `backend/.venv`, Postgres+pgvector): a programming session yields valid
  apply + ~2 scenario LOs/questions; every shipped LO passes R1–R8 (or NEEDS_REVIEW with reasons);
  quotes ground (V9); loop converges within `MAX_RETRIES`. Inspect `artifact.knowledge_map`,
  `division_proposal`, `overrides`.
- **Regression:** the Judge must not over-reject a previously-passing programming session.

---

## 17. Risks & mitigations

| Risk | Mitigation |
|---|---|
| HITL resume must re-create run-scoped objects (RagAdapter/Progress aren't checkpointed) | `resume_run` rebuilds them via `build_adapter` + re-registers the `RunContext` before `invoke(Command(resume=…))`. |
| Liberal prerequisite RAG (P5) → call volume | Reuse the per-run `cover_cache` + Judge signature cache; degrade gracefully on outage. |
| More LLM calls (Planner, ScenarioAuthor, R6–R8) → cost/latency | Judge stays cached; Planner/Scenario are 1 call each. |
| 4-tier bloom breaks 2-value consumers | Legacy bridge + explicit `_DIFFICULTY`/`_fallback_type` cases; regression test. |
| Removing the fixed split changes expected question counts | Budget user-supplied (default 20); division surfaced + approved at Gate 1 before authoring. |
| Question stage still programming-coupled | Out of scope; `is_setup_or_cli` kept for it; tracked as follow-up. |

---

## 18. Build status (v2 implemented)

**Done & verified** (all 28 `mcq_pipeline` modules + API import cleanly; pure-logic unit tests pass; graph compiles with gates; routing checked):
- **Foundation** — `lo_config.py`: 4-tier verbs, `feasible_tiers`, `allowed_verbs_for(depth, procedural)`, budget knobs (`MIN_BUDGET=5`, `BUDGET_STEP=5`, `MAX_RETRIES=3`); `lo_concept_graph.py`: removed `_STRONG_PROC`/`is_procedural`, `has_explicit_contrast`/`_CONTRAST_CUE`; `concept_depth` demoted to DepthProfiler fallback; `is_setup_or_cli` kept for the question stage only.
- **lo_nodes** — Planner (`plan_allocation`): feasibility 4-tier division + budget quantization + Gate-1 `division_proposal`; procedurality = LLM `applied_skill` only; assignment-based author (`_reconcile_to_assignments`, grounded synthesis — no junk filler); unified Judge `judge_outcomes` (R1–R8, prereq-RAG context); slim `validate` (V1–V10/V14 + composite V13 gate; V11/V12 deleted; V8 de-coupled); regenerate-with-feedback `repair` (terminal recall fallback only). `_RECAP` deletion removed.
- **Bridge/graph/state/runner** — `lo_artifact`: 4-tier `_BLOOM_TO_LEGACY` + `is_scenario`, budget-flag NEEDS_REVIEW, surfaced `division_proposal` + per-LO rubric verdicts; `lo_graph`: node renamed `coverage_gate`→`judge_outcomes`; `lo_state`: RunContext `question_budget`/`hitl_enabled`, `division_proposal`/`gate_decision` channels; `runner`: `question_budget` threaded.
- **Question stage** — scenario→MCQ routing (`_fallback_type` + recommend guard), `scenario`→HARD difficulty via `bloom_level_raw`, `gen.scenario_rules` stem-framing prompt; `progress.py` stage labels updated.
- **HITL (graph + runner)** — `review_division` / `review_outcomes` interrupt gate nodes wired in, **inert pass-through when `hitl_enabled=False`** (default → zero behavior change); reject loops (Gate 1→re-plan, Gate 2→regenerate rejected); `runner.resume_run` + `_interrupt_payload` for pause/resume on the existing durable checkpointer.

**DONE since the v2 build:** all 6 adversarial-review findings fixed; **HITL jobs/API wired** —
`SyncJob.AWAITING_REVIEW` state, `start_mcq_job` carries `question_budget`/`hitl_enabled`, a paused
run parks the review payload on `progress.review` (+ `durable_checkpoint`), and
`POST /courses/mcq/jobs/{job_id}/resume/` → `start_mcq_resume_job` → `runner.resume_run`
(approve/reject, may pause again at the next gate); `McqGenerateRequest` gained `question_budget` +
`hitl`; activation script `backend/scripts/activate_lo_v2_prompts.py` created.

**Remaining (needs the live stack — Postgres + pgvector + LLM):**
1. **Run the prompt activation** — `cd backend && PYTHONPATH=. .venv/bin/python scripts/activate_lo_v2_prompts.py`
   (seeds `lo.rubric` + `gen.scenario_rules`, resets `lo.author_sys` + `lo.repair_sys` to v2
   defaults). REQUIRED before a real run, else the live author/repair prompts hold stale `<N_RU>` /
   `<BLOOM_LEVEL>` placeholders. *(DB was down in the build env — this is the one ops step left.)*
2. **End-to-end run** — full pipeline on a real session; a non-programming session for
   domain-agnosticism; ~2 scenario LOs/questions on a hands-on session; and one HITL run
   (`hitl: true` → poll job → `AWAITING_REVIEW` + `progress.review` → `resume/`).
3. **`lo_rules.json`** reference-doc sync (remove V11/V12, note de-coupled V8) — cosmetic.

**DONE since (frontend HITL + bug fix):**
- **Frontend HITL UI** — new `McqReviewGate.jsx` renders Gate-1 (division proposal: budget, 4-tier
  counts, in-scope/dropped concepts, flags) and Gate-2 (outcomes with per-LO reject checkboxes +
  note + rubric verdict). `McqGenerationPage` adds a **Questions** budget input + **Human review**
  toggle, treats `AWAITING_REVIEW` as a paused state (stops polling, shows the gate), and POSTs
  approve/reject → `resumeMcq` → resume endpoint. `api.js` got `question_budget`/`hitl` on generate
  + `resumeMcq`. `mcq.css` got the gate styles + `b-scenario` badge. **Frontend builds clean.**
- **Bug fix** — `MAX_RETRIES` was used in `repair()` but missing from `lo_nodes`'s `lo_config`
  import (a NameError that only fired when a run hit the repair loop — past the import/unit checks).
  Now imported; the repair path is exercised by a deterministic test; an AST undefined-name sweep is
  clean across all changed files. Also dropped two now-unused verb-set imports.

**Adversarial review (done):** a 16-agent Workflow (integration / 4-tier / HITL-safety / decoupling, each finding independently verified) confirmed **6 real issues (0 critical, 2 high, 2 medium, 2 low) — ALL FIXED & re-verified**:
1. *(high)* `repair` now **reconciles `allocation_plan`** (tier_counts + per-topic slots) with the repaired outcomes, so a legitimate fix no longer makes V2/V3 spuriously fail → needless NEEDS_REVIEW; a genuine tier-**downgrade** is surfaced as an explicit `tier_downgraded` override with a clear NEEDS_REVIEW reason (was a confusing V2 mismatch).
2. *(high)* `effective_bloom_split` now emits real **4-tier counts** (scenario was being lumped into remember_understand); `frontend/McqResults.jsx` renders all four tiers.
3. *(medium)* terminal-downgrade escalation reason is now specific (folded into #1).
4. *(low)* `finalize` syntax null-out now covers **scenario** LOs, not just apply.
5. *(low)* `no_scenario_feasible` flag now emitted in the division proposal (Gate-1 visibility).
6. *(medium)* HITL pause payload now surfaces **`durable_checkpoint`** so a non-durable in-memory fallback isn't treated as resumable.
```
