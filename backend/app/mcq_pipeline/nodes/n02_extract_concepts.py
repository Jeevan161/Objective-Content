"""LO pipeline · Node 2 — extract_concepts (K-sample self-consistency)."""
from __future__ import annotations

from collections import Counter

from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.config import K_SAMPLES, MAJORITY, TEMP_EXTRACT
from app.mcq_pipeline.utils.concept_graph import description_grounded
from app.mcq_pipeline.utils.llm import chat, parse_json
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.nodes._common import _bind_rag, _prog


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


