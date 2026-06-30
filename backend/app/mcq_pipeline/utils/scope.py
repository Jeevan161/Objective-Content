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
# Optional per-thread sink that collects every LLM call made through `utils.llm.chat`
# (prompt messages + response), so each pipeline node's trace span can show its LLM I/O.
_llm_calls_var: contextvars.ContextVar = contextvars.ContextVar("mcq_llm_calls", default=None)
# Optional per-thread sink that collects token USAGE for every LLM call (from the usage
# callback in utils.llm), so each node span — and the whole run — can report token cost.
_llm_usage_var: contextvars.ContextVar = contextvars.ContextVar("mcq_llm_usage", default=None)
# The triggering user's personal LLM API key for this run. Bound at job start and
# re-bound into pmap workers so EVERY LLM call uses that user's key (all other
# provider settings stay global). None → fall back to the global provider key.
_user_api_key_var: contextvars.ContextVar = contextvars.ContextVar("mcq_user_api_key", default=None)


def set_user_api_key(key: str | None) -> None:
    """Bind the current run's per-user LLM API key (None clears it)."""
    _user_api_key_var.set(key or None)


def get_user_api_key():
    """The per-user LLM API key bound for this thread/run, or None."""
    return _user_api_key_var.get()

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


def record_llm_call(entry: dict) -> None:
    """Append one LLM call (prompt + response) to the active recorder, if recording is in
    effect for this thread. A no-op otherwise, so `utils.llm.chat` can always call it safely.
    `list.append` is atomic under the GIL, so it is safe to share one list across pmap workers."""
    sink = _llm_calls_var.get()
    if sink is not None:
        sink.append(entry)


def start_llm_recording() -> tuple:
    """Begin collecting LLM calls on this thread. Returns (calls_list, token); pass the token to
    :func:`stop_llm_recording`. Used by the ProgressReporter to scope LLM I/O to one node span."""
    calls: list = []
    token = _llm_calls_var.set(calls)
    return calls, token


def stop_llm_recording(token) -> None:
    _llm_calls_var.reset(token)


def record_llm_usage(entry: dict) -> None:
    """Append one call's token usage to the active usage recorder, if recording is in effect for
    this thread. A no-op otherwise, so the usage callback can always call it safely. `list.append`
    is atomic under the GIL, so one list is safe to share across pmap workers."""
    sink = _llm_usage_var.get()
    if sink is not None:
        sink.append(entry)


def start_usage_recording() -> tuple:
    """Begin collecting token usage on this thread. Returns (usage_list, token); pass the token to
    :func:`stop_usage_recording`. Used by the ProgressReporter to scope token cost to one node span."""
    usage: list = []
    token = _llm_usage_var.set(usage)
    return usage, token


def stop_usage_recording(token) -> None:
    _llm_usage_var.reset(token)


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
    """Wrap ``fn`` so it runs with the CURRENT thread's run scope re-bound — used by `pmap` so
    worker threads inherit the adapter AND the active RAG/LLM recorders captured at fan-out time
    (so LLM/RAG calls made inside a parallel node are still attributed to that node's trace span)."""
    adapter = _adapter_var.get()
    rag_calls = _rag_calls_var.get()
    llm_calls = _llm_calls_var.get()
    llm_usage = _llm_usage_var.get()
    user_api_key = _user_api_key_var.get()

    def wrapped(x: T) -> R:
        ta = _adapter_var.set(adapter)
        tr = _rag_calls_var.set(rag_calls)
        tl = _llm_calls_var.set(llm_calls)
        tu = _llm_usage_var.set(llm_usage)
        tk = _user_api_key_var.set(user_api_key)
        try:
            return fn(x)
        finally:
            _adapter_var.reset(ta)
            _rag_calls_var.reset(tr)
            _llm_calls_var.reset(tl)
            _llm_usage_var.reset(tu)
            _user_api_key_var.reset(tk)

    return wrapped
