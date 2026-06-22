"""
app/services/rag_search.py
--------------------------
Scoped retrieval over the RAG store + the topic-search "judge" (ported from the
Workflow POC ``topic_search.py``).

  search(course_ids, query, …)         cosine top-k (pgvector) + MMR re-rank
  check_concept(course_ids, topic, …)  rewrite -> embed -> search -> expand -> judge

Every query REQUIRES course_ids and filters in SQL — there is no unscoped path,
so retrieval can never leak across courses the caller didn't ask for.
"""

from __future__ import annotations

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Course, RagChunk, Topic, Unit, UnitPart
from app.services.openrouter import chat, embed_query

_SNIPPET = 400      # snippet length in search results
_JUDGE_SNIPPET = 1800  # per-chunk cap when feeding the judge
_ANSWER_SNIPPET = 1800  # per-chunk cap when feeding the chat answerer
_MMR_LAMBDA = 0.5


# --------------------------------------------------------------------------- #
# Vector search + MMR
# --------------------------------------------------------------------------- #
def _candidate_rows(
    session: Session, qvec: list[float], course_ids: list[str],
    topic_ids: list[str] | None, unit_ids: list[str] | None, pool: int,
):
    """Cosine-nearest RagChunks within scope, with their owning names. Filters use
    portal-facing ids (Topic.topic_id, UnitPart.unit_id) resolved to internal ids."""
    stmt = (
        select(
            RagChunk,
            Course.course_name,
            Topic.topic_name,
            Unit.label,
            UnitPart.name,
            UnitPart.unit_id,
        )
        .join(Course, RagChunk.course_id == Course.course_id)
        .join(Topic, RagChunk.topic_id == Topic.id)
        .join(Unit, RagChunk.unit_id == Unit.id)
        .join(UnitPart, RagChunk.unit_part_id == UnitPart.id)
        .where(RagChunk.course_id.in_(course_ids))
    )
    if topic_ids:
        topic_pks = select(Topic.id).where(
            Topic.topic_id.in_(topic_ids), Topic.course_id.in_(course_ids)
        )
        stmt = stmt.where(RagChunk.topic_id.in_(topic_pks))
    if unit_ids:
        part_pks = select(UnitPart.id).where(UnitPart.unit_id.in_(unit_ids))
        stmt = stmt.where(RagChunk.unit_part_id.in_(part_pks))

    stmt = stmt.order_by(RagChunk.embedding.cosine_distance(qvec)).limit(pool)
    return session.execute(stmt).all()


def _mmr(qvec: np.ndarray, vecs: list[np.ndarray], top_k: int, lam: float) -> list[int]:
    """Maximal Marginal Relevance: relevant to the query but diverse from each other."""
    selected: list[int] = []
    candidates = list(range(len(vecs)))
    while candidates and len(selected) < top_k:
        best_i, best_score = None, -1e9
        for i in candidates:
            relevance = float(vecs[i] @ qvec)
            diversity = max((float(vecs[i] @ vecs[j]) for j in selected), default=0.0)
            score = lam * relevance - (1 - lam) * diversity
            if score > best_score:
                best_i, best_score = i, score
        selected.append(best_i)
        candidates.remove(best_i)
    return selected


def _citation(course_name: str, unit_label: str, part_name: str, section: str | None) -> str:
    name = unit_label or part_name or "?"
    where = f"{course_name} > {name}"
    return f"{where} > {section}" if section else where


def search(
    session: Session, *, course_ids: list[str], query: str,
    topic_ids: list[str] | None = None, unit_ids: list[str] | None = None,
    top_k: int = 10, use_mmr: bool = True,
) -> list[dict]:
    """Semantic search scoped to course_ids (+ optional topic/unit). Returns the
    best-matching sections with a short snippet and a cosine score."""
    qvec = embed_query(query)
    rows = _candidate_rows(
        session, qvec, course_ids, topic_ids, unit_ids, pool=max(top_k * 4, top_k)
    )
    if not rows:
        return []

    qarr = np.asarray(qvec, dtype=np.float32)
    vecs = [np.asarray(r[0].embedding, dtype=np.float32) for r in rows]
    order = _mmr(qarr, vecs, top_k, _MMR_LAMBDA) if use_mmr else list(range(min(top_k, len(rows))))

    out = []
    for i in order:
        chunk, course_name, topic_name, unit_label, part_name, part_unit_id = rows[i]
        text = (chunk.text or "").strip()
        out.append({
            "course_id": chunk.course_id,
            "course_name": course_name,
            "topic_name": topic_name,
            "unit_label": unit_label,
            "part_name": part_name,
            "part_unit_id": part_unit_id,
            "section": chunk.section,
            "score": float(vecs[i] @ qarr),
            "snippet": text if len(text) <= _SNIPPET else text[:_SNIPPET] + " …",
        })
    return out


