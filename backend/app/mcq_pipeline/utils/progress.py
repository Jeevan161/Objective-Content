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

# The pipeline's stages, in order. `parallel_group` marks branches the UI renders
# side by side; per-LO stages carry done/total.
STAGE_DEFS = [
    # LO-creation stage — the deterministic 10-node pipeline.
    {"key": "parse_structure", "label": "Parse structure"},
    {"key": "extract_concepts", "label": "Extract concepts (self-consistency)"},
    {"key": "canonicalize_concepts", "label": "Canonicalize concepts"},
    {"key": "build_dependency_graph", "label": "Build dependency graph"},
    {"key": "profile_coverage", "label": "Profile coverage (breadth & depth)"},
    {"key": "plan_allocation", "label": "Plan division (feasibility + budget)"},
    {"key": "review_division", "label": "Review division (human gate 1)"},
    {"key": "author_outcomes", "label": "Author outcomes"},
    {"key": "resolve_prerequisites", "label": "Resolve prerequisites"},
    {"key": "judge_outcomes", "label": "Judge outcomes (R1–R8 rubric)"},
    {"key": "validate", "label": "Validate (structural + rubric gate)"},
    {"key": "repair", "label": "Repair (regenerate, if needed)"},
    {"key": "review_outcomes", "label": "Review outcomes (human gate 2)"},
    {"key": "finalize", "label": "Finalize & freeze"},
    {"key": "lo_to_legacy", "label": "Bridge to questions"},
    {"key": "sequence_outcomes", "label": "Sequence outcomes (deep-dive order)"},
    # Question stage — unchanged.
    {"key": "recommend_question_types", "label": "Pick question types"},
    {"key": "generate_questions", "label": "Generate questions"},
    {"key": "review_questions", "label": "Review & fix"},
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProgressReporter:
    def __init__(self, sink: Callable[[dict], None] | None = None,
                 trace_sink: Callable[[dict], None] | None = None):
        self._lock = threading.Lock()
        self._sink = sink
        self._trace_sink = trace_sink          # emits one span per node entry (our own trace)
        self._open: dict[str, tuple] = {}       # node key -> (started_dt, started_perf)
        self._stages: dict[str, dict] = {
            d["key"]: {**d, "state": "pending"} for d in STAGE_DEFS
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
        span = None
        with self._lock:
            stage = self._stages.get(key)
            if stage is not None:
                stage.update(fields)
                new_state = fields.get("state")
                now = datetime.now(timezone.utc)
                # First transition into "running" opens a span (covers start()/counter()/tick());
                # the matching "done"/"error" closes it and emits the span. A node re-entered by
                # the repair loop / resume re-opens, so each entry is its own span.
                if new_state == "running" and key not in self._open:
                    self._open[key] = (now, perf_counter())
                elif new_state in ("done", "error"):
                    started_dt, started_perf = self._open.pop(key, (now, perf_counter()))
                    span = {
                        "node": key, "label": stage.get("label", ""),
                        "status": "error" if new_state == "error" else "ok",
                        "detail": stage.get("detail", "") or "",
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
