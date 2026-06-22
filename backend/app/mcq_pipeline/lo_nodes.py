"""
app/mcq_pipeline/lo_nodes.py
----------------------------
The 10 nodes of the deterministic LO pipeline (PRD v1.0), ported from the POC
`_build_lo.py` into functional LangGraph nodes. Each node is `(state, config) ->
partial-state-update` and pulls its live RAG/progress objects from the run's
RunContext (see `lo_state`), so nothing run-scoped is module-global and concurrent
runs stay isolated.

6 nodes are pure (parse/canonicalize/plan/resolve/validate/finalize); the 5 agent
nodes (extract/graph/author/coverage_gate/repair) are drift-suppressed via
self-consistency voting, controlled-verb enums, exact per-topic counts, and (for
coverage_gate) a strict coverage rubric.

`finalize` lives in `lo_artifact`; this module covers the remaining nodes.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from . import rag_api, scope
from .concurrency import pmap
from .lo_config import (
    APPLY_VERBS, COMPARISON_VERBS, DEFAULT_SPLIT, DEPTH_CATEGORIES, DROP_NAMED_ONLY,
    K_SAMPLES, LOW_DEMAND_VERBS, MAJORITY, MAX_LOS_PER_CONCEPT, MIN_BUDGET,
    QUESTION_BUDGET, SKILL_TYPES, TEMP_AUTHOR, TEMP_EXTRACT, TEMP_GRAPH, VERBS,
    allowed_verbs_for,
)

_FENCE_RE = re.compile(r"```[a-zA-Z0-9]*\n(.*?)```", re.S)
from .lo_concept_graph import (
    canonical_name, concept_depth, description_grounded, graph_find_prerequisites,
    ground_quote, has_explicit_contrast, is_procedural, is_setup_or_cli,
    largest_remainder, loosen_text, reachable, recover_syntax, slugify, syntax_grounded,
)
from .lo_llm import chat, parse_json
from .lo_state import run_ctx
from .prompt_store import get_prompt, register


# --- run-context helpers (defensive so pure nodes are unit-testable) ------- #
class _NoProgress:
    def start(self, *a, **k): pass
    def done(self, *a, **k): pass
    def tick(self, *a, **k): pass
    def detail(self, *a, **k): pass
    def error(self, *a, **k): pass
    def counter(self, key, total):
        def _on(**k): pass
        return _on


_NOOP = _NoProgress()


def _ctx(config):
    try:
        return run_ctx(config)
    except Exception:  # noqa: BLE001 — pure nodes may run without a registered ctx
        return None


def _prog(config):
    c = _ctx(config)
    return c.progress if c is not None else _NOOP


def _bind_rag(config) -> None:
    c = _ctx(config)
    if c is not None and c.rag is not None:
        scope.set_adapter(c.rag)


# ── Node 1 · parse_structure (D) ─────────────────────────────────────────── #
_TOPIC_HEADING = re.compile(r"^(#{1,3})\s+(.*\S)\s*$")
# NOTE: 'conclusion' intentionally NOT here — in essay/research/science content a
# "Conclusion" is often substantive, not a recap; dropping it loses real material.
_RECAP = re.compile(r"\b(summary|recap|revision|cheat\s*sheet|takeaway)", re.I)


def parse_structure(state, config) -> dict:
    prog = _prog(config)
    prog.start("parse_structure")
    text = state["source_text"]
    blocks, cur, in_fence = [], None, False
    for line in text.split("\n"):
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
        m = None if in_fence else _TOPIC_HEADING.match(line)
        if m and len(m.group(1)) <= 2:                 # split on # / ## only
            cur = {"title": m.group(2).strip(), "lines": []}
            blocks.append(cur)
        elif cur is not None:
            cur["lines"].append(line)
        elif line.strip():                             # preamble before first heading
            cur = {"title": "Introduction", "lines": [line]}
            blocks.append(cur)
    if not blocks:                                     # fallback: whole doc as one topic
        blocks = [{"title": "Introduction", "lines": text.split("\n")}]

    sections, order, logs = [], 0, []
    for b in blocks:
        body = "\n".join(b["lines"]).strip()
        is_recap = bool(_RECAP.search(b["title"]))
        if is_recap or not body:
            logs.append({"node": "parse_structure", "dropped": b["title"],
                         "reason": "recap/summary" if is_recap else "empty"})
            continue
        sections.append({"topic_id": f"T{order+1}_{slugify(b['title'])}"[:48],
                         "title": b["title"], "order": order, "text": body,
                         "has_code": "```" in body})
        order += 1
    if not sections:
        raise RuntimeError("ESCALATE: structure could not be recovered from source.")
    prog.done("parse_structure", detail=f"{len(sections)} topics")
    return {"sections": sections, "log": logs}


# ── Node 2 · extract_concepts (A · K-sample self-consistency) ─────────────── #
_EXTRACT_SYS = register("lo.extract_sys", """\
You extract the distinct teachable CONCEPTS from one section of instructional reading material (any subject). A concept is a TRANSFERABLE idea, skill, or rule a learner could be assessed on and could apply BEYOND this specific reading (e.g. "dependency version conflict", "virtual environment isolation", "list slicing").

Instructional text usually teaches a concept THROUGH an illustrative example — a scenario, sample program, story, or named placeholders such as "Project A"/"Project B", sample variable/file/function names, characters, or one-off sample values. The example is EVIDENCE for a concept; it is NOT itself a concept. Extract the general concept the example demonstrates and give it a self-contained, transferable canonical name. NEVER turn an example's label or a one-off detail into a concept (extract "dependency version conflict", NOT "Project A" or "the Django version Project A needs").

Genuinely taught technologies, tools, commands, or terms the learner must know BY NAME (e.g. "venv", "pip", "Django") ARE valid concepts — keep those. Do NOT invent concepts not present in the text.

Prefer SPECIFIC, single-idea concepts over broad umbrellas. If a section covers a broad activity (e.g. "project setup"), extract the distinct sub-concepts it actually teaches (e.g. "dependency isolation", "creating a virtual environment", "activating an environment") rather than one vague umbrella.

A concept must be SUBSTANTIVELY taught (the section defines, explains, or demonstrates it) — not merely NAMED in passing. When the section just lists items or gives a one-line overview (e.g. "the three frameworks are A, B, C, each for a different use"), capture the overview as ONE concept; do not mint a deep, separately-assessable concept per named item, and do not imply the items are contrasted unless the text actually contrasts them.