# --------------------------------------------------------------------------- #
# Conversational answer (RAG chat) — retrieve scoped sections, then synthesize
# --------------------------------------------------------------------------- #
def answer(
    session: Session, *, course_ids: list[str], query: str, top_k: int = 15,
) -> dict:
    """Answer a free-form question using ONLY the scoped course materials.

    Retrieves the most relevant sections across `course_ids` (the chat UI passes a
    course plus its prerequisites), then asks the chat model to answer from those
    sections with citations. Returns the answer text and the sources it drew from.

    `top_k` is deliberately generous: "list all X" (enumeration) questions need
    high recall because the items are scattered across many sections (e.g. a string
    method documented under "Lists and Strings > Joining" rather than the dedicated
    "String Methods" section). MMR then keeps the surfaced sections diverse so the
    breadth isn't wasted on near-duplicate chunks.
    """
    query = (query or "").strip()
    if not query:
        return {"query": query, "course_ids": course_ids,
                "answer": "Ask a question to search the course materials.", "sources": []}

    qvec = embed_query(query)
    # Pull a wide candidate pool so low-but-relevant sections (the ones a single
    # narrow query would drop) are still in contention before MMR narrows to top_k.
    rows = _candidate_rows(
        session, qvec, course_ids, None, None, pool=max(top_k * 5, 80)
    )
    if not rows:
        return {"query": query, "course_ids": course_ids,
                "answer": "I couldn't find anything about that in the indexed course "
                          "materials. Make sure the course (and its prerequisites) have "
                          "been ingested.",
                "sources": []}

    qarr = np.asarray(qvec, dtype=np.float32)
    vecs = [np.asarray(r[0].embedding, dtype=np.float32) for r in rows]
    order = _mmr(qarr, vecs, top_k, _MMR_LAMBDA)
    selected = [rows[i] for i in order]

    def fmt(row) -> str:
        chunk, course_name, _topic, unit_label, part_name, _uid = row
        body = (chunk.text or "")[:_ANSWER_SNIPPET]
        more = " …[truncated]" if len(chunk.text or "") > _ANSWER_SNIPPET else ""
        cite = _citation(course_name, unit_label, part_name, chunk.section)
        return f"[{cite}]\n{body}{more}"

    context = "\n\n---\n\n".join(fmt(r) for r in selected)
    messages = [
        {
            "role": "system",
            "content": (
                "You answer a learner's question using ONLY the provided course "
                "sections. Be clear and concise. Reproduce any code EXACTLY as shown. "
                "Cite the sections you draw on as [course > unit > section].\n"
                "If the question asks you to LIST or enumerate all of something (e.g. "
                "'what are all the string methods'), scan EVERY provided section and "
                "include every distinct instance you find — do not stop at the first "
                "section that lists some of them; items are often scattered across "
                "different sections. Group them and cite each source.\n"
                "If the sections do not contain the answer, say so plainly — never "
                "invent facts that aren't in the materials. When enumerating, note that "
                "the list reflects only what appears in the retrieved sections."
            ),
        },
        {"role": "user", "content": f"Question: {query}\n\nSections:\n\n{context}"},
    ]
    text = chat(messages)

    sources = []
    for chunk, course_name, topic_name, unit_label, part_name, part_unit_id in selected:
        sources.append({
            "course_id": chunk.course_id,
            "course_name": course_name,
            "topic_name": topic_name,
            "unit_label": unit_label,
            "part_name": part_name,
            "section": chunk.section,
        })
    return {"query": query, "course_ids": course_ids, "answer": text, "sources": sources}


