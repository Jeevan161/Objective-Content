"""
app/mcq_pipeline/tools.py
-------------------------
Builds the apply-agent's RAG tools as closures over the run's bound `RagAdapter`,
so the ReAct agent's tool calls work regardless of which thread LangGraph runs
the apply node on (no reliance on the ContextVar surviving framework threading).
"""

from __future__ import annotations

import json

from langchain_core.tools import tool


def make_apply_tools(adapter):
    @tool
    def check_concept(topic: str, syntax: str = "") -> str:
        """Check whether a concept (and optional exact syntax) is explained anywhere in
        the course reading materials. Returns a verdict (EXPLAINED / PARTIALLY EXPLAINED
        / NOT EXPLAINED) plus citations. Use this to confirm an apply-skill is genuinely
        taught before writing an outcome for it."""
        return json.dumps(adapter.check_concept(topic, syntax or None), ensure_ascii=False)

    @tool
    def find_prerequisites(topic: str) -> str:
        """Find the earlier concepts a learner must understand BEFORE this topic. Use
        this to write the justification for an apply-level outcome — it tells you what
        prior knowledge the skill depends on."""
        return json.dumps(adapter.find_prerequisites(topic), ensure_ascii=False)

    @tool
    def search_reading_material(query: str) -> str:
        """Semantic search over the course reading materials. Returns the best-matching
        sections with snippets. Use it to pull supporting evidence or related context."""
        return json.dumps(adapter.search(query), ensure_ascii=False)

    return [check_concept, find_prerequisites, search_reading_material]
