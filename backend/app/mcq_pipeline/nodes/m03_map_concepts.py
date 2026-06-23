"""LO pipeline (LO-first) · Node 3 — map_concepts (consistent topic↔concept mapping).

Step 2 of the LO-first flow: "Ensure the topic↔concept mapping is done CONSISTENTLY across
outcomes." The candidate outcomes from Node 2 each name a raw concept independently, so the same
idea can surface under slightly different surface names ("global environment" vs "global
environments"). This node is the variance sink:

  * Canonicalize each outcome's concept name to a stable `concept_id` (`canonical_name` + slug).
  * Build the `concept_inventory` from the UNION of concepts the outcomes reference — one entry per
    canonical concept, carrying its topic, a grounded description, and verbatim evidence.
  * Merge near-duplicate concepts (token-set Jaccard ≥ 0.7, subset relation) via union-find, and
    REMAP every outcome's concept_id onto the surviving representative — so two outcomes that meant
    the same concept end up pointing at the SAME concept_id.
  * Re-key each outcome's id off its final concept_id (deduping collisions).

Input:  state["outcomes"]  — candidate proto-outcomes (carry `_concept_name`, topic_id, evidence).
Output: concept_inventory = [{concept_id, canonical_name, topic_id, in_scope, procedural,
        description, evidence_quotes, evidence}]  +  outcomes stamped with concept_id  +  logs.
Downstream: build_outcome_graph (procedural + weights), profile_depth (taught depth + scope).
"""
from __future__ import annotations

from collections import Counter

from app.mcq_pipeline.utils.concept_graph import (
    canonical_name, description_grounded, display_name, ground_quote, slugify)
from app.mcq_pipeline.nodes._common import _prog


def map_concepts(state, config) -> dict:
    prog = _prog(config)
    prog.start("map_concepts")
    sec_text = {s["topic_id"]: s["text"] for s in state["sections"]}
    outcomes = [dict(o) for o in state["outcomes"]]
    inv: dict = {}

    # (1) Canonicalize each outcome's concept name -> concept_id; accrue the inventory.
    for o in outcomes:
        name = (o.get("_concept_name") or o.get("title") or "").strip()
        canon = canonical_name(name)
        cid = "C_" + slugify(canon)
        o["concept_id"] = cid
        topic_id = o.get("topic_id", "")
        section_text = sec_text.get((o.get("source_evidence") or {}).get("section", topic_id), "")
        ev_quote = (o.get("source_evidence") or {}).get("quote", "")
        quote = ev_quote if (ev_quote and ev_quote in section_text) else ground_quote(name, section_text)
        desc = (o.get("description") or "").strip()
        if not description_grounded(desc, [ev_quote, quote], section_text):
            desc = ev_quote or quote
        if cid not in inv:
            inv[cid] = {"concept_id": cid, "canonical_name": display_name(canon),
                        "topic_id": topic_id, "in_scope": True, "procedural": False,
                        "description": desc,
                        "evidence_quotes": [q for q in {ev_quote, quote} if q],
                        "evidence": {"quote": (quote or ev_quote), "section": topic_id}}
        else:
            for q in (ev_quote, quote):
                if q and q not in inv[cid]["evidence_quotes"]:
                    inv[cid]["evidence_quotes"].append(q)
            if len(desc) > len(inv[cid]["description"]):     # keep the richest grounded prose
                inv[cid]["description"] = desc

    # (2) Merge near-duplicate concepts (union-find). Same metric as the old canonicalize node:
    # token-set Jaccard >= 0.7 AND one token-set a subset of the other (plural/singular, etc.).
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
                    keep = ra if len(wa["canonical_name"]) >= len(wb["canonical_name"]) else rb
                    drop = rb if keep == ra else ra
                    parent[drop] = keep

    groups: dict = {}
    for k in ids:
        groups.setdefault(_find(k), []).append(k)
    remap: dict = {}                      # merged concept_id -> surviving representative
    for rep, members in groups.items():
        for m in members:
            remap[m] = rep
            if m != rep:
                for q in inv[m].get("evidence_quotes", []):
                    if q and q not in inv[rep]["evidence_quotes"]:
                        inv[rep]["evidence_quotes"].append(q)
                if len(inv[m].get("description", "")) > len(inv[rep].get("description", "")):
                    inv[rep]["description"] = inv[m]["description"]
                logs.append({"node": "map_concepts", "merged": m, "into": rep})

    # (3) Remap every outcome onto the surviving concept_id and re-key its id.
    seen = Counter()
    for o in outcomes:
        o["concept_id"] = remap.get(o["concept_id"], o["concept_id"])
        o.pop("_concept_name", None)
        base = slugify(f'{o["learner_action"]}_{o["concept_id"][2:]}')
        seen[base] += 1
        o["id"] = base if seen[base] == 1 else f"{base}_{seen[base]}"

    inventory = [inv[k] for k in sorted(groups)]
    snapshot = {"concept_count": len(inventory), "outcome_count": len(outcomes),
                "merges": len(logs),
                "concepts": [{"concept_id": c["concept_id"], "name": c["canonical_name"],
                              "topic_id": c["topic_id"]} for c in inventory]}
    prog.done("map_concepts",
              detail=f"{len(inventory)} concepts across {len(outcomes)} candidate outcomes",
              snapshot=snapshot)
    return {"concept_inventory": inventory, "outcomes": outcomes, "log": logs}
