"""
concurrency.py (vendored, adapted)
----------------------------------
Fan-out helper for the per-LO LLM stages. ADAPTED from the Workflow original to
re-bind the run's RAG scope inside each worker thread (`scope.with_current_adapter`)
so the vendored `rag_api` shim resolves the right scoped adapter off the main thread.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, List, TypeVar

from app.mcq_pipeline.utils.scope import with_current_adapter

T = TypeVar("T")
R = TypeVar("R")

DEFAULT_WORKERS = 8


def pmap(fn: Callable[[T], R], items: Iterable[T], *, workers: int = DEFAULT_WORKERS) -> List[R]:
    """Map `fn` over `items` concurrently, preserving input order. Runs inline for
    0/1 items. Worker threads inherit the caller's bound RAG adapter."""
    items = list(items)
    if len(items) <= 1:
        return [fn(x) for x in items]
    bound = with_current_adapter(fn)
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(bound, items))
