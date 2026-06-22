"""
app/mcq_pipeline/tracing.py
---------------------------
LangSmith wiring. Tracing is a no-op unless LANGSMITH is configured in settings.
`setup_langsmith()` exports the env vars LangChain reads; `capture_trace()` wraps
a run and, after it finishes, exposes the root run id + a best-effort trace URL.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

from app.core.config import settings


def tracing_enabled() -> bool:
    return bool(settings.langchain_tracing_v2 and settings.langchain_api_key)


def setup_langsmith() -> bool:
    """Export LangChain tracing env vars from settings. Safe to call repeatedly."""
    if not tracing_enabled():
        return False
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key or ""
    os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project
    os.environ["LANGCHAIN_ENDPOINT"] = settings.langchain_endpoint
    return True


def _run_url(run_id: str) -> str:
    if not run_id:
        return ""
    try:
        from langsmith import Client

        return Client().get_run_url(run_id=run_id)  # type: ignore[call-arg]
    except Exception:  # noqa: BLE001 — best-effort URL
        return f"https://smith.langchain.com/o/-/projects/p/{settings.langchain_project}/r/{run_id}"


@contextmanager
def capture_trace():
    """Yield a dict populated (after the block) with {'run_id', 'url'} when tracing
    is on; a no-op otherwise."""
    holder = {"run_id": "", "url": ""}
    if not tracing_enabled():
        yield holder
        return
    try:
        from langchain_core.tracers.context import collect_runs
    except Exception:  # noqa: BLE001
        yield holder
        return
    with collect_runs() as cb:
        yield holder
    try:
        runs = list(getattr(cb, "traced_runs", []) or [])
        if runs:
            run_id = str(getattr(runs[0], "id", "") or "")
            holder["run_id"] = run_id
            holder["url"] = _run_url(run_id)
    except Exception:  # noqa: BLE001
        pass
