"""LO pipeline · Node 7 — resolve_prerequisites (RAG-verified prerequisite closure)."""
from __future__ import annotations

from app.mcq_pipeline.utils import rag_api
from app.mcq_pipeline.utils.concept_graph import graph_find_prerequisites, slugify
from app.mcq_pipeline.nodes._common import _bind_rag, _prog


# ── Node 7 · resolve_prerequisites (D) ────────────────────────────────────── #
def resolve_prerequisites(state, config) -> dict:
    """Assign each apply outcome its prerequisite closure + a scope verdict. The verdict is
    RAG-VERIFIED: a prerequisite must be actually TAUGHT across the accessible scope (the
    CURRENT session OR a prior course — check_concept is scoped to both) or be a declared
    foundational assumption; merely being present as a concept-id is not enough. Records
    per-outcome `prerequisite_coverage` evidence. Graceful if RAG is unavailable."""
    _bind_rag(config)
    prog = _prog(config)
    prog.start("resolve_prerequisites")
    cg = state["concept_graph"]
    assumed_ids = {"C_" + slugify(p) for p in cg["assumed_prior"]}
    assumed_name = {"C_" + slugify(p): p for p in cg["assumed_prior"]}
    in_scope_ids = {c["concept_id"] for c in state["concept_inventory"] if c["in_scope"]}
    name_by_id = {c["concept_id"]: c["canonical_name"] for c in state["concept_inventory"]}
    outcomes = [dict(o) for o in state["outcomes"]]

    cover_cache: dict = {}

    def _covered(pid: str):
        """True/False if RAG can resolve coverage across the scope (current session + prior
        courses), None if RAG is unavailable."""
        if pid in cover_cache:
            return cover_cache[pid]
        name = name_by_id.get(pid) or assumed_name.get(pid) or pid[2:].replace("_", " ")
        try:
            verdict = (rag_api.check_concept(name).get("verdict") or "").split("\n", 1)[0].upper()
            res = "NOT EXPLAINED" not in verdict
        except Exception:  # noqa: BLE001 — RAG down: unknown
            res = None
        cover_cache[pid] = res
        return res

    for o in outcomes:
        if o["bloom_level"] not in ("apply", "scenario"):
            o["prerequisites"], o["prerequisite_scope"] = [], None
            continue
        prereqs = graph_find_prerequisites(state, o["concept_id"])
        if not prereqs:
            prereqs = sorted(assumed_ids)
        o["prerequisites"] = prereqs
        # Cross-session PROVENANCE (P2): record HOW each prerequisite is satisfied so
        # answerability is auditable — taught_here (this session) / taught_earlier (a prior
        # course, RAG-confirmed) / assumed_prior (declared foundational) / unresolved (the
        # answerability risk: not taught anywhere and not a declared assumption).
        records, covered, uncovered, all_ok = [], [], [], True
        for p in prereqs:
            nm = name_by_id.get(p) or assumed_name.get(p) or p[2:].replace("_", " ")
            if p in in_scope_ids:
                prov = "taught_here"
            else:
                cov = _covered(p)
                if cov is True:
                    prov = "taught_earlier"                     # found in a prior course via RAG
                elif p in assumed_ids:
                    prov = "assumed_prior"                      # declared foundational (RAG False/down)
                else:
                    prov = "unresolved"                         # not taught, not assumed -> risk
            ok = prov != "unresolved"
            (covered if ok else uncovered).append(nm)
            records.append({"id": p, "name": nm, "provenance": prov, "ok": ok})
            all_ok = all_ok and ok
        o["prerequisite_scope"] = "all_in_scope" if all_ok else "has_out_of_scope"
        o["prerequisite_coverage"] = {"covered": covered, "uncovered": uncovered,
                                      "records": records}
    prog.done("resolve_prerequisites")
    return {"outcomes": outcomes}


