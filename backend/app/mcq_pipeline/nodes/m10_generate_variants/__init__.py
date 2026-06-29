"""Classroom Quiz · Node 10 — generate_variants (base question -> objective-bound variants)."""
from app.mcq_pipeline.nodes.m10_generate_variants.node import (
    CQ_VARIANT_MAX,
    CQ_VARIANT_MIN,
    generate_variants_for_questions,
)

__all__ = ["generate_variants_for_questions", "CQ_VARIANT_MIN", "CQ_VARIANT_MAX"]
