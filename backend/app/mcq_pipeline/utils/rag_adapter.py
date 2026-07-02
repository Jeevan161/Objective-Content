"""
app/mcq_pipeline/rag_adapter.py
-------------------------------
Run-scoped RAG, backed by the app's pgvector retrieval (`app.services.rag_search`).
Every call is metadata-scoped to the run's `course_ids` (course + prerequisite
courses) — never unscoped. When the scope isn't ingested (no RagChunks), it
degrades to keyword search over the in-memory session reading material so the
apply agent still runs.

Each method opens its OWN short-lived Session (thread-safe for the per-LO
threadpool — mirrors `app/services/jobs.py`).
"""

from __future__ import annotations

import re
import threading

from app.db.session import SessionLocal
from app.services import rag_search


def _split_sections(md: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) sections for degraded keyword search."""
    if not md:
        return []
    sections: list[tuple[str, str]] = []
    title, buf = "Reading material", []
    for line in md.splitlines():
        if line.lstrip().startswith("#"):
            if buf:
                sections.append((title, "\n".join(buf)))
                buf = []
            title = line.lstrip("#").strip() or title
        else:
            buf.append(line)
    if buf:
        sections.append((title, "\n".join(buf)))
    return sections or [("Reading material", md)]


def _terms(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", (text or "").lower()) if len(t) > 2]


# Programming keywords that signal real code. Only applied INSIDE fenced code
# blocks — many of these (for/if/from/class…) are also ordinary English words,
# so scanning prose would false-positive on conceptual sessions.
_CODE_KW = re.compile(
    r"\b(def|class|import|return|print|elif|lambda|async|await"
    r"|function|const|let|var|public|private|void|SELECT|INSERT|UPDATE|DELETE)\b"
)
# Unambiguous shell/dev commands — specific enough to be safe in prose too.
_CMD = re.compile(
    r"(pip install|manage\.py|python3?\s+-m\b|python3?\s+manage|npm (install|run)"
    r"|git (clone|init|commit)|\$\s+\w)"
)


def detect_code(md: str) -> bool:
    """True when the reading material actually shows code or runnable commands —
    used to forbid code-path question types on purely conceptual sessions.

    A fenced block counts as code only if it has a programming keyword or code
    punctuation, so diagrams / TOCs / arrow-flows inside ``` fences don't qualify.
    Prose is never scanned for keywords (for/if/from/class are English words too)."""
    md = md or ""
    for block in re.findall(r"```[^\n]*\n(.*?)```", md, re.DOTALL):
        if _CODE_KW.search(block) or re.search(r"[(){};]|=[^=]", block):
            return True
    return bool(_CMD.search(md))


class RagAdapter:
    def __init__(self, *, course_ids: list[str], prereq_units: list[dict],
                 reading_material: str, ingested: bool, unit_ids: list[str] | None = None,
                 domain: str = ""):
        if not course_ids:
            raise ValueError("RagAdapter requires a non-empty course_ids scope.")
        self.course_ids = course_ids
        # course_ids[0] is the CURRENT course being generated; the rest are prerequisite courses.
        # Grounding retrieval stays in the current course (see `search`) so a prereq course's
        # chunks can't pollute a question's grounding.
        self.primary_course_id = course_ids[0]
        self._prereq_units = prereq_units
        self.reading_material = reading_material or ""
        self.ingested = ingested
        # Course-level MCQ generation DOMAIN (e.g. "SQL"); empty = generic. Set from
        # Course.question_domain at build time and read by the generation/review/type
        # nodes to activate domain-specific rules deterministically (no per-LO guessing).
        self.domain = (domain or "").strip().upper()
        # Optional reading-material unit_id filter (main course units + selected
        # prerequisite units) — narrows GROUNDING search to chosen content.
        self.unit_ids = unit_ids or None
        # Whether this session actually shows code — gates code-path question types.
        self.has_code = detect_code(self.reading_material)
        # Per-run memo for idempotent RAG probes (check_concept / code_coverage): the same
        # term recurs across many LOs/questions in a run, so cache the result on the adapter
        # (one per run -> dropped with the run). Thread-safe for the per-LO worker pool.
        self._rag_memo: dict = {}
        self._rag_memo_lock = threading.Lock()

    # --- retrieval ---------------------------------------------------------- #
    def search(self, query: str, *, top_k: int = 6) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        if self.ingested:
            # GROUNDING must stay in the CURRENT course. Cross-course prerequisite chunks pollute a
            # question's grounding — e.g. a Python "Dictionaries > Accessing Items" section surfacing
            # (and looking like "evidence") for a Django `request.POST` question, which is how a wrong,
            # ungrounded syntax gets reinforced as the key. When the reviewer/generator explicitly
            # chose prerequisite units (`unit_ids`), honor the full scope (that filter already narrows
            # it); otherwise restrict grounding retrieval to the primary (current) course.
            cids = self.course_ids if self.unit_ids else [self.primary_course_id]
            with SessionLocal() as session:
                hits = rag_search.search(
                    session, course_ids=cids, query=query,
                    unit_ids=self.unit_ids, top_k=top_k,
                )
            return [
                {
                    "seq": 0,
                    "unit_name": h.get("unit_label") or h.get("part_name"),
                    "topic_name": h.get("topic_name"),
                    "section": h.get("section"),
                    "snippet": h.get("snippet"),
                }
                for h in hits
            ]
        return self._fallback_search(query, top_k)

    def _fallback_search(self, query: str, top_k: int) -> list[dict]:
        terms = _terms(query)
        scored = []
        for title, body in _split_sections(self.reading_material):
            blob = f"{title} {body}".lower()
            scored.append((sum(blob.count(t) for t in terms), title, body))
        scored.sort(key=lambda x: -x[0])
        out: list[dict] = []
        for score, title, body in scored[:top_k]:
            if score <= 0 and out:
                break
            out.append({"seq": 0, "unit_name": "session", "topic_name": None,
                        "section": title, "snippet": body[:400]})
        return out or [{"seq": 0, "unit_name": "session", "topic_name": None,
                        "section": "Reading material", "snippet": self.reading_material[:400]}]

    def check_concept(self, topic: str, syntax: str | None = None) -> dict:
        if self.ingested:
            with SessionLocal() as session:
                res = rag_search.check_concept(
                    session, course_ids=self.course_ids, topic=topic, syntax=syntax
                )
            verdict = res.get("verdict", "")
            # GUARANTEE the CURRENT session is never missed: pgvector retrieval can fail to surface a
            # concept that IS taught here (a canonical name embeds poorly, or the treatment is brief).
            # If RAG didn't already find it but the concept is present in THIS session's reading
            # material, treat it as explained — a concept drawn from the current session must NOT be
            # marked NOT EXPLAINED (which would wrongly flag it external / an uncovered prerequisite).
            head = verdict.split("\n", 1)[0].upper()
            if not (head.startswith("EXPLAINED") or head.startswith("PARTIALLY")) \
                    and self._present(topic) and (not syntax or self._present(syntax)):
                verdict = "EXPLAINED — present in the current session's reading material."
            return {
                "topic": topic, "syntax": syntax, "verdict": verdict,
                "files": [],
                "sources": [
                    {"seq": 0, "unit_name": s.get("unit_label"), "section": s.get("section")}
                    for s in (res.get("sources") or [])[:6]
                ],
            }
        present = self._present(topic) and (not syntax or self._present(syntax))
        return {"topic": topic, "syntax": syntax,
                "verdict": "EXPLAINED" if present else "NOT EXPLAINED",
                "files": [], "sources": []}

    def code_coverage(self, concept: str, syntax: str | None = None, *, max_seq=None) -> dict:
        # Hard gate: a session that shows no code can't support a code-path
        # question, no matter how well the concept is "explained" in prose.
        if not self.has_code:
            return {"concept": concept, "syntax": syntax, "covered": False,
                    "verdict": "NOT EXPLAINED — the session shows no code/commands to write or run.",
                    "sources": []}
        res = self.check_concept(concept, syntax)
        head = (res.get("verdict") or "").split("\n", 1)[0].upper()
        return {"concept": concept, "syntax": syntax,
                "covered": "NOT EXPLAINED" not in head,
                "verdict": res.get("verdict", ""), "sources": res.get("sources", [])}

    def find_prerequisites(self, topic: str, *, top_k: int = 6) -> dict:
        # Cross-course prerequisite UNITS (deterministic) PLUS a scoped RAG search that ALSO
        # covers the CURRENT session — a prerequisite may be taught earlier in this SAME
        # session, not only in prior courses. search() is scoped to course_ids + unit_ids
        # (which include the current course/unit), so the current session is included.
        in_session = self.search(topic, top_k=top_k) if (topic or "").strip() else []
        return {"topic": topic, "prerequisites": self._prereq_units, "in_session": in_session}

    def prior_units(self) -> list[dict]:
        return list(self._prereq_units)

    def _present(self, text: str) -> bool:
        terms = _terms(text)
        if not terms:
            return False
        rm = self.reading_material.lower()
        return sum(1 for t in terms if t in rm) >= max(1, len(terms) // 2)
