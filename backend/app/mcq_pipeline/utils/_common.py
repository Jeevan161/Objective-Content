"""
app/mcq_pipeline/lo_nodes/_common.py
------------------------------------
Shared run-context helpers. Each node pulls its live RAG/progress objects from the run's
RunContext (see lo_state) via these, so nothing run-scoped is module-global and pure nodes stay
unit-testable (they tolerate a missing ctx).
"""
from __future__ import annotations

from app.mcq_pipeline.utils import scope
from app.mcq_pipeline.state import run_ctx


# --- run-context helpers (defensive so pure nodes are unit-testable) ------- #
class _NoProgress:
    def start(self, *a, **k): pass
    def done(self, *a, **k): pass
    def tick(self, *a, **k): pass
    def detail(self, *a, **k): pass
    def error(self, *a, **k): pass
    def counter(self, key, total):
        def _on(**k): pass
        return _on


_NOOP = _NoProgress()


def _ctx(config):
    try:
        return run_ctx(config)
    except Exception:  # noqa: BLE001 — pure nodes may run without a registered ctx
        return None


def _prog(config):
    c = _ctx(config)
    return c.progress if c is not None else _NOOP


def _bind_rag(config) -> None:
    c = _ctx(config)
    if c is not None and c.rag is not None:
        scope.set_adapter(c.rag)
