"""
app/mcq_pipeline/lo_state.py
----------------------------
The LangGraph state for the LO pipeline + the run-context registry.

State management note (production constraint):
  A checkpointer serializes EVERY channel value. The run's live objects — the
  `RagAdapter` (holds DB sessions) and the `ProgressReporter` (holds a lock) — are
  not JSON/pickle-safe and must NOT live in checkpointed state. So `LOState` holds
  ONLY plain data, and the live objects ride in a `RunContext` kept in a process-
  local registry keyed by the run's `thread_id` (= the job id). Nodes pull them via
  `run_ctx(config)`. The registry is cleared in a `finally` by the runner.

  Consequence: concurrent runs are isolated — each has its own thread_id, its own
  RunContext, and its own checkpoint thread. Nothing run-scoped is global mutable.
"""

from __future__ import annotations

import operator
import threading
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict


class LOState(TypedDict, total=False):
    # ---- immutable run inputs (plain data) -------------------------------- #
    session_id: str
    title: str
    source_text: str

    # ---- derived session focus ("motive") — keeps LOs/questions on-topic --- #
    # {objective: <one paragraph>, central_concepts: [...], incidental: [...]} derived once from
    # title + source_text by the derive_session_focus node; session_objective is the flat string
    # threaded into authoring / consolidation / validation / generation / review prompts.
    session_focus: dict
    session_objective: str

    # ---- POC forward-only artifacts --------------------------------------- #
    sections: list
    raw_concepts: list                     # POC "_raw_concepts"
    # REPLACE channel threaded through canonicalize -> build_dependency_graph ->
    # profile_coverage -> plan_allocation. CONTRACT: each of those nodes MUST return the
    # COMPLETE inventory (copy + augment, never reorder/drop/partial-return) — a partial
    # return would silently clobber a predecessor's fields (procedural, depth_category,
    # in_scope, apply_suitable) under replace semantics.
    concept_inventory: list
    concept_graph: dict                    # {nodes, edges, _adj, assumed_prior, acyclic}
    outcome_graph: dict                    # LO-level graph {nodes, edges, weights} (build_outcome_graph)
    coverage_profile: dict                 # breadth x depth manifest (profile_depth node)
    allocation_plan: dict
    division_proposal: dict                # Planner output, shown to the human at HITL Gate 1
    # v1 LO-first planning: the enumerate→plan stage records WHAT it generated and which
    # outcomes it pre-selected toward the (soft) default budget, for the freeze gate + portal.
    selection_summary: dict
    frozen_selected_ids: list              # outcome ids the human froze at the LO review gate
    lo_feedback_log: Annotated[list, operator.add]   # per-LO human feedback (persisted as McqQuestionFeedback stage='lo')
    outcomes: list
    backfill_pool: list                    # ranked UNSELECTED candidates (plan_outcomes) — used to
                                           # top the set back up to budget after dedup / R1-drop
    last_regenerated_ids: list             # LO ids regenerated in the last human-gate round, so the
                                           # gate UI can split 'regenerated' from 'previously approved'

    # ---- validation / repair loop ----------------------------------------- #
    validation_report: dict
    lo_reviews: dict                       # outcome_id -> unified-judge rubric verdict (R1–R8)
    gate_decision: dict                    # last HITL gate decision (Gate 1 division / Gate 2 outcomes)
    retry_count: int
    overrides: Annotated[list, operator.add]
    escalation: dict

    # ---- terminal artifact ------------------------------------------------ #
    artifact: dict

    # ---- bridge into the (unchanged) question pipeline -------------------- #
    final_los: list
    questions: list
    question_reviews: list

    # ---- audit trail ------------------------------------------------------ #
    log: Annotated[list, operator.add]
    notes: Annotated[list[str], operator.add]


@dataclass
class RunContext:
    """The run's live, non-serializable objects + run-level flags. Kept OUT of
    checkpointed state; looked up per node by thread_id."""
    rag: Any
    progress: Any
    db_prereq_units: list = field(default_factory=list)
    review_questions: bool = True
    run_coverage_gate: bool = True         # run the unified LLM Judge (R1–R8) over LOs (V13)
    question_budget: Any = None            # user-supplied budget (default ceiling = QUESTION_BUDGET)
    hitl_enabled: bool = False             # pause at review gates (Gate 1 / 2); off for tests/headless
    # Classroom Quiz: clamp the final LO set to this ceiling (with floor 4 / hard-floor 3). None =
    # the standard MCQ budget logic. Read by select_outcomes (m02/m06) only when set.
    lo_budget: Any = None


class _Registry:
    """Thread-safe process-local registry of in-flight RunContexts, keyed by the
    run's thread_id. Concurrent runs never collide (unique thread_id per job)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ctxs: dict[str, RunContext] = {}

    def register(self, thread_id: str, ctx: RunContext) -> None:
        with self._lock:
            self._ctxs[thread_id] = ctx

    def get(self, thread_id: str) -> RunContext:
        with self._lock:
            ctx = self._ctxs.get(thread_id)
        if ctx is None:
            raise RuntimeError(f"No RunContext registered for thread_id={thread_id!r}.")
        return ctx

    def pop(self, thread_id: str) -> None:
        with self._lock:
            self._ctxs.pop(thread_id, None)


REGISTRY = _Registry()


def run_ctx(config: dict) -> RunContext:
    """Fetch the current run's RunContext from a LangGraph node's `config`."""
    thread_id = (config or {}).get("configurable", {}).get("thread_id")
    if not thread_id:
        raise RuntimeError("thread_id missing from config — cannot resolve RunContext.")
    return REGISTRY.get(thread_id)


def new_state(session_id: str, title: str, source_text: str) -> LOState:
    """Initial pipeline state (plain data only)."""
    return {
        "session_id": session_id,
        "title": title,
        "source_text": source_text,
        "sections": [],
        "raw_concepts": [],
        "concept_inventory": [],
        "concept_graph": {"nodes": [], "edges": [], "_adj": {},
                          "assumed_prior": [], "acyclic": True},
        "outcome_graph": {"nodes": [], "edges": [], "weights": {}},
        "allocation_plan": {},
        "selection_summary": {},
        "frozen_selected_ids": [],
        "lo_feedback_log": [],
        "outcomes": [],
        "validation_report": {},
        "lo_reviews": {},
        "retry_count": 0,
        "overrides": [],
        "escalation": None,
        "log": [],
        "notes": [],
    }
