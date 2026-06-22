# Learning-Outcome Pipeline Replacement — Integration Plan

**Status:** implemented (pending live end-to-end) · **Date:** 2026-06-18

## Implementation status

Built and verified (imports + deterministic smoke + graph compile + `py_compile`):

| File | State |
|---|---|
| `mcq_pipeline/lo_config.py` | NEW — budget/split/K/verbs/alias constants |
| `mcq_pipeline/lo_llm.py` | NEW — `chat()` + `parse_json()` over `ChatOpenAI` |
| `mcq_pipeline/lo_concept_graph.py` | NEW — pure deterministic core (no LLM/DB) |
| `mcq_pipeline/lo_state.py` | NEW — `LOState` TypedDict + `RunContext` registry |
| `mcq_pipeline/lo_nodes.py` | NEW — nodes 1–9 (parse…repair) |
| `mcq_pipeline/lo_artifact.py` | NEW — `finalize()` + `lo_to_legacy()` bridge |
| `mcq_pipeline/lo_graph.py` | REWIRED — 10-node flow + repair loop + question stage + checkpointer factory |
| `mcq_pipeline/runner.py` | RunContext registry, `thread_id`, artifact in result |
| `mcq_pipeline/progress.py` | new `STAGE_DEFS` (10 LO + bridge + 3 question) |
| `services/jobs.py` | `_MCQ_SEMAPHORE` bounded concurrency, `thread_id=job_id` |
| `db/session.py` | pooled (`db_pool_size`/`max_overflow`/`timeout`) |
| `core/config.py` | `mcq_max_concurrent_jobs`, `mcq_checkpointer`, pool sizes |

Production-hardening delivered: state holds **plain data only** (live `rag`/`progress`
ride in the `RunContext`, keyed by `thread_id`, never checkpointed); checkpointer is a
**pooled, lazy, fail-fast** `PostgresSaver` with graceful `InMemorySaver` fallback;
job concurrency is **semaphore-bounded**; the SQLAlchemy pool is **sized**.

Deferred: live end-to-end run (needs the DB up, an OpenRouter key, and an ingested
course) to exercise the 4 agent nodes + question stage.

---

**Original plan below.**

**Status:** proposed · **Date:** 2026-06-18

Replace the current LO-creation logic in the FastAPI backend (`backend/app/mcq_pipeline/`)
with the POC's deterministic 10-node Learning-Outcome pipeline
(`/home/nxtwave/poc/Objective Content Workflow/_build_lo.py`,
`lo_workflow.ipynb`), while keeping the existing question-generation pipeline.

## Decisions (locked)

1. **Scope:** Replace the LO-creation nodes only; keep `recommend_question_types →
   generate_questions → review_questions`. A new adapter node bridges the new
   outcome schema to the legacy `LearningOutcome` shape the question nodes consume.
2. **Prerequisites:** Use **both** — the in-session concept-DAG ancestor closure
   (POC) **and** the DB cross-course prereq units the backend already resolves
   (additive, not either/or).
3. **State management:** Port the POC's state dict into a LangGraph `TypedDict`
   **and** add a Postgres checkpointer so the repair loop, escalation, and all
   intermediates are durable, inspectable, and resumable.

---

## 1. The seam

Current graph (`mcq_pipeline/lo_graph.py:390`):

```
START → extract_facts → {agent_remember ‖ agent_apply} → merge_validate
      → recommend_question_types → generate_questions → review_questions → END
```

Cut everything **before** `recommend_question_types`. Splice in the new flow,
ending with a `lo_to_legacy` adapter that produces `final_los`. The three
question nodes stay unchanged.

New graph:

```
START
  → parse_structure          (D)
  → extract_concepts         (A, K=3 self-consistency)
  → canonicalize_concepts    (D)        ← variance sink
  → build_dependency_graph   (A, K=3 edge voting)
  → plan_allocation          (D)
  → author_outcomes          (A, 1 call per topic)
  → resolve_prerequisites    (D)        ← in-session DAG closure
  → validate                 (D) ──cond──┐
        pass            → finalize        │ fail + retries → repair → resolve_prerequisites
        fail+no-retries → finalize(NEEDS_REVIEW)
  → finalize                 (D)        ← freeze + hash + provenance + attach DB prereqs
  → lo_to_legacy             (D, NEW)   ← schema adapter
  → recommend_question_types (unchanged)
  → generate_questions       (unchanged)
  → review_questions         (unchanged)
  → END
```

D = deterministic (pure function), A = agent/LLM. 6 of 10 LO nodes are pure;
the 4 agent nodes are drift-suppressed (self-consistency, schema + verb enums,
fixed counts).

---

## 2. Module layout

