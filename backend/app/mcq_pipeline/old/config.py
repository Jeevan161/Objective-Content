"""
app/mcq_pipeline/lo_config.py
-----------------------------
Configuration for the LO pipeline. Model ids / keys still come from `config.py`
(the single OpenRouter source of truth); these are the LO-specific knobs.

Bloom is a 4-TIER model — remember / understand / apply / scenario — and the tier
DIVISION is derived from per-concept FEASIBILITY (taught depth + procedurality), not a
fixed split. See `feasible_tiers` / `allowed_verbs_for`.
"""

from __future__ import annotations

# --- question budget (user-supplied; QUESTION_BUDGET is only the default ceiling) --- #
QUESTION_BUDGET = 20                # default ceiling when the caller supplies no budget
MAX_LOS_PER_CONCEPT = 2             # capacity = (# in-scope concepts) x this
MIN_BUDGET = 5                      # floor; budget is quantized to multiples of BUDGET_STEP
BUDGET_STEP = 5                     # step the budget down to the nearest multiple of 5 when thin
SCENARIO_TARGET = 2                 # aim for ~this many scenario LOs when feasible (0 is allowed)
# When True, an LLM ranks concepts by pedagogical importance and the allocator hands the scarce
# scenario / extra-deepening slots to the most central concepts first (LLM proposes, deterministic
# allocator disposes within all ceilings). Falls back to inventory order if disabled or on failure.
USE_LLM_IMPORTANCE_RANKING = True
# When True, prerequisite coverage is checked by an LLM-driven, RAG-GROUNDED probe: the LLM writes
# search queries (per depth aspect), the RAG tool answers, and the LLM judges covered + depth from
# the retrieved evidence ONLY. Falls back to a single grounded check_concept if disabled/unavailable.
USE_LLM_COVERAGE_PROBE = True

# Quality: a concept only NAMED in passing (taught_depth "mention") is not assessable beyond
# bare recall, so it is dropped from scope rather than seeding a recall LO on a bare mention.
DROP_NAMED_ONLY = True

K_SAMPLES = 3                       # self-consistency samples (NFR2)
MAJORITY = (K_SAMPLES // 2) + 1     # = 2 — a concept/edge is "stable" at this vote count
MAX_RETRIES = 3                     # regenerate-repair loop cap

TEMP_EXTRACT = 0.3
TEMP_GRAPH = 0.2
TEMP_AUTHOR = 0.2

# --- controlled action-verb vocabulary, per Bloom TIER (BR11) ---------------------- #
REMEMBER_VERBS = {"identify", "list", "label", "recognize", "match", "name", "define", "state"}
UNDERSTAND_VERBS = {"explain", "describe", "summarize", "interpret", "classify", "outline",
                    "compare", "distinguish", "differentiate", "illustrate"}
APPLY_VERBS = {"execute", "implement", "apply", "write", "compute", "solve", "construct",
               "use", "modify", "calculate", "debug", "trace", "develop", "build",
               "perform", "produce"}
# Scenario = apply in a NOVEL situation (transfer). Verbs overlap apply but read as judgement.
SCENARIO_VERBS = {"apply", "solve", "determine", "diagnose", "predict", "recommend",
                  "choose", "decide", "evaluate"}

TIER_ORDER = ("remember", "understand", "apply", "scenario")
VERBS = {"remember": REMEMBER_VERBS, "understand": UNDERSTAND_VERBS,
         "apply": APPLY_VERBS, "scenario": SCENARIO_VERBS}

SKILL_TYPES = {"conceptual", "practical_application", "diagnostic"}

# Pre-authoring DEPTH categories (set per concept by the profile_coverage node). mention =
# named/stated once, no real explanation; moderate = explained with reasoning across a few
# sentences; deep = thoroughly developed (explanation PLUS examples/steps/contrast).
DEPTH_CATEGORIES = ("mention", "moderate", "deep")


def feasible_tiers(depth: str, procedural: bool) -> tuple[str, ...]:
    """The Bloom tiers a concept taught at this DEPTH (and procedurality) can support.
    Domain-agnostic feasibility ceiling:
      mention            -> remember
      moderate           -> remember, understand            (+ apply if procedural)
      deep               -> remember, understand            (+ apply, scenario if procedural)
    """
    tiers = ["remember"]
    if depth in ("moderate", "deep"):
        tiers.append("understand")
        if procedural:
            tiers.append("apply")
            if depth == "deep":
                tiers.append("scenario")
    return tuple(tiers)


def allowed_verbs_for(depth: str, procedural: bool) -> set[str]:
    """The learner-action verbs an outcome on a concept of this taught DEPTH may use —
    the union of the verb sets of every tier the feasibility ceiling permits."""
    allowed: set[str] = set()
    for tier in feasible_tiers(depth, procedural):
        allowed |= VERBS[tier]
    return allowed


# Alias map: surface name -> canonical (the variance sink). Domain-agnostic (empty) by
# default; supply synonym->canonical pairs per subject if needed.
ALIAS_MAP: dict[str, str] = {}

SPEC_VERSION = "2.0.0"