# --------------------------------------------------------------------------- #
# Topic-search judge (prompts ported verbatim from the POC)
# --------------------------------------------------------------------------- #
def _concept_of(query: str) -> str:
    """Map a query / pasted syntax to the underlying concept (better for embedding)."""
    messages = [
        {
            "role": "system",
            "content": (
                "You map a learner's query or pasted Python syntax to the underlying "
                "Python concept it is about. Reply with ONLY a short concept phrase "
                "(e.g. 'list comprehension', 'dictionary methods', 'for loop with range', "
                "'string slicing'). No code, no explanation."
            ),
        },
        {"role": "user", "content": query},
    ]
    return chat(messages, temperature=0).strip()


def check_concept(
    session: Session, *, course_ids: list[str], topic: str, syntax: str | None = None,
    rewrite: bool = True,
) -> dict:
    """Is `topic` (and optional `syntax`) explained in the scoped course materials?
    Returns a grounded verdict (EXPLAINED / PARTIALLY EXPLAINED / NOT EXPLAINED)."""
    locate_text = " ".join(p for p in (topic, syntax) if p and p.strip()).strip()
    if not locate_text:
        return {
            "topic": topic, "syntax": syntax, "search_query": "", "course_ids": course_ids,
            "verdict": "Nothing to search — give a topic and/or a syntax.", "sources": [],
        }

    search_query = _concept_of(locate_text) if rewrite else locate_text
    qvec = embed_query(search_query)
    rows = _candidate_rows(session, qvec, course_ids, None, None, pool=10)
    if not rows:
        return {
            "topic": topic, "syntax": syntax, "search_query": search_query,
            "course_ids": course_ids, "verdict": "Not found — no matching reading material.",
            "sources": [],
        }

    # Expand: pull EVERY chunk of the best hit's Session unit (its full teaching),
    # then merge in the other top hits, de-duplicating by chunk id.
    best_chunk = rows[0][0]
    full_rows = session.execute(
        select(
            RagChunk, Course.course_name, Topic.topic_name, Unit.label, UnitPart.name,
            UnitPart.unit_id,
        )
        .join(Course, RagChunk.course_id == Course.course_id)
        .join(Topic, RagChunk.topic_id == Topic.id)
        .join(Unit, RagChunk.unit_id == Unit.id)
        .join(UnitPart, RagChunk.unit_part_id == UnitPart.id)
        .where(RagChunk.unit_id == best_chunk.unit_id)
        .order_by(RagChunk.position)
    ).all()

    by_id = {r[0].id: r for r in full_rows}
    for r in rows:
        by_id.setdefault(r[0].id, r)
    merged = list(by_id.values())

    def fmt(row) -> str:
        chunk, course_name, _topic, unit_label, part_name, _uid = row
        body = (chunk.text or "")[:_JUDGE_SNIPPET]
        more = " …[truncated]" if len(chunk.text or "") > _JUDGE_SNIPPET else ""
        cite = _citation(course_name, unit_label, part_name, chunk.section)
        return f"[{cite}]\n{body}{more}"

    context = "\n\n---\n\n".join(fmt(r) for r in merged)
    syntax_line = (
        f"Also check specifically whether this exact syntax is shown: {syntax}\n"
        if syntax else ""
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are checking a course's reading materials. Using ONLY the provided "
                "sections, decide whether the given TOPIC is explained and whether its "
                "SYNTAX is shown. Reply in this form:\n"
                "  Verdict: EXPLAINED | PARTIALLY EXPLAINED | NOT EXPLAINED\n"
                "  Then 2-4 lines of evidence: what the materials cover and any syntax/"
                "examples they show, reproducing code EXACTLY. Cite sources as "
                "[course > unit > section].\n"
                "Base everything on the sections only — never invent. If the topic is "
                "absent from the sections, say NOT EXPLAINED."
            ),
        },
        {
            "role": "user",
            "content": f"TOPIC: {topic or search_query}\n{syntax_line}\nSections:\n\n{context}",
        },
    ]
    verdict = chat(messages)

    sources = []
    for chunk, course_name, topic_name, unit_label, part_name, part_unit_id in merged:
        sources.append({
            "course_id": chunk.course_id,
            "course_name": course_name,
            "topic_name": topic_name,
            "unit_label": unit_label,
            "part_name": part_name,
            "section": chunk.section,
        })
    return {
        "topic": topic,
        "syntax": syntax,
        "search_query": search_query,
        "course_ids": course_ids,
        "verdict": verdict,
        "sources": sources,
    }