New files under `backend/app/mcq_pipeline/`:

| File | Contents |
|---|---|
| `lo_state.py` | Unified `LOState` TypedDict (POC keys + run plumbing + question keys). |
| `lo_concept_graph.py` | Pure-Python ports: `slugify`, `ground_quote`, `syntax_grounded`, `recover_syntax`, `largest_remainder`, `_is_procedural`, `graph_check_concept`, `graph_find_prerequisites`, topo-sort / acyclicity. **No LLM.** |
| `lo_nodes.py` | The 10 node functions, each `(state) -> dict` partial-state updates. |
| `lo_artifact.py` | `finalize()` (sort, sha256 hash, provenance), `lo_to_legacy()` adapter. |
| `lo_prompts.py` | `register()` calls for the 4 agent prompts (extract / edge-vote / author / repair) — DB-overridable like existing prompts. |

Edited in place:
- `lo_schemas.py` — add `RawConcept`, `ConceptNode`, `DagEdge`, `AuthoredOutcome`,
  `ValidationReport`, `Artifact`; keep `LearningOutcome`/`LOBatch` for the bridge.
- `lo_graph.py` — rewire nodes + conditional repair edges + checkpointer compile.
- `runner.py` — pass `db_prereq_units` into state; compile with checkpointer
  (`thread_id = job_id`); add `artifact` to the returned result.
- `progress.py` — rewrite `STAGE_DEFS` to the 10 LO stages + 3 question stages.
- `config.py` — add the POC config constants.

Retired for LO creation (RagAdapter itself stays — question nodes still use it):
`extract_facts`, `agent_remember`, `agent_apply`, `_enforce_grounding`, the ReAct
`make_apply_tools` authoring path.

---

## 3. State — "memory state management"

One `TypedDict`, threaded by LangGraph with reducers (POC's single mutable dict
becomes immutable per-node returns so each step can be checkpointed):

```python
class LOState(TypedDict, total=False):
    # immutable run inputs
    session_id: str; title: str; source_text: str
    rag: Any; progress: Any                 # run plumbing — NOT checkpointed (see note)
    db_prereq_units: list                   # DB cross-course prereqs ("both" choice)
    # POC forward-only artifacts
    sections: list
    raw_concepts: list                      # POC "_raw_concepts"
    concept_inventory: list
    concept_graph: dict                     # {nodes, edges, _adj, assumed_prior, acyclic}
    allocation_plan: dict
    outcomes: list
    validation_report: dict
    retry_count: int                        # repair cap = MAX_RETRIES (2)
    overrides: Annotated[list, operator.add]
    log: Annotated[list, operator.add]
    artifact: dict; escalation: dict
    # bridge + question pipeline (unchanged keys)
    final_los: list                         # produced by lo_to_legacy
    generate_questions: bool; review_questions: bool
    questions: list; question_reviews: list
    notes: Annotated[list[str], operator.add]
```

**Checkpointer:** compile with `langgraph.checkpoint.postgres.PostgresSaver`
against the existing Postgres DB, `thread_id = str(job_id)`. Makes the repair
loop, escalation, concept DAG, allocation plan, and validation report durable,
inspectable, and resumable.

- New dependency: `langgraph-checkpoint-postgres`.
- Its tables are created by `checkpointer.setup()` (run once at startup or a
  one-off migration).
- `rag` / `progress` are live, non-picklable objects — inject them per-node from
  a run context (or a custom serde) rather than storing them in checkpointed
  state, or the saver will fail to serialize.

---

## 4. Conditional repair loop

LangGraph replaces the POC's `while True`:

```python
g.add_conditional_edges("validate", route_after_validate,
    {"repair": "repair", "finalize": "finalize"})
g.add_edge("repair", "resolve_prerequisites")   # repair → re-resolve → re-validate
```

`route_after_validate` → `"repair"` when failing rules exist and
`retry_count < MAX_RETRIES`; else `"finalize"` (sets `status="NEEDS_REVIEW"` +
`escalation` if rules still fail). Bump `recursion_limit` to cover 2 repair
cycles × node count. The pipeline never raises — failure is flagged in the
artifact.

---

## 5. Tools & prerequisites

- `graph_check_concept` / `graph_find_prerequisites` become **deterministic
  functions over `state["concept_graph"]`** (per the POC), not LLM/RAG calls.
  Used inside `resolve_prerequisites` and `validate`.
- `search_reading_material` stays bound to the run's `RagAdapter` (via
  `scope.set_adapter`) and remains available for optional evidence grounding;
  authoring grounds deterministically via `ground_quote`.
