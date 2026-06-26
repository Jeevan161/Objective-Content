"""LO pipeline · Node 3 — resolve_prerequisites (LLM-driven, RAG-grounded coverage probe)."""
from __future__ import annotations

from app.mcq_pipeline.config import TEMP_GRAPH, USE_LLM_COVERAGE_PROBE
from app.mcq_pipeline.utils import rag_api
from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.utils.concept_graph import graph_find_prerequisites, slugify
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.utils._common import _bind_rag, _prog


# ── Node 3 · resolve_prerequisites (A · LLM writes queries, RAG answers, LLM judges) ── #
_DEPTH_RANK = {"none": 0, "shallow": 1, "partial": 2, "full": 3}
# depth grade for a prerequisite taught in THIS session, from the profiler's depth_category.
_SESSION_DEPTH = {"deep": "full", "moderate": "partial", "mention": "shallow", "named": "none"}

# The LLM writes the search queries; the GROUNDED RAG tool answers; the LLM judges coverage+depth
# from the retrieved evidence ONLY — never from its own knowledge (that would hallucinate coverage).
_COVERAGE_PLAN_SYS = register("lo.coverage_plan", (
    "You generate SEARCH QUERIES to verify whether a PREREQUISITE concept is present in course material.\n\n"

    "IMPORTANT RULE:\n"
    "- You do NOT decide whether the concept is covered.\n"
    "- You ONLY generate search queries that can help retrieve evidence.\n\n"

    "Each query must be grounded in course-style terminology and must be directly testable in text.\n\n"

    "Generate 2–3 queries max. Each query must target a DIFFERENT aspect:\n"
    "- definition   → checks if the concept is explicitly named or defined\n"
    "- explanation  → checks if reasoning, mechanism, or 'why/how' is explained\n"
    "- application  → checks if the concept is used in an example or procedure\n\n"

    "STRICT RULES:\n"
    "- Do NOT use outside knowledge or encyclopedic phrasing\n"
    "- Do NOT rephrase broadly (e.g., 'data handling concepts')\n"
    "- Each query must be specific enough that it could appear in the course text\n"
    "- Keep queries short, natural, and course-like\n\n"

    'Return ONLY JSON:\n'
    '{"queries":[{"q":"<query>","aspect":"definition|explanation|application"}]}'
))

_COVERAGE_JUDGE_SYS = register("lo.coverage_judge", (
    "You evaluate whether a prerequisite concept is covered in course material USING ONLY provided retrieval evidence.\n\n"

    "ABSOLUTE RULE:\n"
    "- Do NOT use outside knowledge.\n"
    "- Do NOT assume the concept is covered unless evidence explicitly shows it.\n\n"

    "You will be given probe results (definition / explanation / application).\n\n"

    "DEFINITIONS:\n"
    "- definition = concept is explicitly named or stated\n"
    "- explanation = mechanism, reasoning, or how/why is explicitly described\n"
    "- application = concept is demonstrated in a worked example or real use\n\n"

    "DECISION RULES:\n"
    "- covered = TRUE only if definition EXISTS in evidence\n"
    "- depth is determined ONLY from evidence:\n"
    "    none     → no evidence found\n"
    "    shallow  → only definition present\n"
    "    partial  → definition + explanation present, but no application\n"
    "    full     → definition + explanation + application present\n\n"

    "CONSERVATIVE RULE:\n"
    "- If evidence is incomplete, weak, indirect, or ambiguous → choose LOWER depth\n"
    "- If unsure between two levels → always choose the LOWER one\n\n"

    "CRITICAL DISTINCTIONS:\n"
    "- Named mention WITHOUT definition = NOT covered\n"
    "- Definition alone = covered but shallow\n"
    "- Explanation without explicit definition = NOT covered\n\n"

    'Return ONLY JSON:\n'
    '{"covered": <bool>, "depth": "none|shallow|partial|full", "rationale": "<one clear sentence>"}'
))


def _norm_verdict(raw: str) -> str:
    """Collapse a free-text RAG verdict to one of the three canonical states (first line wins)."""
    head = (raw or "").split("\n", 1)[0].upper()
    if "NOT EXPLAINED" in head:
        return "NOT EXPLAINED"
    if "PARTIAL" in head:
        return "PARTIALLY EXPLAINED"
    if "EXPLAINED" in head:
        return "EXPLAINED"
    return "NOT EXPLAINED"


