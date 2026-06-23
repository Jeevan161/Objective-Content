"""LO pipeline · Node 2 — extract_concepts.

Finds the distinct teachable CONCEPTS in each topic section produced by parse_structure.

What it does:
  * For every section, calls the extractor LLM (`lo.extract_sys`) K times and keeps only the
    concepts that reach a MAJORITY across those K runs — "K-sample self-consistency", which
    suppresses one-off LLM drift/hallucination. For each surviving concept it keeps the richest
    sample (the longest grounded description) plus a VERBATIM evidence quote from the section.
  * In parallel, derives a short grounded DESCRIPTION for each topic (`lo.topic_desc_sys`), kept
    only if it traces back to the section text — otherwise it falls back to the topic title.
  * Sections run concurrently via `pmap`; a section that yields no stable concept is flagged.

A concept is a TRANSFERABLE idea / skill / rule — never an example-local label ("Project A", a
sample variable). The illustrative example is evidence FOR a concept, not a concept itself.

Input:  state["sections"]  — topic sections (see parse_structure).
Output: raw_concepts = [{name, description, evidence: {quote, section}}]  +  the sections enriched
        with a grounded `description`  +  per-section extraction logs.
Downstream: canonicalize_concepts dedups/merges these into the canonical concept inventory.
"""
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
You extract the DISTINCT, TEACHABLE CONCEPTS from one section of instructional reading material (any subject).

A concept is a TRANSFERABLE idea, rule, skill, mechanism, or principle that a learner could be assessed on and could apply BEYOND this specific section.

---

### CORE DISTINCTION RULE
Instructional text often teaches concepts through examples (scenarios, sample programs, stories, placeholder names like "Project A", "User1", sample files, or values).
- Examples are ONLY evidence.
- NEVER treat example labels or concrete instances as concepts.
- Extract the GENERALIZED idea demonstrated by the example.

✔ Correct: "dependency version conflict"
✘ Incorrect: "Project A", "Django Project A"

---

### CROSS-DOMAIN ABSTRACTION RULE (NEW IMPORTANT RULE)

Examples and scenarios may appear in domain-specific forms (coding, networking, biology, finance, etc.).

You MUST:
- Recognize the SAME underlying concept even if expressed in different domains.
- Normalize it into a domain-independent conceptual form.

✔ Examples of correct abstraction:
- "Docker container isolation" → "environment isolation"
- "Bank account overdraft example" → "resource limit violation"
- "TCP packet retransmission" → "reliable data transfer mechanism"
- "Student A / Student B comparison" → "entity comparison logic"

✘ Do NOT:
- Keep domain-specific framing as the concept itself
- Create separate concepts for the same underlying idea across domains
- Anchor concept names to the story/context unless it is essential to the idea

However:
- If a concept is inherently domain-specific (e.g. "venv", "HTTP GET", "SQL JOIN"), KEEP the domain term as the concept name.

---

### VALID CONCEPT INCLUSION RULES

Include a concept ONLY if:

1. It is EXPLICITLY taught, explained, defined, or demonstrated in the text.
2. It is SUBSTANTIVE (not just mentioned in passing).
3. It is TRANSFERABLE beyond the given example or context.

---

### TOOL / TERMINOLOGY RULE

Technologies, tools, commands, or domain terms explicitly taught by name ARE valid concepts:
Examples: "pip", "venv", "Django", "HTTP GET"

Do NOT infer concepts not present in the text.

---

### GRANULARITY RULE

Prefer SPECIFIC, single-idea concepts over broad umbrellas.

✔ Good:
- "creating a virtual environment"
- "installing dependencies"

✘ Bad:
- "project setup process"

---

### GROUPING RULE FOR LIST-STYLE CONTENT

If the section only lists items or gives a high-level overview:

- Extract ONE concept representing the OVERVIEW.
- Do NOT break into multiple concepts unless each item is independently explained.

---

### OUTPUT REQUIREMENTS

For each concept return:

- "name": short, canonical, transferable concept name (no example-specific labels)
- "description": 1–2 sentences strictly grounded in the text only. No external knowledge.
- "quote": verbatim supporting evidence from the section

---

### STRICT LIMITS

- Return ONLY valid JSON
- Output must be a JSON list
- 4–8 concepts max
- Every concept MUST have a supporting quote
- No markdown, no commentary, no extra text

---

### FINAL OUTPUT FORMAT

[{"name": "...", "description": "...", "quote": "..."}]
""")


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


