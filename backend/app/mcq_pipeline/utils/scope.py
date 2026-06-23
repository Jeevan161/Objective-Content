"""
app/mcq_pipeline/scope.py
-------------------------
Run-scoped RAG binding. The vendored agent modules call the module-level
`rag_api` functions with no scope argument, so we stash the run's bound
``RagAdapter`` in a ContextVar and let `rag_api` read it.

ContextVars are per-thread, so each graph node sets it from the graph state at
the top of the node (surviving LangGraph's internal threading), and `pmap`
re-binds it inside its worker threads via :func:`with_current_adapter`.
"""

from __future__ import annotations

import contextvars
from typing import Any, Callable, TypeVar

_adapter_var: contextvars.ContextVar = contextvars.ContextVar("mcq_rag_adapter", default=None)
# Optional per-thread sink that collects every RAG call made through `rag_api`,
# so each outcome/question can report exactly which RAG calls grounded it.
_rag_calls_var: contextvars.ContextVar = contextvars.ContextVar("mcq_rag_calls", default=None)

T = TypeVar("T")
R = TypeVar("R")


def set_adapter(adapter: Any) -> None:
    """Bind the RAG adapter for the current thread/run."""
    _adapter_var.set(adapter)


def get_adapter() -> Any:
    adapter = _adapter_var.get()
    if adapter is None:
        raise RuntimeError("No RAG adapter bound for this MCQ run (scope.set_adapter not called).")
    return adapter


def record_rag_call(entry: dict) -> None:
    """Append one RAG call to the active recorder, if recording is in effect for
    this thread. A no-op otherwise, so `rag_api` can always call it safely."""
    sink = _rag_calls_var.get()
    if sink is not None:
        sink.append(entry)


class recording:
    """Context manager that collects every RAG call made on this thread while
    active. Yields the list the calls accumulate into::

        with scope.recording() as rag_calls:
            ...do work that hits rag_api...
        result["rag_calls"] = rag_calls
    """

    def __enter__(self) -> list:
        self._calls: list = []
        self._token = _rag_calls_var.set(self._calls)
        return self._calls

    def __exit__(self, *exc) -> None:
        _rag_calls_var.reset(self._token)


def with_current_adapter(fn: Callable[[T], R]) -> Callable[[T], R]:
    """Wrap ``fn`` so it runs with the CURRENT thread's adapter re-bound — used by
    `pmap` so worker threads inherit the scope captured at fan-out time."""
    current = _adapter_var.get()

    def wrapped(x: T) -> R:
        token = _adapter_var.set(current)
        try:
            return fn(x)
        finally:
            _adapter_var.reset(token)

    return wrapped
