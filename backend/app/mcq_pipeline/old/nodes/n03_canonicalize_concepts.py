"""LO pipeline · Node 3 — canonicalize_concepts (variance sink + evidence binding)."""
from __future__ import annotations

from app.mcq_pipeline.utils.concept_graph import (
    canonical_name, description_grounded, display_name, ground_quote, slugify)
from app.mcq_pipeline.nodes._common import _prog


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
        # procedurality is set later by build_dependency_graph (LLM applied_skill vote).
        # Evidence-bind the description: keep the LLM prose only if it traces to the
        # material; otherwise fall back to the verbatim evidence quote so the concept's
        # description is grounded BY CONSTRUCTION (no hallucinated descriptions survive).
        ev_quote = rc["evidence"]["quote"]
        desc = (rc.get("description") or "").strip()
        if not description_grounded(desc, [ev_quote, quote], section_text):
            desc = ev_quote or quote
        if cid not in inv:
            inv[cid] = {"concept_id": cid, "canonical_name": display_name(canon),
                        "topic_id": rc["evidence"]["section"], "in_scope": True,
                        "procedural": False, "description": desc,
                        "evidence_quotes": [q for q in {ev_quote, quote} if q],
                        "evidence": rc["evidence"]}
        else:
            for q in (ev_quote, quote):
                if q and q not in inv[cid]["evidence_quotes"]:
                    inv[cid]["evidence_quotes"].append(q)
            if len(desc) > len(inv[cid]["description"]):   # keep the richest grounded prose
                inv[cid]["description"] = desc

    # Merge near-duplicate concepts (not just log them): token-set Jaccard >= 0.7 AND
    # one token-set a subset of the other (e.g. plural/singular, "global environment"
    # vs "global environments"). Union-find so merges are order-independent. The
    # surviving concept_id is the more specific (longer name) one — NOTE procedural is
    # uniformly False at this node (set later in build_dependency_graph), so it does
    # not yet affect the choice.
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