- **Prereq merge:** `resolve_prerequisites` computes the in-session ancestor
  closure (`prerequisites` = concept_ids, `prerequisite_scope` = verdict).
  `finalize` / `lo_to_legacy` then **also** attaches `state["db_prereq_units"]`
  (resolved in `runner.py:128`) so each legacy LO carries both. POC determinism
  preserved; DB grounding additive.

---

## 6. Schema bridge (`lo_to_legacy`)

New `AuthoredOutcome` → legacy `LearningOutcome` dict:

| legacy field | source |
|---|---|
| `outcome` | `id` |
| `bloom_category` / `bloom_level` | `apply→apply`; `remember_understand→understand` (documented lossy map) |
| `concept` | inventory lookup `canonical_name` by `concept_id` |
| `sub_concept` | `concept_id` |
| `description`, `learner_action`, `syntax`, `justification` | passthrough |
| `source_evidence` | `source_evidence["quote"]` |
| `prerequisites` | in-session prereq names **+** `db_prereq_units` |

Only place the two schemas touch — `recommend_for_los` / `generate_for_los` /
`review_and_fix_for_los` stay untouched.

---

## 7. Persistence & API

- `finalize` writes the artifact into `state["artifact"]` (**drop** the POC's
  `specs/*.json` disk write). `runner.run_mcq_pipeline` returns it; `services/
  jobs.py` already persists the whole result on `McqRun.result` (JSONB).
- Add `artifact` to the result dict: `spec_hash`, `source_fingerprint`, `status`,
  `effective_bloom_split`, `overrides`, `escalation`, `validation_report`.
  `lo_count` summary column = `len(outcomes)`.
- API surface (`/courses/mcq/generate`, `/runs/{id}`, `serialize_mcq_run`)
  unchanged; result payload is richer. Surface `status=NEEDS_REVIEW` to the UI.
- `progress.py:STAGE_DEFS` rewritten to the 10 LO stages + 3 question stages.
  UI renders dynamically from the snapshot, so no frontend contract break — but
  stage keys change.

---

## 8. Config constants (`config.py` / settings)

```
QUESTION_BUDGET = 20
DEFAULT_SPLIT   = {"remember_understand": 12, "apply": 8}
K_SAMPLES = 3 ; MAJORITY = 2 ; MAX_RETRIES = 2
TEMP_EXTRACT = 0.3 ; TEMP_GRAPH = 0.2 ; TEMP_AUTHOR = 0.2
ALLOW_SPLIT_OVERRIDE = True
RU_VERBS = {...} ; APPLY_VERBS = {...} ; VERBS = {"remember_understand": RU_VERBS, "apply": APPLY_VERBS}
SKILL_TYPES = {"conceptual", "practical_application", "diagnostic"}
ALIAS_MAP = {}   # per-domain synonyms
```

POC `chat()` → `ChatOpenAI` (already used) with structured output for K-sample
voting; `parse_json` ported as a fallback parser.

---

## 9. Phased implementation

1. **Pure core, no graph** — `lo_concept_graph.py` + `lo_schemas.py` models +
   unit tests on the For-Loop sample (`data/11_*`) → reproduce
   `specs/11_for_loop_reading_material_v1.0.0.json` (hash match). No DB/LLM.
   *Highest-confidence first.*
2. **Agent nodes** — `lo_nodes.py` extract/graph/author/repair with K-sample
   voting via `ChatOpenAI`; prompts into `prompt_store`.
3. **Graph wiring** — `lo_graph.py` rewire + conditional repair edges +
   `lo_to_legacy` + question nodes; run without checkpointer first.
4. **Checkpointer** — add `PostgresSaver`, `thread_id=job_id`, non-serializable
   `rag`/`progress` handling, `setup()` at startup.
5. **Runner / progress / persistence** — `db_prereq_units` into state,
   `STAGE_DEFS`, artifact into `McqRun.result`.
6. **End-to-end** via `/courses/mcq/generate` on a real ingested course; verify
   questions still generate.

---

## 10. Open risks

- **`bloom_category` lossy map** (`remember_understand`→`understand`) — confirm
  the question-type agent doesn't need the `remember` / `understand` distinction.
- **Checkpointer + live objects** — `rag` / `progress` can't be pickled; needs
  the inject-per-node pattern or custom serde.
- **POC RAG layer differs** — POC builds an in-memory `VectorStore` from one RM;
  backend `RagAdapter` is pgvector + course scope. New D-node tools are
  graph-backed (sidestep RAG), so this only affects optional
  `search_reading_material` and the unchanged question nodes. POC `bind_rm()` is
  **not** ported.

---

## 11. Security note (out of band)

`backend/.env:16` contains a live OpenRouter API key in plaintext. Rotate it and
confirm `.env` is gitignored, independent of this work.
