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


def search_reading_material(query: str, *, top_k: int = 6) -> list[dict]:
    res = scope.get_adapter().search(query, top_k=top_k)
    scope.record_rag_call({
        "tool": "search_reading_material",
        "args": {"query": query, "top_k": top_k},
        "result": {"hits": len(res), "sections": [r.get("section") for r in res[:top_k]]},
    })
    return res


def check_concept(topic: str, syntax: str | None = None) -> dict:
    res = scope.get_adapter().check_concept(topic, syntax)
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
    res = scope.get_adapter().code_coverage(concept, syntax, max_seq=max_seq)
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
