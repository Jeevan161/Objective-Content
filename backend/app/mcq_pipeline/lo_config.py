"""
app/mcq_pipeline/lo_config.py
-----------------------------
Configuration for the deterministic 10-node Learning-Outcome pipeline (PRD v1.0),
ported from the POC (`_build_lo.py`). Model ids / keys still come from `config.py`
(the single OpenRouter source of truth); these are the LO-specific knobs.
"""

from __future__ import annotations

QUESTION_BUDGET = 20
# Apply is a CEILING (BR2): the split may be lowered on theory-heavy sessions.
DEFAULT_SPLIT = {"remember_understand": 12, "apply": 8}

# Adaptive budget: a small/thin session must NOT be padded to 20 questions (that
# forces invention of ungrounded outcomes). Cap the budget at the number of grounded
# in-scope concepts × MAX_LOS_PER_CONCEPT, with a small floor so tiny sessions still
# yield a usable set.
MAX_LOS_PER_CONCEPT = 2
MIN_BUDGET = 4

# Quality (LO redesign P1): a concept only NAMED in passing (taught_depth "mention")
# is not substantively explained, so it is NOT assessable — drop it from scope rather
# than mint recall outcomes on a bare mention.
DROP_NAMED_ONLY = True

K_SAMPLES = 3                       # self-consistency samples (NFR2)
MAJORITY = (K_SAMPLES // 2) + 1     # = 2 — a concept/edge is "stable" at this vote count
MAX_RETRIES = 2                     # repair-loop cap (§16)

TEMP_EXTRACT = 0.3
TEMP_GRAPH = 0.2
TEMP_AUTHOR = 0.2

# Policy: allow lowering Apply on theory-heavy sessions; else validate() escalates.
ALLOW_SPLIT_OVERRIDE = True

# Controlled action-verb vocabulary, per Bloom level (BR11, Q4).
RU_VERBS = {"define", "identify", "describe", "explain", "summarize", "classify",
            "distinguish", "compare", "illustrate", "recognize", "label", "list",
            "match", "interpret", "outline", "differentiate"}
APPLY_VERBS = {"execute", "implement", "apply", "write", "compute", "solve", "construct",
               "use", "modify", "demonstrate", "calculate", "debug", "trace", "develop",
               "build", "perform", "produce"}
VERBS = {"remember_understand": RU_VERBS, "apply": APPLY_VERBS}
SKILL_TYPES = {"conceptual", "practical_application", "diagnostic"}

# Verb tiers for the V12 grounding-DEPTH gate. A thinly-covered concept (a mention /
# one-line overview) supports ONLY low-demand recall; elaboration verbs need the concept
# taught in depth, and COMPARISON verbs need the material to explicitly contrast the items.
LOW_DEMAND_VERBS = {"identify", "list", "label", "recognize", "match", "name", "define", "state"}
COMPARISON_VERBS = {"compare", "distinguish", "differentiate", "contrast"}

# Pre-authoring DEPTH categories (set per concept by the profile_coverage node) and the
# verb CEILING each permits. mention = named/stated once, no real explanation; moderate =
# explained with reasoning across a few sentences; deep = thoroughly developed (explanation
# PLUS examples/steps/contrast). The ceiling BOUNDS what an LO on that concept may ask, so
# authoring respects the material's actual depth BEFORE it writes outcomes.
DEPTH_CATEGORIES = ("mention", "moderate", "deep")
_UNDERSTAND_VERBS = {"describe", "explain", "summarize", "interpret", "classify", "outline"}


def allowed_verbs_for(depth: str, apply_suitable: bool) -> set[str]:
    """The learner-action verbs an outcome on a concept of this taught DEPTH may use.
    mention -> recall only; moderate -> + understand; deep -> all RU incl. comparison.
    Apply verbs are permitted only when the concept is apply_suitable AND taught beyond a
    bare mention."""
    base = set(LOW_DEMAND_VERBS)
    if depth in ("moderate", "deep"):
        base |= _UNDERSTAND_VERBS
    if depth == "deep":
        base |= COMPARISON_VERBS
    allowed = base & RU_VERBS
    if apply_suitable and depth in ("moderate", "deep"):
        allowed |= APPLY_VERBS
    return allowed

# Alias map: surface name -> canonical (the variance sink, §9.3). Domain-agnostic
# (empty) by default; supply synonym->canonical pairs per subject if needed.
ALIAS_MAP: dict[str, str] = {}

SPEC_VERSION = "1.0.0"
