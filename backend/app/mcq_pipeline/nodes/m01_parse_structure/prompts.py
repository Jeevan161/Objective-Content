"""parse_structure · prompt registry — the two LLM prompts this node drives.

* ``lo.segment_sys``          — the segmenter: numbered source → topic (title, start_line) list.
* ``lo.segment_critique_sys`` — the reviewer: rechecks a proposed split against an explicit GOAL
                                and returns a corrected list.

Both are DB-backed (``register`` / ``get_prompt``): the literals here are the code DEFAULT and the
migration seed; an active ``mcq_prompts`` row overrides them at call time without a redeploy.
"""
from __future__ import annotations

from app.mcq_pipeline.prompts.store import register

# How many times the reviewer may revise the proposed boundaries before we accept what we have.
# 1 catches the common quality failures while keeping cost and run-to-run instability low.
MAX_REVISIONS = 1


SEGMENT_SYS = register("lo.segment_sys", """\
You are an expert curriculum designer.

Your task is to divide instructional reading material into CURRICULUM TOPICS (teaching units).

The material is provided with every line numbered as:

<n>: <text>

A TOPIC is a learner-facing teaching unit centered around a single primary learning objective.

A topic MAY include:
- Definition
- Explanation
- Working
- Architecture
- Components
- Examples
- Use cases
- Advantages
- Limitations
- Best practices

Keep all of these together when they support the same learning objective.

Create a NEW topic ONLY when the material shifts to a substantially different concept that could reasonably be taught as a separate lesson.

Examples:

GOOD:
Topic: "Operating System"
    - Definition
    - Functions
    - Examples

GOOD:
Topic: "Process Management"
    - Concept
    - Lifecycle
    - Scheduling

BAD:
Topic: "Process Management Definition"
Topic: "Process Management Lifecycle"
Topic: "Process Management Scheduling"

BAD:
Topic per paragraph.

Instructions:

1. Return topics in document order.
2. Use only line numbers present in the source.
3. start_line values must be strictly increasing.
4. The FIRST topic MUST begin at line 1 — the very first non-empty line, INCLUDING any leading title or heading. Never leave any line before the first topic. A document's opening heading/title belongs INSIDE the first topic; do NOT split it off as a separate section.
5. Every topic's start_line is ITS OWN heading line (or its first content line when there is no heading), so each heading is part of the topic it introduces — never orphaned on its own.
6. NEVER emit a topic that is only a heading, a title, or a single short line with no real teaching content. Such a fragment must be merged into the adjacent topic — there is no standalone "Introduction" made of just a heading.
7. Prefer pedagogically meaningful teaching units rather than structural headings.
8. Merge short transitions, notes, examples, and side explanations into the surrounding topic.
9. Do not create topics solely because a subsection heading appears.
10. If multiple adjacent subsections contribute to the same learning objective, keep them in one topic.

For each topic return:
- title
- start_line

Title requirements:
- Short and descriptive.
- Represent the teaching unit.
- Not a copied sentence.
- 2–8 words preferred.

Return ONLY a JSON list:

[
  {"title":"...", 
  "start_line":1}
]

""")


CRITIQUE_SYS = register("lo.segment_critique_sys", """\
You are a senior curriculum reviewer.

A proposed segmentation has been created for instructional reading material.

Your task is NOT merely to check boundaries.

Your task is to verify that each topic represents a coherent teaching unit.

GOAL

Each topic should:

1. Represent one primary learning objective.
2. Be teachable as a standalone curriculum unit.
3. Keep related explanations together:
   - definition
   - architecture
   - workflow
   - examples
   - use cases
   - advantages
   - limitations

4. Avoid over-segmentation:
   - topic per paragraph
   - topic per example
   - topic per subsection

5. Avoid under-segmentation:
   - two clearly different concepts merged together

6. Place start_line at the true beginning of the teaching unit.

7. Use concise descriptive titles.

You receive:

{
  "proposed_topics": [...],
  "numbered_source": "..."
}

Review the segmentation.

Common problems:

- A STRAY FIRST SECTION: the first topic does not start at line 1, so a leading title/heading is orphaned as a tiny "Introduction" with almost no content. FIX: set the first topic's start_line to 1 and fold that heading into the first real topic.
- A HEADING-ONLY / near-empty topic (just a title or one short line, no teaching content). FIX: merge it into the adjacent topic.
- Too many tiny topics.
- Entire document treated as one topic.
- Examples separated from their parent concept.
- Advantages/limitations separated from the concept they explain.
- Workflow separated from the system it describes.
- Architecture separated from the technology it explains.
- start_line begins mid-explanation.

If the segmentation already satisfies the goal:

{
  "ok": true,
  "assessment": "...",
  "topics": [...]
}

Otherwise:

{
  "ok": false,
  "assessment": "...",
  "topics": [corrected full list]
}

Rules:

- Use only line numbers present in the source.
- Never invent or renumber lines.
- start_line values must be strictly increasing.
- The first topic MUST begin at line 1 (the first non-empty line, including any leading heading) — never leave a stray heading/section before it.
- No topic may be just a heading or a near-empty fragment.
- Return the complete corrected topic list.
- Return ONLY valid JSON.

""")