def _single_check(name: str) -> dict | None:
    """Fallback path (probe disabled): one grounded RAG verdict, depth read off its 3-way result.
    None if RAG is unavailable so the caller can fall back to id-membership."""
    try:
        v = _norm_verdict(rag_api.check_concept(name).get("verdict"))
    except Exception:  # noqa: BLE001 — RAG down: unknown
        return None
    covered = v != "NOT EXPLAINED"
    depth = "partial" if v == "PARTIALLY EXPLAINED" else "full" if covered else "none"
    return {"covered": covered, "depth": depth, "rationale": "", "queries": [name], "sources": []}


def _probe_coverage(name: str, description: str) -> dict | None:
    """LLM-driven, RAG-grounded coverage probe: the LLM writes per-aspect queries, RAG answers each,
    and the LLM judges covered + depth from the retrieved evidence ONLY. Returns
    {covered, depth, rationale, queries, sources} — or None if the LLM/RAG is unavailable so the
    caller falls back to id-membership. Grounding is preserved: the verdict is bound to retrieved
    material, the LLM's freedom is limited to query-writing and reasoning over those results."""
    try:
        plan = parse_json(chat(
            [{"role": "system", "content": get_prompt("lo.coverage_plan", _COVERAGE_PLAN_SYS)},
             {"role": "user",
              "content": str({"concept": name, "description": (description or "")[:300]})}],
            temperature=TEMP_GRAPH)) or {}
        queries = [q for q in (plan.get("queries") or [])
                   if isinstance(q, dict) and str(q.get("q", "")).strip()][:3] \
            or [{"q": name, "aspect": "definition"}]

        def _run(q):
            r = rag_api.check_concept(str(q["q"]))
            return {"q": q["q"], "aspect": q.get("aspect", "definition"),
                    "verdict": _norm_verdict(r.get("verdict")),
                    "evidence": (r.get("verdict") or "")[:240],
                    "sources": r.get("sources", [])}

        probes = [p for p in pmap(_run, queries) if p]
        verdict = parse_json(chat(
            [{"role": "system", "content": get_prompt("lo.coverage_judge", _COVERAGE_JUDGE_SYS)},
             {"role": "user", "content": str({"concept": name, "probes": [
                 {k: p[k] for k in ("aspect", "verdict", "evidence")} for p in probes]})}],
            temperature=TEMP_GRAPH)) or {}
    except Exception:  # noqa: BLE001 — LLM/RAG unavailable -> caller falls back to id-membership
        return None

    covered = bool(verdict.get("covered"))
    depth = verdict.get("depth")
    if depth not in _DEPTH_RANK:
        depth = "partial" if covered else "none"
    # consistency guard: not-covered is always "none"; a covered concept is at least "shallow".
    if not covered:
        depth = "none"
    elif depth == "none":
        depth = "shallow"
    return {"covered": covered, "depth": depth,
            "rationale": str(verdict.get("rationale", ""))[:200],
            "queries": [q["q"] for q in queries],
            "sources": [s for p in probes for s in (p.get("sources") or [])][:6]}


