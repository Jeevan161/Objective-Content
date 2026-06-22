"""
lo_schemas.py (vendored verbatim)
---------------------------------
Pydantic models for the Learning Outcomes the workflow produces.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class LearningOutcome(BaseModel):
    outcome: str = Field(description="snake_case slug, e.g. apply_migrate_command_to_execute_migrations")
    bloom_category: str = Field(description="remember | understand | apply | implement")
    bloom_level: str = Field(description="same Bloom level as bloom_category")
    skill_type: str = Field(description="e.g. conceptual_knowledge | practical_application")
    concept: str = Field(description="the broad concept, e.g. 'Applying Migrations'")
    sub_concept: str = Field(description="the specific sub-concept, e.g. 'migrate_command'")
    description: str = Field(description="one-line outcome statement starting with the learner action")
    learner_action: str = Field(description="the verb the learner performs, e.g. apply / define / explain")
    syntax: str = Field(default="", description="exact command/syntax if applicable, else empty string")
    justification: str = Field(description="why this is a worthwhile outcome (for apply: the prerequisites)")
    source_evidence: str = Field(description="the exact line/quote from the material that supports this")


class LOBatch(BaseModel):
    """What an agent returns: a list of outcomes."""
    learning_outcomes: List[LearningOutcome]


class SessionFacts(BaseModel):
    """Structured parse of the session reading material (shared by both agents)."""
    concepts: List[str] = Field(default_factory=list, description="concepts taught in the session")
    commands: List[str] = Field(default_factory=list, description="CLI/commands shown, e.g. 'python3 manage.py migrate'")
    syntax: List[str] = Field(default_factory=list, description="code/syntax patterns shown")
    key_points: List[str] = Field(default_factory=list, description="other teachable points")
