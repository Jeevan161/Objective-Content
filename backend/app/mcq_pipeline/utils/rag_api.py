"""
app/mcq_pipeline/rag_api.py
---------------------------
Shim replacing the Workflow's `rag_api`. Same function names/signatures the
vendored agents call, but every call resolves the run-scoped `RagAdapter` from
`scope` (a ContextVar), so retrieval is always metadata-scoped to the run's
courses. No global, multi-course-leaking state.
"""

from __future__ import annotations

from app.mcq_pipeline.utils import scope


def _memoized(adapter, bucket: str, key, compute):
    """Per-run cache for idempotent RAG probes. Result is cached on the run-scoped
    `adapter` (one per run, dropped when the run ends), keyed by (bucket, key). The same
    term recurs across many LOs/questions, so this removes duplicate backend calls without
    changing results. Falls back to `compute()` when the adapter carries no cache (tests).
    Note: callers still record the rag_call each time, so per-question attribution is
    unchanged — only the expensive backend call is skipped on a cache hit."""
    cache = getattr(adapter, "_rag_memo", None)
    if cache is None:
        return compute()
    lock = adapter._rag_memo_lock
    ck = (bucket, key)
    with lock:
        if ck in cache:
            return cache[ck]
    res = compute()                      # compute OUTSIDE the lock so RAG calls aren't serialized
    with lock:
        return cache.setdefault(ck, res)


def search_reading_material(query: str, *, top_k: int = 6) -> list[dict]:
    res = scope.get_adapter().search(query, top_k=top_k)
    scope.record_rag_call({
        "tool": "search_reading_material",
        "args": {"query": query, "top_k": top_k},
        "result": {"hits": len(res), "sections": [r.get("section") for r in res[:top_k]]},
    })
    return res


def check_concept(topic: str, syntax: str | None = None) -> dict:
    adapter = scope.get_adapter()
    res = _memoized(adapter, "check_concept", (topic, syntax),
                    lambda: adapter.check_concept(topic, syntax))
    scope.record_rag_call({
        "tool": "check_concept",
        "args": {"topic": topic, "syntax": syntax},
        "result": {"verdict": res.get("verdict"), "sources": res.get("sources", [])},
    })
    return res


def find_prerequisites(topic: str, *, top_k: int = 6) -> dict:
    res = scope.get_adapter().find_prerequisites(topic, top_k=top_k)
    scope.record_rag_call({
        "tool": "find_prerequisites",
        "args": {"topic": topic, "top_k": top_k},
        "result": {"prerequisites": res.get("prerequisites", []),
                   "in_session": res.get("in_session", [])},
    })
    return res


def code_coverage(concept: str, syntax: str | None = None, *, max_seq=None) -> dict:
    adapter = scope.get_adapter()
    res = _memoized(adapter, "code_coverage", (concept, syntax, max_seq),
                    lambda: adapter.code_coverage(concept, syntax, max_seq=max_seq))
    scope.record_rag_call({
        "tool": "code_coverage",
        "args": {"concept": concept, "syntax": syntax},
        "result": {"covered": res.get("covered"), "verdict": res.get("verdict")},
    })
    return res


def anchor_seq_for(query: str) -> int:
    """Unused in the pgvector world (scope is by course, not seq); kept for parity."""
    return 0


def list_prior_units(anchor_seq: int) -> list[dict]:
    return scope.get_adapter().prior_units()
