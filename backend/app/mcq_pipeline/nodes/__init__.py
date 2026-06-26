"""
app/mcq_pipeline/nodes/__init__.py
----------------------------------
The LO pipeline's nodes, one numbered file per node (m01_… in LO-first pipeline order). Re-exports
every node entry point so callers keep importing `from .nodes import parse_structure, …`. Importing
the package triggers each node module, firing its register(...) prompt defaults.

Flow (LO-first):
  parse_structure → generate_outcomes → map_concepts → build_outcome_graph → profile_depth
  → plan_outcomes → resolve_prerequisites → review_outcomes_quality → validate
  → repair (loop) → finalize → sequence_outcomes → [Gate 2] → (question stage: n13–n15)
"""
from app.mcq_pipeline.nodes.m01_parse_structure import parse_structure
from app.mcq_pipeline.nodes.m02_generate_outcomes import generate_outcomes
from app.mcq_pipeline.nodes.m03_map_concepts import map_concepts
from app.mcq_pipeline.nodes.m04_build_outcome_graph import build_outcome_graph
from app.mcq_pipeline.nodes.m05_profile_depth import profile_depth
from app.mcq_pipeline.nodes.m06_plan_outcomes import plan_outcomes
from app.mcq_pipeline.nodes.m07_resolve_prerequisites import resolve_prerequisites
from app.mcq_pipeline.nodes.m08_review_outcomes_quality import review_outcomes_quality
from app.mcq_pipeline.nodes.m09_validate import validate
from app.mcq_pipeline.nodes.m10_repair import repair
from app.mcq_pipeline.nodes.m11_sequence_outcomes import sequence_outcomes

__all__ = [
    "parse_structure", "generate_outcomes", "map_concepts", "build_outcome_graph",
    "profile_depth", "plan_outcomes", "resolve_prerequisites", "review_outcomes_quality",
    "validate", "repair", "sequence_outcomes",
]