For each concept give:
- "name": a SHORT transferable canonical name (no example-specific labels).
- "description": 1-2 sentences stating what THIS section actually teaches about the concept — grounded ONLY in the text, no outside knowledge, no claims the section does not make. Describe it the way the material does; do not generalize beyond it.
- "quote": a VERBATIM evidence quote copied from the section that supports the description (the quote MAY cite the example).
Return ONLY a JSON list: [{"name": "...", "description": "...", "quote": "..."}]. 4-8 concepts max.""")


def _extract_once(section: dict) -> dict:
    reply = chat([{"role": "system", "content": get_prompt("lo.extract_sys", _EXTRACT_SYS)},
                  {"role": "user", "content":
                   f"SECTION: {section['title']}\n\n{section['text'][:3500]}"}],
                 temperature=TEMP_EXTRACT)
    data = parse_json(reply) or []
    out = {}
    for item in data if isinstance(data, list) else []:
        name = (item.get("name") or "").strip()
        if name:
            out[name.lower().strip()] = {"name": name,
                                         "description": (item.get("description") or "").strip(),
                                         "quote": (item.get("quote") or "").strip()}
    return out


def _extract_section(section: dict) -> tuple[list, dict, bool]:
    samples = [_extract_once(section) for _ in range(K_SAMPLES)]
    tally = Counter(k for s in samples for k in s)
    kept = [k for k, c in tally.items() if c >= MAJORITY]
    raw = []
    for k in kept:
        # Pick the richest sample for this concept: the one whose description is longest
        # (most informative) among the K runs that surfaced it.
        evs = [s[k] for s in samples if k in s]
        ev = max(evs, key=lambda e: len(e.get("description") or ""))
        raw.append({"name": ev["name"],
                    "description": ev.get("description", ""),
                    "evidence": {"quote": ev["quote"], "section": section["topic_id"]}})
    log = {"node": "extract_concepts", "section": section["topic_id"], "k": K_SAMPLES,
           "proposed": len(tally), "kept": len(kept), "discarded_tail": len(tally) - len(kept)}
    return raw, log, (not kept)


_TOPIC_DESC_SYS = register("lo.topic_desc_sys", (
    "You write a SHORT factual description of what one topic/section of instructional "
    "reading material actually teaches — for a curriculum map. 1-2 sentences, grounded "
    "ONLY in the text (no outside knowledge, no claims the section does not make). State "
    "the general subject matter the section covers; do NOT reference example-specific or "
    'source-local labels (e.g. "Project A", a sample variable name). Return ONLY JSON: '
    '{"description": "..."}.'
))


def _describe_topic(section: dict) -> str:
    try:
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("lo.topic_desc_sys", _TOPIC_DESC_SYS)},
             {"role": "user", "content": f"TOPIC: {section['title']}\n\n{section['text'][:3500]}"}],
            temperature=TEMP_EXTRACT)) or {}
    except Exception:  # noqa: BLE001 — LLM down: leave the topic description empty, never fail the node
        data = {}
    return (data.get("description") or "").strip()


def extract_concepts(state, config) -> dict:
    _bind_rag(config)
    prog = _prog(config)
    prog.start("extract_concepts")
    results = pmap(_extract_section, state["sections"])
    descriptions = pmap(_describe_topic, state["sections"])   # topic-level name+description
    raw, logs = [], []
    for section_raw, log, zero in results:
        raw.extend(section_raw)
        logs.append(log)
        if zero:
            logs.append({"node": "extract_concepts", "section": log["section"],
                         "flag": "zero_stable_concepts"})
    # Enrich each topic with a grounded description (evidence-bound: keep only if it
    # traces to the section text, else fall back to the section title).
    sections = []
    for sec, desc in zip(state["sections"], descriptions):
        s = dict(sec)
        s["description"] = desc if description_grounded(desc, [], sec["text"]) else sec["title"]
        sections.append(s)
    prog.done("extract_concepts", detail=f"{len(raw)} stable concepts")
    return {"raw_concepts": raw, "sections": sections, "log": logs}


# ── Node 3 · canonicalize_concepts (D · variance sink) ────────────────────── #
def canonicalize_concepts(state, config) -> dict:
    prog = _prog(config)
    prog.start("canonicalize_concepts")
    sec_text = {s["topic_id"]: s["text"] for s in state["sections"]}
    inv: dict = {}
    for rc in state["raw_concepts"]:
        canon = canonical_name(rc["name"])
        cid = "C_" + slugify(canon)
        section_text = sec_text.get(rc["evidence"]["section"], "")
        quote = ground_quote(rc["name"], section_text)          # deterministic V9
        procedural = is_procedural(quote, canon, section_text)
        # Evidence-bind the description: keep the LLM prose only if it traces to the
        # material; otherwise fall back to the verbatim evidence quote so the concept's
        # description is grounded BY CONSTRUCTION (no hallucinated descriptions survive).
        ev_quote = rc["evidence"]["quote"]
        desc = (rc.get("description") or "").strip()
        if not description_grounded(desc, [ev_quote, quote], section_text):
            desc = ev_quote or quote
        if cid not in inv:
            inv[cid] = {"concept_id": cid, "canonical_name": canon.title(),
                        "topic_id": rc["evidence"]["section"], "in_scope": True,
                        "procedural": procedural, "description": desc,
                        "evidence_quotes": [q for q in {ev_quote, quote} if q],
                        "evidence": rc["evidence"]}
        else:
            inv[cid]["procedural"] = inv[cid]["procedural"] or procedural
            for q in (ev_quote, quote):
                if q and q not in inv[cid]["evidence_quotes"]:
                    inv[cid]["evidence_quotes"].append(q)
            if len(desc) > len(inv[cid]["description"]):   # keep the richest grounded prose
                inv[cid]["description"] = desc

    # Merge near-duplicate concepts (not just log them): token-set Jaccard >= 0.7 AND
    # one token-set a subset of the other (e.g. plural/singular, "global environment"
    # vs "global environments"). Union-find so merges are order-independent. The
    # surviving concept_id is the more procedural / more specific (longer name) one.
    logs = []
    ids = sorted(inv)
    parent = {k: k for k in ids}

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(ids):
        ta = set(a.split("_")[1:])
        for b in ids[i + 1:]:
            tb = set(b.split("_")[1:])
            if not (ta and tb):
                continue
            if len(ta & tb) / len(ta | tb) >= 0.7 and (ta <= tb or tb <= ta):
                ra, rb = _find(a), _find(b)
                if ra != rb:
                    wa, wb = inv[ra], inv[rb]
                    keep = ra if (wa["procedural"], len(wa["canonical_name"])) >= \
                        (wb["procedural"], len(wb["canonical_name"])) else rb
                    drop = rb if keep == ra else ra
                    parent[drop] = keep

    groups: dict = {}
    for k in ids:
        groups.setdefault(_find(k), []).append(k)
    for rep, members in groups.items():
        for m in members:
            if m != rep:
                inv[rep]["procedural"] = inv[rep]["procedural"] or inv[m]["procedural"]
                for q in inv[m].get("evidence_quotes", []):
                    if q and q not in inv[rep]["evidence_quotes"]:
                        inv[rep]["evidence_quotes"].append(q)
                if len(inv[m].get("description", "")) > len(inv[rep].get("description", "")):
                    inv[rep]["description"] = inv[m]["description"]
                logs.append({"node": "canonicalize", "merged": m, "into": rep})
    inventory = [inv[k] for k in sorted(groups)]
    prog.done("canonicalize_concepts",
              detail=f"{len(inventory)} concepts ({sum(c['procedural'] for c in inventory)} procedural)")
    return {"concept_inventory": inventory, "log": logs}


# ── Node 4 · build_dependency_graph (A · K-sample edge voting) ────────────── #
_GRAPH_SYS = register("lo.graph_sys", (
    "You analyze ONE target concept from a reading (ANY subject) against the OTHER concepts "
    "taught in the same session, and output JSON about the TARGET only.\n"
    'Output ONLY JSON: {"prerequisites": ["<concept_id>", ...], "applied_skill": <bool>, '
    '"assumed_prior": ["<short prior-knowledge name>", ...]}.\n'
    "- prerequisites: which of the OTHER given concept_ids must be understood BEFORE the "
    "target (are its direct prerequisites). Use ONLY ids from the given list; [] if none.\n"
    "- applied_skill: true ONLY if the target is a PERFORMABLE SKILL the learner actively "
    "carries out or APPLIES (solve a problem, compute a value, apply a method/framework to a "
    "case, construct an argument, produce an artifact, execute steps) — NOT a fact, "
    "definition, or idea merely recognized or explained. DOMAIN-GENERAL: include "
    "non-programming skills, not just code.\n"
    "- assumed_prior: foundational knowledge the target ASSUMES but that is NOT taught in "
    "this session (short generic names); [] if none. Anything the target needs that is "
    "OUTSIDE the given concept list belongs here, NOT in prerequisites.\n"
    "Judge ONLY from what the material teaches; do not invent."
))


def build_dependency_graph(state, config) -> dict:
    """Build the prerequisite DAG + applied-skill + assumed-prior signals ONE concept at a
    time (sequential, isolated), with K-sample self-consistency per concept. assumed_prior is
    LLM-derived per concept (no hardcoded fallback)."""
    _bind_rag(config)
    prog = _prog(config)
    inv = state["concept_inventory"]
    ids = [c["concept_id"] for c in inv]
    idset = set(ids)

    def _line(c):
        return f'{c["concept_id"]}: {c["canonical_name"]} (evidence: "{(c.get("evidence") or {}).get("quote", "")[:100]}")'

    on_done = prog.counter("build_dependency_graph", len(inv))
    prereq_votes: dict = {}                 # cid -> Counter(prereq_id -> votes)
    skill_votes, prior = Counter(), Counter()
    # ONE concept at a time (sequential, isolated); K-sample self-consistency per concept.
    for c in inv:
        cid = c["concept_id"]
        others = "\n".join(_line(o) for o in inv if o["concept_id"] != cid) or "(none)"
        usr = f"TARGET CONCEPT:\n{_line(c)}\n\nOTHER CONCEPTS IN THIS SESSION:\n{others}"

        def _vote(_i, _usr=usr):
            return parse_json(chat([{"role": "system", "content": get_prompt("lo.graph_sys", _GRAPH_SYS)},
                                    {"role": "user", "content": _usr}], temperature=TEMP_GRAPH)) or {}

        pv = Counter()
        for d in pmap(_vote, list(range(K_SAMPLES))):
            if not isinstance(d, dict):
                continue
            for p in d.get("prerequisites", []):
                if p in idset and p != cid:
                    pv[p] += 1
            if d.get("applied_skill") is True:
                skill_votes[cid] += 1
            for ap in d.get("assumed_prior", []):
                if isinstance(ap, str) and ap.strip():
                    prior[ap.strip()] += 1
        prereq_votes[cid] = pv
        on_done()

    # majority-voted prerequisites -> edges P->C (P is a prerequisite of C), with cycle guard
    adj, edges, logs = defaultdict(set), [], []
    candidates = sorted((p, cid) for cid, pv in prereq_votes.items()
                        for p, v in pv.items() if v >= MAJORITY)
    for (p, cid) in candidates:
        if not reachable(adj, cid, p):          # adding p->cid is safe if cid can't already reach p
            adj[p].add(cid)
            edges.append({"from": p, "to": cid, "relation": "depends_on"})
        else:
            logs.append({"node": "build_graph", "dropped_edge": [p, cid], "reason": "would_create_cycle"})
    assumed = [p for p, v in prior.items() if v >= MAJORITY]   # LLM-derived; NO hardcoded default

    # Two-level graph (P2): lift concept edges to a TOPIC dependency DAG — topic B precedes
    # topic A when some concept of A depends on a concept of B. Deterministic (derived, no
    # extra LLM), cycle-guarded independently of the concept graph. Drives sequencing and
    # gives the portal a coarse structural map.
    topic_of = {c["concept_id"]: c["topic_id"] for c in inv}
    tadj, topic_edges = defaultdict(set), []
    for (tp, tc) in sorted({(topic_of[e["from"]], topic_of[e["to"]]) for e in edges
                            if topic_of.get(e["from"]) and topic_of.get(e["to"])
                            and topic_of[e["from"]] != topic_of[e["to"]]}):
        if not reachable(tadj, tc, tp):
            tadj[tp].add(tc)
            topic_edges.append({"from": tp, "to": tc, "relation": "depends_on"})

    graph = {"nodes": ids, "edges": edges, "_adj": {k: sorted(v) for k, v in adj.items()},
             "topic_nodes": sorted({c["topic_id"] for c in inv}), "topic_edges": topic_edges,
             "_topic_adj": {k: sorted(v) for k, v in tadj.items()},
             "assumed_prior": assumed, "acyclic": True}

    # applied-skill promotion (majority): add procedurality the regex floor missed; never demote.
    promoted = {cid for cid, v in skill_votes.items() if v >= MAJORITY}
    new_inv, added = [], []
    for c in inv:
        c2 = dict(c)
        if c2["concept_id"] in promoted and not c2.get("procedural"):
            c2["procedural"] = True
            added.append(c2["concept_id"])
        new_inv.append(c2)

    logs.append({"node": "build_graph", "k": K_SAMPLES, "per_concept": True, "edges": len(edges),
                 "assumed_prior": assumed, "llm_procedural_promoted": added})
    prog.done("build_dependency_graph",
              detail=f"{len(ids)} nodes, {len(edges)} edges, +{len(added)} apply-skill (per-concept)")
    return {"concept_graph": graph, "concept_inventory": new_inv, "log": logs}


# ── Node 4.5 · profile_coverage (A · breadth x depth, pre-authoring) ──────── #
# Builds the coverage profile BEFORE authoring so LOs are bounded by what the material
# actually teaches: per concept it scores taught DEPTH (LLM, deterministic fallback) and
# confirms the concept is genuinely taught in-course (RAG scope-closure drops external /
# named-in-passing terms). depth_category then sets each concept's allowed-verb ceiling
# (lo_config.allowed_verbs_for), which plan_allocation and author_outcomes obey — so depth
# is established ONCE here instead of being reverse-engineered by V12/coverage_gate repairs.
_DEPTH_PROFILE_SYS = register("lo.depth_profile", (
    "You score how DEEPLY one concept is taught in the given section, for assessment "
    "planning. Read the concept and its section, then classify the taught DEPTH as exactly "
    "one of:\n"
    "- mention: only named or stated, in passing or a single sentence, with no real "
    "explanation (supports only recall: identify/list/define).\n"
    "- moderate: explained with some reasoning or detail across a few sentences (supports "
    "understand: explain/describe), but not thoroughly developed.\n"
    "- deep: thoroughly developed — explanation PLUS examples, steps, or an explicit "
    "contrast/comparison (supports compare/differentiate and, if procedural, apply).\n"
    "Judge ONLY by what THIS material actually teaches; never credit outside knowledge.\n"
    'Return ONLY JSON: {"depth": "mention|moderate|deep", "why": "<one line>"}.'
))


def _profile_one(concept: dict, section_text: str) -> dict:
    name = concept.get("canonical_name", "")
    ev = (concept.get("evidence") or {}).get("quote", "")
    usr = (f'CONCEPT: {name}\n\nEVIDENCE (where it was drawn from):\n"{ev}"\n\n'
           f"SECTION TEXT:\n{(section_text or '')[:8000]}")
    data = {}
    try:
        data = parse_json(chat([{"role": "system", "content": get_prompt("lo.depth_profile", _DEPTH_PROFILE_SYS)},
                                {"role": "user", "content": usr}], temperature=0)) or {}
    except Exception:  # noqa: BLE001 — LLM down: fall back to the deterministic depth heuristic
        data = {}
    depth = str(data.get("depth", "")).strip().lower()
    if depth not in DEPTH_CATEGORIES:
        d = concept_depth(name, section_text)
        depth = "mention" if d <= 1 else ("moderate" if d <= 3 else "deep")
    return {"depth_category": depth, "depth_why": str(data.get("why", ""))[:200]}


def profile_coverage(state, config) -> dict:
    """Pre-authoring breadth+depth profile. Per concept (concurrent): LLM depth score +
    best-effort RAG scope-closure. Attaches depth_category, drops out-of-scope externals
    (in_scope=False), and emits a coverage_profile manifest."""
    _bind_rag(config)
    prog = _prog(config)
    inv = [dict(c) for c in state["concept_inventory"]]
    sec_text = {s["topic_id"]: s.get("text", "") for s in state.get("sections", [])}
    on_done = prog.counter("profile_coverage", len(inv))

    def _one(c):
        prof = _profile_one(c, sec_text.get(c.get("topic_id"), ""))
        external = False
        try:  # scope-closure: is this concept actually taught in the course scope?
            verdict = (rag_api.check_concept(c["canonical_name"]).get("verdict") or "").split("\n", 1)[0].upper()
            external = "NOT EXPLAINED" in verdict
        except Exception:  # noqa: BLE001 — RAG down: never drop a concept on a failed check
            external = False
        on_done()
        return (c["concept_id"], prof, external)

    by_id = {cid: (prof, ext) for cid, prof, ext in pmap(_one, inv)}
    named_only = []
    for c in inv:
        prof, ext = by_id.get(c["concept_id"], ({"depth_category": "moderate", "depth_why": ""}, False))
        c["depth_category"] = prof["depth_category"]
        c["depth_why"] = prof["depth_why"]
        # Quality model: taught_depth + explained describe HOW the session teaches the
        # concept. "mention" = named/stated only, no real explanation → not explained.
        c["taught_depth"] = prof["depth_category"]
        c["explained"] = prof["depth_category"] != "mention"
        if ext:
            c["in_scope"] = False
            c["out_of_scope_reason"] = "named in passing; not taught in course scope (external)"
        elif DROP_NAMED_ONLY and not c["explained"] and c["in_scope"]:
            # Identify-but-don't-explain: a bare mention is not assessable, so it must NOT
            # seed an outcome. Drop it from scope rather than mint a recall LO on a name.
            c["in_scope"] = False
            c["out_of_scope_reason"] = "named in passing; not substantively explained in this session"
            named_only.append(c["concept_id"])

    cats = Counter(c["depth_category"] for c in inv if c["in_scope"])
    external = [c["concept_id"] for c in inv if not c["in_scope"] and c["concept_id"] not in named_only]
    manifest = {"by_depth": dict(cats), "in_scope": sum(1 for c in inv if c["in_scope"]),
                "dropped_external": external, "dropped_named_only": named_only}
    prog.done("profile_coverage",
              detail=f"{manifest['in_scope']} in-scope {dict(cats)}; "
                     f"dropped {len(external)} external, {len(named_only)} named-only")
    return {"concept_inventory": inv, "coverage_profile": manifest,
            "log": [{"node": "profile_coverage", **manifest}]}


# ── Node 5 · plan_allocation (D + feasibility branch) ─────────────────────── #
def plan_allocation(state, config) -> dict:
    prog = _prog(config)
    prog.start("plan_allocation")
    inv = [dict(c) for c in state["concept_inventory"]]   # copy — we add apply_suitable
    topics = state["sections"]
    by_topic = defaultdict(list)
    for c in inv:
        by_topic[c["topic_id"]].append(c)
    in_scope_ids = {c["concept_id"] for c in inv if c["in_scope"]}
    # Adaptive budget — never pad a thin session to the full QUESTION_BUDGET (padding
    # forces invention of ungrounded outcomes). Flows into the split, V1/V2/V3 and the
    # finalized artifact's question_budget.
    budget = max(MIN_BUDGET, min(QUESTION_BUDGET, len(in_scope_ids) * MAX_LOS_PER_CONCEPT))
    weights = [max(1, len(by_topic[t["topic_id"]])) for t in topics]
    slots = largest_remainder(weights, budget)

    # Concept-anchored code presence: a concept "has code to apply" only when its OWN
    # name tokens appear inside a fenced block of its topic — not merely because the
    # topic contains some unrelated fence (the old `sec_has_code` rubber-stamp).
    named_in_fence = {}
    for t in topics:
        fences = "\n".join(_FENCE_RE.findall(t["text"])).lower()
        named_in_fence[t["topic_id"]] = {
            c["concept_id"] for c in by_topic[t["topic_id"]]
            if (toks := set(re.findall(r"[a-z_][a-z0-9_]{2,}", c["canonical_name"].lower())))
            and any(tok in fences for tok in toks)
        }
    # The setup/CLI exclusion below is a PROGRAMMING-domain heuristic; gate it to
    # code-bearing sections so it can't mis-fire on another domain (e.g. "shared
    # environment" in genetics, "shell" in chemistry, "terminal" in anatomy).
    has_code_by_topic = {t["topic_id"]: bool(t.get("has_code") or "```" in (t.get("text") or ""))
                         for t in topics}
    graph_state = {"concept_graph": state["concept_graph"], "concept_inventory": inv}

    def _apply_suitable(c):
        if not c["procedural"]:
            return False
        if c.get("depth_category", "moderate") == "mention":
            return False   # a barely-mentioned concept can't support an apply outcome
        # Installation / shell / environment-setup concepts are procedural-looking but
        # have no runnable, deterministically-gradable skill — assess them via MCQ, never
        # apply / FIB. ONLY in code-bearing sections, so it stays domain-safe.
        if has_code_by_topic.get(c["topic_id"]) and \
                is_setup_or_cli(c.get("evidence", {}).get("quote", ""), c["canonical_name"]):
            return False
        prereqs = graph_find_prerequisites(graph_state, c["concept_id"])
        return (any(p in in_scope_ids for p in prereqs)
                or c["concept_id"] in named_in_fence.get(c["topic_id"], set()))

    for c in inv:
        c["apply_suitable"] = _apply_suitable(c)

    proc_count = {t["topic_id"]: sum(1 for c in by_topic[t["topic_id"]] if c["apply_suitable"]) for t in topics}
    cap_apply = {t["topic_id"]: min(slots[t["order"]], proc_count[t["topic_id"]]) for t in topics}
    apply_capacity = sum(cap_apply.values())

    # Split is derived from the (possibly reduced) budget; apply is a ceiling capped by
    # genuine apply capacity, so remember_understand + apply always sums to the budget.
    ap_total = min(DEFAULT_SPLIT["apply"], apply_capacity)
    split = {"apply": ap_total, "remember_understand": budget - ap_total}
    overrides, logs = [], []
    if budget != QUESTION_BUDGET or ap_total != DEFAULT_SPLIT["apply"]:
        ov = {"rule": "bloom_split",
              "from": f'{DEFAULT_SPLIT["remember_understand"]}/{DEFAULT_SPLIT["apply"]} of {QUESTION_BUDGET}',
              "to": f'{split["remember_understand"]}/{split["apply"]} of {budget}',
              "reason": f"adaptive budget {budget} for {len(in_scope_ids)} grounded concept(s); "
                        f"{apply_capacity} apply-suitable"}
        overrides.append(ov)
        logs.append({"node": "plan_allocation", "override": ov})

    apply_left = min(split["apply"], apply_capacity)
    apply_by_topic = {t["topic_id"]: 0 for t in topics}
    ranked = sorted(topics, key=lambda t: (-proc_count[t["topic_id"]], t["topic_id"]))
    progressed = True
    while apply_left > 0 and progressed:
        progressed = False
        for t in ranked:
            tid = t["topic_id"]
            if apply_left > 0 and apply_by_topic[tid] < cap_apply[tid]:
                apply_by_topic[tid] += 1
                apply_left -= 1
                progressed = True
            if apply_left == 0:
                break

    plan = {}
    for t in topics:
        tid, n = t["topic_id"], slots[t["order"]]
        ap = apply_by_topic[tid]
        plan[tid] = {"topic_id": tid, "title": t["title"], "slots": n,
                     "bloom": {"remember_understand": n - ap, "apply": ap}}
        if n == 0:
            logs.append({"node": "plan_allocation", "topic": tid, "slots": 0, "note": "more topics than budget"})
    allocation = {"by_topic": plan, "effective_split": split,
                  "apply_suitable_total": apply_capacity, "question_budget": budget}
    prog.done("plan_allocation", detail=f"split {split['remember_understand']}/{split['apply']}")
    return {"concept_inventory": inv, "allocation_plan": allocation, "overrides": overrides, "log": logs}


# ── Node 6 · author_outcomes (A · 1 call per topic) ───────────────────────── #
def _coerce_outcome(item: dict, topic: dict, valid_ids: set, inv: list) -> dict:
    lvl = "apply" if str(item.get("bloom_level", "")).lower().startswith("appl") else "remember_understand"
    verb = str(item.get("learner_action", "")).lower().strip()
    if verb not in VERBS[lvl]:
        verb = "execute" if lvl == "apply" else "explain"
    cid = item.get("concept_id")
    if cid not in valid_ids:
        cid = inv[0]["concept_id"]
    if lvl == "apply":
        proc = [c["concept_id"] for c in inv if c.get("apply_suitable")]
        cur = next((c for c in inv if c["concept_id"] == cid), None)
        if proc and (cur is None or not cur.get("apply_suitable")):
            cid = proc[0]
    # Clamp RU verbs to the concept's depth ceiling (apply is already capacity-bounded by
    # apply_suitable, so leave it). Keeps bloom_level unchanged -> the planned split holds.
    if lvl == "remember_understand":
        cur2 = next((c for c in inv if c["concept_id"] == cid), None)
        if cur2 is not None:
            allowed = allowed_verbs_for(cur2.get("depth_category", "moderate"), cur2.get("apply_suitable", False))
            if verb not in allowed:
                verb = "explain" if "explain" in allowed else "identify"
    title = (item.get("title") or f"{verb} {topic['title']}").strip()
    skill = item.get("skill_type")
    if skill not in SKILL_TYPES:
        skill = "practical_application" if lvl == "apply" else "conceptual"
    cname = next((c["canonical_name"] for c in inv if c["concept_id"] == cid), title)
    quote = ground_quote(cname, topic["text"])
    return {"id": slugify(f"{verb}_{cid[2:]}"), "title": title, "topic_id": topic["topic_id"],
            "concept_id": cid, "bloom_level": lvl, "skill_type": skill, "learner_action": verb,
            "description": (item.get("description") or title).strip(),
            "syntax": (item.get("syntax") or None),
            "prerequisites": [], "prerequisite_scope": None, "target_questions": 1,
            "source_evidence": {"quote": quote, "section": topic["topic_id"]},
            "justification": (item.get("justification") or "Grounded in section evidence.").strip()}


def _reconcile_counts(items: list, topic: dict, plan_row: dict, inv: list) -> list:
    """Force exactly the planned RU/Apply counts (drop extras, synthesize shortfalls)."""
    want = plan_row["bloom"]
    final = []
    used = {o["concept_id"] for o in items}   # prefer concepts not already covered
    for lvl in ("remember_understand", "apply"):
        pool = [o for o in items if o["bloom_level"] == lvl][:want[lvl]]
        used |= {o["concept_id"] for o in pool}
        choices = [c for c in inv if c.get("apply_suitable")] if lvl == "apply" else inv
        choices = choices or inv
        while len(pool) < want[lvl]:
            # synthesize a filler on an UNCOVERED concept first; only restack once
            # every concept is used (avoids 3-6 near-duplicate outcomes per concept).
            pick = next((c for c in choices if c["concept_id"] not in used), None)
            cid = (pick or choices[len(pool) % len(choices)])["concept_id"]
            used.add(cid)
            # 'identify' is always within the verb ceiling at any depth -> safe filler.
            verb = "execute" if lvl == "apply" else "identify"
            name = next(c["canonical_name"] for c in inv if c["concept_id"] == cid)
            pool.append({"id": slugify(f"{verb}_{cid[2:]}_{len(pool)}"),
                         "title": f"{verb.title()} {name}", "topic_id": topic["topic_id"],
                         "concept_id": cid, "bloom_level": lvl,
                         "skill_type": "practical_application" if lvl == "apply" else "conceptual",
                         "learner_action": verb, "description": f"{verb.title()} {name}.",
                         "syntax": None, "prerequisites": [], "prerequisite_scope": None,
                         "target_questions": 1,
                         "source_evidence": {"quote": ground_quote(name, topic["text"]),
                                             "section": topic["topic_id"]},
                         "justification": "Synthesized to meet planned slot count."})
        final.extend(pool)
    return final


# DB-overridable (sentinel placeholders are substituted in code — NOT str.format,
# because the prompt contains literal JSON braces).
_AUTHOR_SYS = register("lo.author_sys", """\
You author measurable LEARNING OUTCOMES for one topic, grounded ONLY in the given concepts/evidence. Return ONLY a JSON list. Each item: {"title","concept_id","bloom_level","learner_action","skill_type","description","syntax","source_evidence":{"quote","section"},"justification"}.
Write EXACTLY <N_RU> outcome(s) with bloom_level "remember_understand" and EXACTLY <N_AP> with bloom_level "apply".
For remember_understand the learner_action MUST be one of: <RU_VERBS>. For apply it MUST be one of: <AP_VERBS>.
Apply outcomes must require genuine application of a procedural concept (not recall reworded). concept_id MUST be one of the given ids.