def resolve_prerequisites(state, config) -> dict:
    """Assign each apply/scenario outcome its prerequisite closure + a scope verdict, GRADED by
    coverage depth. Coverage is LLM-driven but RAG-GROUNDED: the LLM writes search queries, the
    RAG tool answers across the accessible scope (current session OR a prior course), and the LLM
    judges covered + depth from the retrieved evidence only. A prerequisite is satisfied if it is
    taught (here or earlier) or is a declared foundational assumption; merely being a concept-id is
    not enough. Records per-outcome `prerequisite_coverage` (covered / shallow / uncovered + per-
    prereq records with depth) for auditing. Graceful if RAG/LLM is unavailable."""
    _bind_rag(config)
    prog = _prog(config)
    prog.start("resolve_prerequisites")
    cg = state["concept_graph"]
    assumed_ids = {"C_" + slugify(p) for p in cg["assumed_prior"]}
    assumed_name = {"C_" + slugify(p): p for p in cg["assumed_prior"]}
    in_scope_ids = {c["concept_id"] for c in state["concept_inventory"] if c["in_scope"]}
    inv_by_id = {c["concept_id"]: c for c in state["concept_inventory"]}
    name_by_id = {c["concept_id"]: c["canonical_name"] for c in state["concept_inventory"]}
    outcomes = [dict(o) for o in state["outcomes"]]

    probe_cache: dict = {}

    def _probe(pid: str):
        """Cached coverage probe for a NON-local prerequisite (per concept_id)."""
        if pid in probe_cache:
            return probe_cache[pid]
        nm = name_by_id.get(pid) or assumed_name.get(pid) or pid[2:].replace("_", " ")
        desc = (inv_by_id.get(pid) or {}).get("description", "")
        res = _probe_coverage(nm, desc) if USE_LLM_COVERAGE_PROBE else _single_check(nm)
        probe_cache[pid] = res
        return res

    for o in outcomes:
        if o["bloom_level"] not in ("apply", "scenario"):
            o["prerequisites"], o["prerequisite_scope"] = [], None
            continue
        prereqs = graph_find_prerequisites(state, o["concept_id"])
        if not prereqs:
            prereqs = sorted(assumed_ids)
        o["prerequisites"] = prereqs
        # Cross-session PROVENANCE + DEPTH (P2): record HOW each prerequisite is satisfied and HOW
        # DEEPLY, so answerability is auditable — taught_here (this session) / taught_earlier (a
        # prior course, RAG-confirmed) / assumed_prior (declared foundational) / unresolved (risk).
        records, covered, shallow, uncovered, all_ok = [], [], [], [], True
        for p in prereqs:
            nm = name_by_id.get(p) or assumed_name.get(p) or p[2:].replace("_", " ")
            rationale = ""
            if p in in_scope_ids:                          # taught in THIS session
                prov = "taught_here"
                depth = _SESSION_DEPTH.get(
                    (inv_by_id.get(p) or {}).get("depth_category", "moderate"), "partial")
            else:
                res = _probe(p)
                if res is None:                            # RAG/LLM down -> id-membership fallback
                    prov = "assumed_prior" if p in assumed_ids else "unresolved"
                    depth = "n/a" if prov == "assumed_prior" else "none"
                elif res["covered"]:
                    prov, depth, rationale = "taught_earlier", res["depth"], res.get("rationale", "")
                elif p in assumed_ids:
                    prov, depth = "assumed_prior", "n/a"   # declared foundational (RAG says absent)
                else:
                    prov, depth, rationale = "unresolved", "none", res.get("rationale", "")
            ok = prov != "unresolved"                      # presence (drives V6 scope, unchanged)
            sufficient = ok and depth in ("partial", "full", "n/a")   # "covered ENOUGH"
            if not ok:
                uncovered.append(nm)
            else:
                covered.append(nm)                         # all present prereqs (V/judge contract)
                if not sufficient:
                    shallow.append(nm)                     # present but thin -> soft answerability risk
            records.append({"id": p, "name": nm, "provenance": prov, "depth": depth,
                            "ok": ok, "sufficient": sufficient, "rationale": rationale})
            all_ok = all_ok and ok
        o["prerequisite_scope"] = "all_in_scope" if all_ok else "has_out_of_scope"
        o["prerequisite_coverage"] = {"covered": covered, "shallow": shallow,
                                      "uncovered": uncovered, "records": records}
    apply_outs = [o for o in outcomes if o["bloom_level"] in ("apply", "scenario")]
    snapshot = {"apply_outcomes": len(apply_outs),
                "fully_covered": sum(1 for o in apply_outs
                                     if not (o.get("prerequisite_coverage") or {}).get("uncovered")),
                "with_uncovered": sum(1 for o in apply_outs
                                      if (o.get("prerequisite_coverage") or {}).get("uncovered")),
                "outcomes": [{"id": o["id"], "title": o["title"], "scope": o.get("prerequisite_scope"),
                              "covered": (o.get("prerequisite_coverage") or {}).get("covered", []),
                              "shallow": (o.get("prerequisite_coverage") or {}).get("shallow", []),
                              "uncovered": (o.get("prerequisite_coverage") or {}).get("uncovered", [])}
                             for o in apply_outs]}
    prog.done("resolve_prerequisites",
              detail=f"{len(apply_outs)} apply · {snapshot['with_uncovered']} with uncovered prereqs",
              snapshot=snapshot)
    return {"outcomes": outcomes}
