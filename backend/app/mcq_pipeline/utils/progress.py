"""
app/mcq_pipeline/progress.py
----------------------------
Structured live progress for the pipeline. Holds the fixed stage board the UI
renders immediately; graph nodes call `start`/`done`/`tick`/`error` as they run
(safe to call from worker threads — guarded by a lock). Every change is pushed to
a caller-supplied sink (the job runner writes it onto `SyncJob.progress`).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable


class JobCancelled(BaseException):
    """Raised cooperatively when the user cancels a running MCQ job. Subclasses
    BaseException (not Exception) on purpose so the pipeline's broad `except Exception`
    handlers don't swallow it — it propagates up through the graph to the job runner,
    which finalizes the job as CANCELLED."""

# The pipeline's stages, in order. `parallel_group` marks branches the UI renders
# side by side; per-LO stages carry done/total.
STAGE_DEFS = [
    # LO-creation stage — the LO-first pipeline.
    {"key": "parse_structure", "label": "Parse structure"},
    {"key": "author_outcomes", "label": "Author candidate outcomes"},
    {"key": "consolidate_concepts", "label": "Consolidate concepts + taught depth"},
    {"key": "graph_outcomes", "label": "Build outcome graph (weights)"},
    {"key": "select_outcomes", "label": "Select outcomes (budget + feasibility)"},
    {"key": "resolve_prerequisites", "label": "Resolve prerequisites (apply)"},
    {"key": "review_and_validate", "label": "Review & validate (dedup, R1–R8 rubric, structural gate)"},
    {"key": "repair", "label": "Repair (regenerate, if needed)"},
    {"key": "finalize", "label": "Finalize & freeze"},
    {"key": "lo_to_legacy", "label": "Bridge to questions"},
    {"key": "sequence_outcomes", "label": "Sequence outcomes (deep-dive order)"},
    {"key": "recommend_question_types", "label": "Recommend question types"},
    {"key": "review_outcomes", "label": "Review outcomes (human gate)"},
    # Question stage — unchanged.
    {"key": "generate_questions", "label": "Generate questions"},
    {"key": "review_questions", "label": "Review & fix"},
]


# Cap LLM calls recorded per node span so a fan-out node (e.g. K-sample voting over many
# concepts) can't bloat the trace row. The detail string still reports totals.
_LLM_CALLS_CAP = 60


def _start_llm_recording() -> tuple:
    """Begin per-node LLM-I/O recording. Best-effort: returns (None, None) if scope is unavailable
    so progress can never crash a run."""
    try:
        from app.mcq_pipeline.utils import scope
        return scope.start_llm_recording()
    except Exception:  # noqa: BLE001
        return None, None


def _stop_llm_recording(token) -> None:
    if token is None:
        return
    try:
        from app.mcq_pipeline.utils import scope
        scope.stop_llm_recording(token)
    except Exception:  # noqa: BLE001
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProgressReporter:
    def __init__(self, sink: Callable[[dict], None] | None = None,
                 trace_sink: Callable[[dict], None] | None = None,
                 seed_done: list[str] | None = None,
                 cancel_check: Callable[[], bool] | None = None):
        self._lock = threading.Lock()
        self._sink = sink
        self._trace_sink = trace_sink          # emits one span per node entry (our own trace)
        # Cooperative cancellation: consulted on every stage transition (nodes call these
        # frequently). When it returns True we raise JobCancelled, which unwinds the graph.
        self._cancel_check = cancel_check
        self._open: dict[str, tuple] = {}       # node key -> (started_dt, started_perf)
        # seed_done: stages already completed before this reporter took over (a HITL RESUME builds a
        # fresh reporter; without seeding, every prior stage would reset to 'pending' on the board).
        # Seeded stages open no span (their trace spans were already emitted on the original run).
        _seeded = set(seed_done or ())
        self._stages: dict[str, dict] = {
            d["key"]: {**d, "state": "done" if d["key"] in _seeded else "pending"} for d in STAGE_DEFS
        }

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "stages": [dict(self._stages[d["key"]]) for d in STAGE_DEFS],
                "updated_at": _now_iso(),
            }

    def _flush(self) -> None:
        if self._sink:
            try:
                self._sink(self.snapshot())
            except Exception:  # noqa: BLE001 — progress must never crash the run
                pass

    def _set(self, key: str, **fields) -> None:
        # Cooperative cancel point — checked before any work, outside the swallowing
        # _flush, so JobCancelled actually propagates out of the running node.
        if self._cancel_check is not None and self._cancel_check():
            raise JobCancelled()
        span = None
        with self._lock:
            stage = self._stages.get(key)
            if stage is not None:
                stage.update(fields)
                new_state = fields.get("state")
                now = datetime.now(timezone.utc)
                # First transition into "running" opens a span (covers start()/counter()/tick())
                # and begins recording this node's LLM I/O; the matching "done"/"error" closes the
                # span, attaches the recorded LLM calls to its snapshot, and emits it. A node
                # re-entered by the repair loop / resume re-opens, so each entry is its own span.
                if new_state == "running" and key not in self._open:
                    calls, token = _start_llm_recording()
                    self._open[key] = (now, perf_counter(), calls, token)
                elif new_state in ("done", "error"):
                    started_dt, started_perf, calls, token = self._open.pop(
                        key, (now, perf_counter(), None, None))
                    _stop_llm_recording(token)
                    snapshot = dict(stage.get("snapshot") or {})
                    if calls:
                        snapshot["llm_calls"] = calls[:_LLM_CALLS_CAP]
                        if len(calls) > _LLM_CALLS_CAP:
                            snapshot["llm_calls_truncated"] = len(calls) - _LLM_CALLS_CAP
                    span = {
                        "node": key, "label": stage.get("label", ""),
                        "status": "error" if new_state == "error" else "ok",
                        "detail": stage.get("detail", "") or "",
                        "snapshot": snapshot,
                        "started_at": started_dt, "ended_at": now,
                        "duration_ms": int(max(0.0, perf_counter() - started_perf) * 1000),
                    }
        self._flush()
        if span is not None and self._trace_sink:
            try:
                self._trace_sink(span)
            except Exception:  # noqa: BLE001 — tracing must never crash the run
                pass

    def start(self, key: str, detail: str | None = None) -> None:
        self._set(key, state="running", **({"detail": detail} if detail else {}))

    def detail(self, key: str, detail: str) -> None:
        self._set(key, detail=detail)

    def tick(self, key: str, done: int, total: int, **extra) -> None:
        self._set(key, state="running", done=done, total=total, **extra)

    def done(self, key: str, **extra) -> None:
        self._set(key, state="done", **extra)

    def error(self, key: str, message: str) -> None:
        self._set(key, state="error", detail=message)

    def counter(self, key: str, total: int):
        """Return a thread-safe `on_done()` callback that increments this stage's
        done/total as each of `total` concurrent items finishes."""
        self._set(key, state="running", done=0, total=total)
        state = {"done": 0}
        lock = threading.Lock()

        def on_done(**extra) -> None:
            with lock:
                state["done"] += 1
                done = state["done"]
            self.tick(key, done, total, **extra)

        return on_done