SELF-CONTAINED & TRANSFERABLE — each outcome's "title" and "description" MUST state the GENERAL concept or skill so they stand on their own, independent of this particular reading. NEVER reference an example-specific or source-local entity from the material: a scenario label ("Project A"/"Project B"), a sample variable/file/function name, a character, or a one-off sample value. The reading's example is supporting EVIDENCE, not the thing assessed — GENERALIZE it. Write "Explain why two projects that need different versions of the same library cannot share one global installation", NOT "Identify the version of Django required for Project A". Technologies/tools/commands genuinely taught by name (e.g. "venv", "Django") MAY be named.

CRISP & DISTINCT — each outcome must target ONE specific concept with a SINGLE unambiguous correct answer. Avoid broad/umbrella outcomes ("set up a project") that admit several defensible answers. Within this topic, no two outcomes may be interchangeable (the same idea under a different verb); each must assess a distinct concept or sub-aspect.

GROUNDED IN TAUGHT DEPTH — the verb must match how much the material actually TEACHES the concept, not merely that it names it. A concept covered only as a brief MENTION or one-line OVERVIEW (named in a list, a single descriptive sentence, no elaboration) supports ONLY low-level recall: identify / list / recognize / label / name / define. Do NOT use explain / describe / summarize / interpret / illustrate, comparison verbs (compare / distinguish / differentiate), or apply / implement for such thinly-covered concepts.
COMPARISON outcomes (compare / distinguish / differentiate) are valid ONLY when the material EXPLICITLY contrasts the items along the assessed dimension (states how they differ). Never manufacture a comparison from items the material only mentions separately. Example: given "Flask is lightweight…" and "FastAPI is fast and asynchronous…" as two separate one-liners, author "Identify the best use case for FastAPI", NOT "Differentiate Flask and FastAPI by their lightweight and asynchronous capabilities".

