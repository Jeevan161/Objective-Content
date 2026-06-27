"""
app/mcq_pipeline/nodes/__init__.py
----------------------------------
The LO pipeline's nodes, one numbered file per node (m01_… in LO-first pipeline order). Re-exports
every node entry point so callers keep importing `from .nodes import parse_structure, …`. Importing
the package triggers each node module, firing its register(...) prompt defaults.

Flow (LO-first):
  parse_structure → [plan_los sub-graph: author_outcomes → consolidate_concepts → graph_outcomes
  → select_outcomes] → resolve_prerequisites → review_and_validate
  → repair (loop) → finalize → sequence_outcomes → [Gate 2] → (question stage: m07–m09)
"""
from app.mcq_pipeline.nodes.m01_parse_structure import parse_structure
from app.mcq_pipeline.nodes.m02_plan_los import (author_outcomes, consolidate_concepts,
                                                 graph_outcomes, select_outcomes)
from app.mcq_pipeline.nodes.m03_resolve_prerequisites import resolve_prerequisites
from app.mcq_pipeline.nodes.m04_review_and_validate import review_and_validate
from app.mcq_pipeline.nodes.m05_repair import repair
from app.mcq_pipeline.nodes.m06_sequence_outcomes import sequence_outcomes

__all__ = [
    "parse_structure", "author_outcomes", "consolidate_concepts", "graph_outcomes",
    "select_outcomes", "resolve_prerequisites", "review_and_validate",
    "repair", "sequence_outcomes",
]