VERB CEILING (HARD) — each concept below is annotated with [taught depth=...; allowed verbs: ...], derived from how deeply the material actually teaches it. An outcome's learner_action MUST be one of ITS OWN concept's allowed verbs — NEVER exceed that ceiling. And NEVER reference an external resource, tool, framework, dataset, or terminology that is not present in THIS material (no out-of-box concepts, no links to other resources).

'syntax' is a code/command reference COPIED VERBATIM from the section text (null if the section has no code/command for it — do NOT invent or guess syntax). 'quote' must be copied verbatim from the evidence. Do not add commentary.""")


def _author_topic(state: dict, topic: dict, plan_row: dict) -> list:
    # Author ONLY against in-scope (substantively explained) concepts — a named-only or
    # external concept was dropped by profile_coverage and must not seed an outcome.
    topic_concepts = [c for c in state["concept_inventory"] if c["topic_id"] == topic["topic_id"]]
    inv = [c for c in topic_concepts if c.get("in_scope", True)] or topic_concepts \
        or [c for c in state["concept_inventory"] if c.get("in_scope", True)] \
        or state["concept_inventory"]
    concept_lines = "\n".join(
        f'{c["concept_id"]}: {c["canonical_name"]} '
        f'[taught depth={c.get("depth_category", "moderate")}; allowed verbs: '
        f'{sorted(allowed_verbs_for(c.get("depth_category", "moderate"), c.get("apply_suitable", False)))}] '
        f'— {(c.get("description") or "").strip()[:240]} '
        f'(evidence: "{c["evidence"]["quote"][:120]}")' for c in inv)
    n_ru, n_ap = plan_row["bloom"]["remember_understand"], plan_row["bloom"]["apply"]
    sys = (get_prompt("lo.author_sys", _AUTHOR_SYS)
           .replace("<N_RU>", str(n_ru))
           .replace("<N_AP>", str(n_ap))
           .replace("<RU_VERBS>", str(sorted(VERBS["remember_understand"])))
           .replace("<AP_VERBS>", str(sorted(VERBS["apply"]))))
    usr = (f"TOPIC: {topic['title']} ({topic['topic_id']}, section_text_len="
           f"{len(topic['text'])})\n\nCONCEPTS:\n{concept_lines}")
    data = parse_json(chat([{"role": "system", "content": sys},
                            {"role": "user", "content": usr}], temperature=TEMP_AUTHOR)) or []
    valid_ids = {c["concept_id"] for c in inv}
    out = [_coerce_outcome(item, topic, valid_ids, inv) for item in (data if isinstance(data, list) else [])]
    return _reconcile_counts(out, topic, plan_row, inv)


def author_outcomes(state, config) -> dict:
    _bind_rag(config)
    prog = _prog(config)
    plan = state["allocation_plan"]["by_topic"]
    work = [t for t in state["sections"] if plan[t["topic_id"]]["slots"] > 0]
    on_done = prog.counter("author_outcomes", len(work))

    def _one(topic):
        rows = _author_topic(state, topic, plan[topic["topic_id"]])
        on_done()
        return rows

    results = pmap(_one, work)
    outcomes = [o for rows in results for o in rows]
    seen = Counter()
    for o in outcomes:
        seen[o["id"]] += 1
        if seen[o["id"]] > 1:
            o["id"] = f'{o["id"]}_{seen[o["id"]]}'
    prog.done("author_outcomes", detail=f"{len(outcomes)} outcomes")
    return {"outcomes": outcomes}


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
        if o["bloom_level"] != "apply":
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


# ── Node 7.5 · coverage_gate (A · STRICT coverage rubric) ─────────────────── #
# A dedicated agent that scores each authored outcome against an explicit COVERAGE RUBRIC
# (lo.coverage_rubric): could a student answer a question built from this outcome using
# ONLY the material? It STRICTLY rejects the core failure — the concept is covered, but
# the outcome reaches PAST what is taught (unanswerable for the learner). Verdicts feed
# validate as V13. One call per outcome (concurrent, temp 0); cached by signature so the
# repair loop only re-scores what changed; degrades to "covered" if the LLM is
# unavailable so it never blocks a run.
_COVERAGE_RUBRIC = register("lo.coverage_rubric", (
    "You score ONE learning outcome against the reading material with a STRICT COVERAGE "
    "RUBRIC. A student will see ONLY a question built from this outcome and must answer it "
    "using ONLY this material — no outside knowledge. For EACH criterion decide PASS/FAIL:\n"
    "R1 PRESENT — the concept is explicitly TAUGHT here (not merely named in passing, "
    "alluded to, or assumed from prior knowledge).\n"
    "R2 DEPTH MATCHES DEMAND — the material teaches it to the depth the outcome's verb "
    "requires: recall (identify/list/define) needs the fact stated; understand "
    "(explain/describe) needs the explanation/reason given, not just the term; "
    "compare/differentiate needs the material to EXPLICITLY contrast the items on the "
    "stated dimension; apply (apply/compute/solve/construct/...) needs the method, steps, "
    "or a worked example shown.\n"
    "R3 ANSWERABLE FROM MATERIAL ALONE — a typical learner who read ONLY this material can "
    "determine the answer with certainty: no outside knowledge, no inference the material "
    "does not make, no specific values/cases the material does not give.\n"
    "R4 NO BEYOND-SCOPE LEAP — the outcome does NOT require any detail, value, case, "
    "edge-case, comparison, or sub-topic the material does not actually cover. (THE key "
    "failure to catch: the concept is covered, but the outcome reaches past what is taught.)\n"
    "R5 ANSWER KEY DERIVABLE — the single correct answer (and why plausible wrong options "
    "are wrong) is derivable from the material.\n\n"
    "Judge THIS ONE outcome in ISOLATION, on its own merits — do NOT assume coverage "
    "carried over from related concepts or from other outcomes.\n"
    "Apply the rubric STRICTLY: the DEFAULT verdict is FAIL. Pass a criterion ONLY when "
    "the material PLAINLY and DIRECTLY supplies what is needed — a mere mention is NOT "
    "coverage. Any doubt, gap, required inference, or 'probably covered' means FAIL. Give "
    "NO benefit of the doubt.\n"
    'Return ONLY JSON: {"R1_present": <bool>, "R2_depth": <bool>, "R3_answerable": <bool>, '
    '"R4_in_scope": <bool>, "R5_answer_key": <bool>, "beyond_coverage_reason": "<one line: '
    "exactly what the outcome asks that the material does not cover, or empty if fully "
    'covered>", "suggested_recall_title": "<a lower outcome the material DOES fully '
    'support, or empty>"}.'
))

_COVERAGE_SRC_CAP = 12000
_RUBRIC_KEYS = ("R1_present", "R2_depth", "R3_answerable", "R4_in_scope", "R5_answer_key")


def _outcome_sig(o: dict) -> str:
    return "|".join(str(o.get(k)) for k in ("id", "learner_action", "bloom_level", "concept_id", "title"))


def _score_coverage(outcome: dict, section_text: str, source_text: str) -> dict:
    compact = {k: outcome.get(k) for k in ("title", "bloom_level", "learner_action", "description")}
    ev = (outcome.get("source_evidence") or {}).get("quote", "")
    usr = (f"OUTCOME:\n{json.dumps(compact, ensure_ascii=False)}\n\n"
           f'CITED EVIDENCE (the exact span this outcome was drawn from):\n"{ev}"\n\n'
           f"SECTION it was drawn from (the coverage scope — judge mainly against this):\n"
           f"{(section_text or '')[:_COVERAGE_SRC_CAP]}\n\n"
           f"REST OF THE READING (background only):\n{(source_text or '')[:_COVERAGE_SRC_CAP]}")
    try:
        data = parse_json(chat([{"role": "system", "content": get_prompt("lo.coverage_rubric", _COVERAGE_RUBRIC)},
                                {"role": "user", "content": usr}], temperature=0)) or {}
    except Exception:  # noqa: BLE001 — LLM down: never block the run; treat as covered
        return {"covered": True, "rubric": {}, "beyond_coverage_reason": "coverage check unavailable",
                "suggested_recall_title": ""}
    rubric = {k: bool(data.get(k, True)) for k in _RUBRIC_KEYS}   # omitted key -> pass (lenient only on omission)
    return {"covered": all(rubric.values()), "rubric": rubric,
            "beyond_coverage_reason": str(data.get("beyond_coverage_reason", ""))[:300],
            "suggested_recall_title": str(data.get("suggested_recall_title", ""))[:160]}


def coverage_gate(state, config) -> dict:
    """STRICT coverage gate: score each authored outcome against the coverage rubric,
    SEQUENTIALLY — one outcome at a time, each its own isolated focused call, never
    batched or judged together (deliberately NOT concurrent). Re-scores only outcomes
    whose signature changed (so the repair loop is cheap). Returns lo_reviews keyed by
    outcome id; validate() reads it as V13."""
    prog = _prog(config)
    ctx = _ctx(config)
    if ctx is not None and not getattr(ctx, "run_coverage_gate", True):
        prog.done("coverage_gate", detail="skipped")
        return {}
    outcomes = state["outcomes"]
    prev = state.get("lo_reviews") or {}
    todo = [o for o in outcomes if (prev.get(o["id"]) or {}).get("_sig") != _outcome_sig(o)]
    if not todo:
        prog.done("coverage_gate", detail="no changes")
        return {}
    on_done = prog.counter("coverage_gate", len(todo))
    sec_text = {s["topic_id"]: s.get("text", "") for s in state.get("sections", [])}
    src = state["source_text"]

    # ONE outcome at a time — strict, isolated, sequential (deliberately NOT pmap), so each
    # is judged on its own merits with no cross-outcome bleed.
    fresh = {}
    for o in todo:
        v = _score_coverage(o, sec_text.get(o.get("topic_id"), ""), src)
        v["_sig"] = _outcome_sig(o)
        fresh[o["id"]] = v
        on_done()

    merged = {**prev, **fresh}
    merged = {o["id"]: merged[o["id"]] for o in outcomes if o["id"] in merged}   # drop stale ids
    failed = sum(1 for v in merged.values() if not v.get("covered", True))
    prog.done("coverage_gate", detail=f"{failed} beyond-coverage of {len(outcomes)}")
    return {"lo_reviews": merged}


# ── Node 8 · validate (D · rules V1–V12 + V13 reads the LLM review verdicts) ─ #
def _loosen(t: str) -> str:
    return loosen_text(t)


def validate(state, config) -> dict:
    prog = _prog(config)
    prog.start("validate")
    O = state["outcomes"]
    plan = state["allocation_plan"]
    rep: dict = {}
    src = _loosen(state["source_text"])
    inv_ids = {c["concept_id"] for c in state["concept_inventory"] if c["in_scope"]}
    budget = plan.get("question_budget", QUESTION_BUDGET)
    topic_text = {s["topic_id"]: s["text"] for s in state["sections"]}

    def rule(rid, ok, detail="", items=None):
        rep[rid] = {"pass": bool(ok), "detail": detail, "failing": items or []}

    rule("V1", len(O) == budget, f"count={len(O)} (want {budget})")
    eff = plan["effective_split"]
    got = {"apply": sum(o["bloom_level"] == "apply" for o in O)}
    got["remember_understand"] = len(O) - got["apply"]
    rule("V2", got == eff, f"got={got} eff={eff}")
    bad = []
    for tid, p in plan["by_topic"].items():
        cnt = sum(o["topic_id"] == tid for o in O)
        if cnt != p["slots"]:
            bad.append({"topic": tid, "got": cnt, "want": p["slots"]})
    rule("V3", not bad, "per-topic slot mismatch", bad)
    covered = {o["concept_id"] for o in O}
    missing = sorted(inv_ids - covered)
    rule("V4", not missing, "uncovered in-scope concepts", missing)
    # V14 — explained-only: an outcome must target a concept that is in scope (substantively
    # explained, not a bare mention or external term). Author/reconcile guarantee this by
    # construction; the rule is the safety net + observability (repair retargets offenders).
    off_scope = [o["id"] for o in O if o["concept_id"] not in inv_ids]
    rule("V14", not off_scope, "outcome targets a non-explained (out-of-scope) concept", off_scope)
    no_pre = [o["id"] for o in O if o["bloom_level"] == "apply" and not o["prerequisites"]]
    rule("V5", not no_pre, "apply outcome with empty prerequisite set", no_pre)
    oos = [o["id"] for o in O if o["bloom_level"] == "apply" and o["prerequisite_scope"] == "has_out_of_scope"]
    rule("V6", not oos, "apply prerequisite closure out of scope", oos)
    rule("V7", state["concept_graph"]["acyclic"], "DAG acyclicity")
    proc = {c["concept_id"]: c.get("apply_suitable", c["procedural"]) for c in state["concept_inventory"]}
    fake = [o["id"] for o in O if o["bloom_level"] == "apply"
            and (o["learner_action"] not in APPLY_VERBS or not proc.get(o["concept_id"], False))]
    rule("V8", not fake, "fake-apply (non-apply verb or non-procedural concept)", fake)
    ungrounded = [o["id"] for o in O
                  if not o["source_evidence"]["quote"].strip()
                  or _loosen(o["source_evidence"]["quote"])[:60] not in src]
    rule("V9", not ungrounded, "source_evidence not found in source", ungrounded)
    badverb = [o["id"] for o in O if o["learner_action"] not in VERBS[o["bloom_level"]]]
    rule("V10", not badverb, "action verb outside controlled vocabulary", badverb)
    badsyntax = [o["id"] for o in O if o["bloom_level"] == "apply" and (
        (o.get("syntax") and not syntax_grounded(o["syntax"], state["source_text"]))
        or (not o.get("syntax") and "```" in topic_text.get(o["topic_id"], ""))
    )]
    rule("V11", not badsyntax,
         "apply syntax not grounded, or null on a fenced topic (repair attaches a grounded command)",
         badsyntax)
    # V12 — deterministic depth FLOOR, fallback-only. The coverage_gate (R2/V13) is the
    # binding depth judge; V12 now evaluates ONLY outcomes the gate did NOT score (gate
    # disabled or LLM unavailable), so the two never double-judge/disagree, yet a depth
    # floor remains when the gate is off. Same heuristic: comparison needs a taught
    # contrast; other elaboration verbs need the concept taught in >1 sentence.
    scored = state.get("lo_reviews") or {}
    cname_by_id = {c["concept_id"]: c["canonical_name"] for c in state["concept_inventory"]}
    overreach = []
    for o in O:
        if o["bloom_level"] != "remember_understand":
            continue
        if o["id"] in scored:          # coverage_gate R2 owns depth for scored outcomes
            continue
        cname = cname_by_id.get(o["concept_id"], o.get("title", ""))
        text = f'{o.get("title", "")} {o.get("description", "")} {cname}'
        verb = o["learner_action"]
        # Judge against the section the outcome was drawn from (it already CONTAINS the
        # cited evidence — don't concatenate the quote, which would double-count depth).
        scope_text = topic_text.get(o["topic_id"]) or state["source_text"]
        if verb in COMPARISON_VERBS:
            if not has_explicit_contrast(text, scope_text):
                overreach.append(o["id"])
        elif verb not in LOW_DEMAND_VERBS:
            if concept_depth(cname, scope_text) <= 1:
                overreach.append(o["id"])
    rule("V12", not overreach,
         "outcome over-reaches the taught depth (a comparison the material does not "
         "explicitly contrast, or elaboration of a one-line mention)", overreach)
    # V13 — STRICT coverage gate: the coverage_gate agent scored each outcome against the
    # coverage rubric; fail any it marks NOT covered (the concept is taught but the outcome
    # reaches PAST what's covered -> a student can't answer it). Pure read; absent verdicts
    # (gate disabled or LLM down) never flag.
    reviews = state.get("lo_reviews") or {}
    not_covered = [o["id"] for o in O if (rv := reviews.get(o["id"])) and not rv.get("covered", True)]
    rule("V13", not not_covered,
         "coverage rubric: outcome asks beyond what the material covers (unanswerable from it)",
         not_covered)

    failed = [k for k, v in rep.items() if not v["pass"]]
    log = {"node": "validate", "attempt": state.get("retry_count", 0), "failed": failed}
    prog.done("validate", detail="pass" if not failed else f"failing: {failed}")
    return {"validation_report": rep, "log": [log]}


# ── Node 9 · repair (A · loop) ────────────────────────────────────────────── #
# DB-overridable (sentinel placeholders substituted in code).
_REPAIR_SYS = register("lo.repair_sys", (
    "Rewrite ONE learning outcome to fix the listed validation failures. "
    "Return ONLY one JSON object with the same keys. bloom_level MUST stay "
    '"<BLOOM_LEVEL>"; learner_action MUST be one of <VERBS>; '
    "concept_id MUST be one of the given ids; the evidence 'quote' MUST be "
    "copied verbatim from the section text. The 'title' and 'description' MUST be "
    "self-contained and transferable — state the general concept and NEVER reference "
    'an example-specific or source-local entity (e.g. "Project A"/"Project B", a '
    "sample variable/file name, a character, or a one-off sample value)."
))


def _topic_of(state, tid):
    return next(t for t in state["sections"] if t["topic_id"] == tid)


def repair(state, config) -> dict:
    _bind_rag(config)
    prog = _prog(config)
    prog.start("repair", detail=f"attempt {state.get('retry_count', 0) + 1}")
    rep = state["validation_report"]
    outcomes = [dict(o) for o in state["outcomes"]]
    by_id = {o["id"]: o for o in outcomes}
    logs = []

    # 1) coverage gaps (V4): retarget an over-covered concept's outcome to the missing one
    missing = list(rep.get("V4", {}).get("failing", []))
    cover = Counter(o["concept_id"] for o in outcomes)
    for mid in missing:
        donor = next((o for o in outcomes if cover[o["concept_id"]] > 1
                      and o["bloom_level"] == "remember_understand"), None)
        donor = donor or next((o for o in outcomes if cover[o["concept_id"]] > 1), None)
        if not donor:
            break
        cover[donor["concept_id"]] -= 1
        cover[mid] += 1
        c = next(c for c in state["concept_inventory"] if c["concept_id"] == mid)
        donor.update({"concept_id": mid, "topic_id": c["topic_id"],
                      "title": f"Explain {c['canonical_name']}", "learner_action": "explain",
                      "bloom_level": "remember_understand", "description": f"Explain {c['canonical_name']}.",
                      "source_evidence": {"quote": ground_quote(c["canonical_name"],
                                          _topic_of(state, c["topic_id"])["text"]),
                                          "section": c["topic_id"]},
                      "justification": "Repaired to close a coverage gap.",
                      "prerequisites": [], "prerequisite_scope": None})
        logs.append({"node": "repair", "fix": "coverage", "concept": mid, "via": donor["id"]})

    # 1a) explained-only (V14): an outcome on an out-of-scope (named-only/external) concept
    # is retargeted onto an in-scope concept — prefer an uncovered one, else any in-scope.
    in_scope = [c for c in state["concept_inventory"] if c.get("in_scope")]
    if in_scope:
        in_scope_ids = {c["concept_id"] for c in in_scope}
        for oid in rep.get("V14", {}).get("failing", []):
            o = by_id.get(oid)
            if not o or o["concept_id"] in in_scope_ids:
                continue
            tgt = next((c for c in in_scope if cover[c["concept_id"]] == 0), None) or \
                next((c for c in in_scope if c["concept_id"] != o["concept_id"]), in_scope[0])
            cover[o["concept_id"]] -= 1
            cover[tgt["concept_id"]] += 1
            o.update({"concept_id": tgt["concept_id"], "topic_id": tgt["topic_id"],
                      "title": f"Identify {tgt['canonical_name']}", "learner_action": "identify",
                      "bloom_level": "remember_understand",
                      "description": f"Identify {tgt['canonical_name']}.",
                      "source_evidence": {"quote": ground_quote(tgt["canonical_name"],
                                          _topic_of(state, tgt["topic_id"])["text"]),
                                          "section": tgt["topic_id"]},
                      "justification": "Repaired: retargeted off a non-explained concept.",
                      "prerequisites": [], "prerequisite_scope": None})
            logs.append({"node": "repair", "fix": "explained_only", "id": oid,
                         "retargeted_to": tgt["concept_id"]})

    # 1b) syntax grounding (V11): recover a real snippet, else null (never ship hallucinated)
    for oid in rep.get("V11", {}).get("failing", []):
        o = by_id.get(oid)
        if not o:
            continue
        cand = recover_syntax(_topic_of(state, o["topic_id"])["text"])
        o["syntax"] = cand if (cand and syntax_grounded(cand, state["source_text"])) else None
        logs.append({"node": "repair", "fix": "syntax", "id": oid,
                     "result": "recovered" if o["syntax"] else "nulled"})

    # 1c) over-reach (V12): lower the outcome to a grounded RECALL of its concept — the
    # thin material does not support a comparison/elaboration. Deterministic so the loop
    # converges (re-checked by validate; "identify" is low-demand + in the RU vocabulary).
    for oid in rep.get("V12", {}).get("failing", []):
        o = by_id.get(oid)
        if not o:
            continue
        c = next((c for c in state["concept_inventory"] if c["concept_id"] == o["concept_id"]), None)
        name = c["canonical_name"] if c else (o.get("title") or "this concept")
        o.update({
            "learner_action": "identify", "bloom_level": "remember_understand",
            "skill_type": "conceptual",
            "title": f"Identify {name}", "description": f"Identify {name}.",
            "source_evidence": {"quote": ground_quote(name, _topic_of(state, o["topic_id"])["text"]),
                                "section": o["topic_id"]},
            "justification": "Repaired: lowered to recall — the material covers this concept only briefly.",
            "prerequisites": [], "prerequisite_scope": None,
        })
        logs.append({"node": "repair", "fix": "overreach_v12", "id": oid, "to": "identify"})

    # 1d) coverage-gate failure (V13): the outcome reaches past what the material covers —
    # deterministically lower it to a grounded recall (preferring the gate's suggested
    # in-coverage title when it is itself a recall verb), same shape as the V12 downgrade.
    # NOTE (bounded, intended): if the failing outcome was APPLY, lowering it to RU shifts
    # the realized bloom split, which V2 then flags and repair does not rebalance — so a
    # genuinely thin, apply-heavy session converges (within MAX_RETRIES) to a NEEDS_REVIEW
    # escalation rather than self-healing. That is the safe outcome (a human reviews
    # material that cannot support the planned apply questions); auto-rebalancing the split
    # is a deliberate future enhancement, not a silent ship.
    recall_verbs = LOW_DEMAND_VERBS & VERBS["remember_understand"]
    for oid in rep.get("V13", {}).get("failing", []):
        o = by_id.get(oid)
        if not o:
            continue
        c = next((c for c in state["concept_inventory"] if c["concept_id"] == o["concept_id"]), None)
        name = c["canonical_name"] if c else (o.get("title") or "this concept")
        sugg = (((state.get("lo_reviews") or {}).get(oid) or {}).get("suggested_recall_title") or "").strip()
        first = sugg.split()[0].lower() if sugg else ""
        if first in recall_verbs:
            verb, title = first, sugg
        else:
            verb, title = "identify", f"Identify {name}"
        o.update({
            "learner_action": verb, "bloom_level": "remember_understand", "skill_type": "conceptual",
            "title": title, "description": title if title.endswith(".") else title + ".",
            "source_evidence": {"quote": ground_quote(name, _topic_of(state, o["topic_id"])["text"]),
                                "section": o["topic_id"]},
            "justification": "Repaired: coverage gate found the outcome reaches beyond the material; lowered to a covered recall.",
            "prerequisites": [], "prerequisite_scope": None,
        })
        logs.append({"node": "repair", "fix": "coverage_v13", "id": oid, "verb": verb})

    # 2) item-level failures (V8/V9/V10/V5/V6): re-author the specific outcome
    failing_ids = set()
    for rid in ("V8", "V9", "V10", "V5", "V6"):
        failing_ids.update(rep.get(rid, {}).get("failing", []))
    for oid in list(failing_ids):
        o = by_id.get(oid)
        if not o:
            continue
        topic = _topic_of(state, o["topic_id"])
        inv = [c for c in state["concept_inventory"] if c["topic_id"] == o["topic_id"]] \
            or state["concept_inventory"]
        reasons = [rid for rid in ("V5", "V6", "V8", "V9", "V10")
                   if oid in rep.get(rid, {}).get("failing", [])]
        verbs = sorted(VERBS[o["bloom_level"]])
        sys = (get_prompt("lo.repair_sys", _REPAIR_SYS)
               .replace("<BLOOM_LEVEL>", o["bloom_level"])
               .replace("<VERBS>", str(verbs)))
        usr = (f"FAILED RULES: {reasons}\nCURRENT: {json.dumps(o)}\n"
               f"SECTION TEXT:\n{topic['text'][:2500]}\nVALID concept_ids: "
               f"{[c['concept_id'] for c in inv]}")
        fixed = parse_json(chat([{"role": "system", "content": sys},
                                 {"role": "user", "content": usr}], temperature=TEMP_AUTHOR))
        if isinstance(fixed, dict):
            merged = _coerce_outcome(fixed, topic, {c["concept_id"] for c in inv}, inv)
            merged["id"] = o["id"]
            o.update(merged)
            logs.append({"node": "repair", "fix": "item", "id": oid, "rules": reasons})

    prog.done("repair")
    return {"outcomes": outcomes, "retry_count": state.get("retry_count", 0) + 1, "log": logs}


# ── Node 9.5 · sequence_outcomes (A · deep-dive ordering) ─────────────────── #
# Orders the final questions basic -> advanced as a coherent DEEP DIVE. DOMAIN-GENERAL:
# driven by the prerequisite concept DAG + concept weights + topic order (NOT hardcoded
# verb/Bloom tiers). LLM-primary (prompt lo.sequence_sys); deterministic graph+weight
# sort as the fallback when the LLM is unavailable.
_SEQUENCE_SYS = register("lo.sequence_sys", (
    "You order a set of learning outcomes into the ideal pedagogical sequence — a DEEP DIVE "
    "that starts with the most FOUNDATIONAL/basic and progresses to the most advanced, so "
    "each question builds on what came before. HARD RULE: never place an outcome before one "
    "it depends on (respect the prerequisite edges). Balance the per-outcome signals: "
    "dag_depth (0 = foundational, higher = more advanced — earlier first), weight (higher = "
    "more foundational, earlier), topic_order (the reading's own sequence), and level/depth. "
    "This must work for ANY subject — judge from these signals, not from keywords. Return "
    'ONLY JSON: {"order": ["<outcome_id>", ...]} listing EVERY given id EXACTLY once, first '
    "to last."
))


def _concept_metrics(graph: dict) -> dict:
    """Per-concept (weight, dag_depth) from the prerequisite DAG. _adj[A]=[B,...] means A is
    a prerequisite of B. weight = count of concepts that (transitively) depend on A
    (foundational-ness — higher sorts earlier); dag_depth = longest prerequisite chain
    leading INTO the concept (0 = foundational, higher = more advanced). Acyclic by
    construction; `seen` guards anyway."""
    adj = {k: list(v) for k, v in (graph.get("_adj") or {}).items()}
    nodes = list(dict.fromkeys(list(graph.get("nodes") or [])
                               + list(adj) + [b for vs in adj.values() for b in vs]))
    parents = {n: set() for n in nodes}
    for a, vs in adj.items():
        for b in vs:
            parents.setdefault(b, set()).add(a)

    desc_cache: dict = {}

    def descendants(n, seen=frozenset()):
        if n in desc_cache:
            return desc_cache[n]
        out = set()
        for m in adj.get(n, []):
            if m not in seen:
                out.add(m)
                out |= descendants(m, seen | {n})
        desc_cache[n] = out
        return out

    depth_cache: dict = {}

    def depth(n, seen=frozenset()):
        if n in depth_cache:
            return depth_cache[n]
        ps = [p for p in parents.get(n, ()) if p not in seen]
        d = 0 if not ps else 1 + max(depth(p, seen | {n}) for p in ps)
        depth_cache[n] = d
        return d

    return {n: {"weight": len(descendants(n)), "dag_depth": depth(n)} for n in nodes}


def sequence_outcomes(state, config) -> dict:
    """Order final_los into a basic->advanced deep dive. LLM-primary; on failure falls back
    to a deterministic graph+weight+topic sort. Domain-general (no verb/Bloom hardcoding)."""
    prog = _prog(config)
    los = state.get("final_los") or []
    if len(los) <= 1:
        return {}
    prog.start("sequence_outcomes")
    graph = state.get("concept_graph") or {}
    metrics = _concept_metrics(graph)
    topic_order = {s["topic_id"]: s.get("order", 99) for s in state.get("sections", [])}
    depth_by_c = {c["concept_id"]: c.get("depth_category", "moderate")
                  for c in (state.get("concept_inventory") or [])}

    def row(lo):
        cm = metrics.get(lo.get("concept_id"), {"weight": 0, "dag_depth": 0})
        return {"id": lo.get("outcome"), "concept": lo.get("concept"),
                "level": lo.get("bloom_category"),
                "depth": depth_by_c.get(lo.get("concept_id"), "moderate"),
                "topic_order": topic_order.get(lo.get("source_section"), 99),
                "weight": cm["weight"], "dag_depth": cm["dag_depth"]}

    rows = [row(lo) for lo in los]
    by_id = {lo.get("outcome"): lo for lo in los}
    edges = [{"from": a, "to": b} for a, bs in (graph.get("_adj") or {}).items() for b in bs]

    order, source = None, "fallback"
    try:
        data = parse_json(chat(
            [{"role": "system", "content": get_prompt("lo.sequence_sys", _SEQUENCE_SYS)},
             {"role": "user", "content": f"OUTCOMES:\n{json.dumps(rows, ensure_ascii=False)}\n\n"
                                         f"PREREQUISITE EDGES (concept A must come before B):\n{json.dumps(edges)}"}],
            temperature=0)) or {}
        cand = [i for i in (data.get("order") or []) if i in by_id]
        if set(cand) == set(by_id):          # a valid permutation of EXACTLY the given ids
            order, source = cand, "LLM"
    except Exception:  # noqa: BLE001 — fall back to the deterministic graph+weight order
        order = None
    if order is None:
        order = [r["id"] for r in sorted(
            rows, key=lambda r: (r["topic_order"], r["dag_depth"], -r["weight"], r["id"]))]
    seq = [by_id[i] for i in order]
    prog.done("sequence_outcomes", detail=f"{source}: {len(seq)} outcomes, basic→advanced")
    return {"final_los": seq}
