"""
qgen_agents.py (vendored, adapted)
----------------------------------
One agent per question type; each generates LEAN, question-specific content
grounded in LO intent + RAG truth. Adapted: relative imports + app config shim;
all guideline blocks/prompts registered with `prompt_store` (DB-overridable) and
fetched at call time; optional `on_progress` callback for live per-LO progress.
"""

from __future__ import annotations

import difflib
import re

from app.mcq_pipeline.utils import llm as config, rag_api, scope
from app.mcq_pipeline.utils.concurrency import pmap
from app.mcq_pipeline.prompts.store import get_prompt, register
from app.mcq_pipeline.schemas.questions import CodeMCQLean, CodeMoreThanOneLean, CodeTextualLean, FibCodingLean, MCQLean, RearrangeLean, TextualLean, TrueFalseLean

CODE_PATH_TYPES = {
    "FIB_CODING", "CODE_ANALYSIS_MULTIPLE_CHOICE",
    "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE", "CODE_ANALYSIS_TEXTUAL",
}

# Types whose options/distractors must be RAG-grounded: a learner can only evaluate
# a wrong option if every term/keyword it uses was actually taught.
OPTION_TYPES = {
    "MULTIPLE_CHOICE", "MORE_THAN_ONE_MULTIPLE_CHOICE",
    "CODE_ANALYSIS_MULTIPLE_CHOICE", "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE",
}

_DIFFICULTY = {"remember": "EASY", "understand": "MEDIUM", "apply": "MEDIUM",
               "scenario": "HARD", "implement": "HARD"}


def difficulty_of(lo: dict) -> str:
    # Prefer the 4-tier bloom_level_raw (carries 'scenario'); fall back to legacy bloom_category.
    tier = (lo.get("bloom_level_raw") or lo.get("bloom_category") or "").lower()
    return _DIFFICULTY.get(tier, "MEDIUM")


def _model(temp: float = 0.3):
    # Question GENERATION agent. Built on the active connector (OpenRouter) but with the
    # generation model id (settings.mcq_generation_model — Sonnet 4.6); empty -> the
    # connector's own model. Review uses its own model (see n15._review_model).
    from app.core.config import settings
    from app.mcq_pipeline.utils.llm import make_chat_model
    return make_chat_model(temperature=temp, model=settings.mcq_generation_model or None)


# --- reusable guideline blocks (registered as DB-overridable prompts) -------- #
_PERSONA = register("gen.persona", """\
You are a Senior Technical Instructional Designer and Assessment Specialist with 10+ years of experience designing programming assessments.

Your primary objective is ASSESSMENT VALIDITY. Always prioritize, in order:
1. Learning Outcome alignment
2. Grounding in the course material
3. Fairness
4. Clarity
5. Reliable grading

Do NOT optimize for difficulty, trickiness, creativity, or novelty when doing so weakens assessment validity.

The goal is not to create the hardest question — it is to create the most accurate measurement of the Learning Outcome.""")

_HEADER = register("gen.header", """\
Write ONE assessment item of type {qtype} that most directly measures the Learning Outcome. Generate a completely new question, not a variation of an existing one.

The question must:
- Assess only the intended skill
- Match the Bloom level of the Learning Outcome
- Be answerable using ONLY the COURSE MATERIAL
- Avoid testing unrelated knowledge
- Minimize ambiguity

Always include:
- a clear question stem
- the type-specific fields
- a concise explanation""")

_GROUNDING_RULES = register("gen.grounding_rules", """\
GROUNDING RULES

The Learning Outcome defines WHAT to assess. The COURSE MATERIAL is the ONLY source of truth.

When the COURSE MATERIAL marks a PRIMARY GROUNDING span (the evidence/section this outcome was drawn from), the question MUST be answerable from THAT span. Use the BACKGROUND material only for supporting context and consistent terminology — never to introduce facts, comparisons, or depth beyond what the primary span actually teaches. If the primary span is thin, ask a simpler question that the span fully supports.

Every fact, term, code snippet, keyword, operator, answer choice, distractor, and explanation must come from the COURSE MATERIAL.

Never use:
- outside knowledge
- assumed knowledge
- common industry knowledge
- undocumented language features

Do NOT introduce:
- technologies
- frameworks
- libraries
- commands
- APIs
- tools
- products
- version numbers
- proper nouns
unless they explicitly appear in the COURSE MATERIAL.

Distractors must be built by misapplying taught concepts, not by introducing untaught concepts.

Grounding is about CONCEPTS, not exact wording. You MAY paraphrase, restate, or use natural-language synonyms for a concept the material teaches (e.g. describing the taught scenario as a "version conflict", or "global environment" as a "shared environment"). What you may NOT do is introduce a genuinely NEW technology, tool, library, command, or proper noun that is absent from the material. Do not copy the material's exact phrasing just to be safe.

If the material lacks enough detail for a sophisticated question, generate a simpler question that remains fully grounded.

GROUNDING VALIDATION — before finalizing, check every:
- noun
- keyword
- operator
- function
- command
- code construct
If it does not appear in the COURSE MATERIAL, remove it.""")

_QUESTION_TEXT_RULES = register("gen.question_text_rules", """\
QUESTION-TEXT RULES

The stem must ask exactly ONE problem.

The learner should immediately understand:
- what is being asked
- what information is relevant
- how to answer

Use:
- clear language
- direct wording
- positive phrasing

Avoid:
- double negatives
- hidden assumptions
- unnecessary context
- story-based wrappers
- trick questions
- puzzle-style questions
- ambiguous wording

SELF-CONTAINED — the question is an INDEPENDENT resource. The learner sees ONLY the stem and options, never the reading material. Therefore:
- Embed EVERY detail needed to answer directly in the stem. Never defer to the source: no 'according to the material', 'based on the lesson', 'in the reading/passage', 'as discussed', 'from this session'.
- Never reference a source-local example entity the learner cannot see — a scenario label ('Project A'/'Project B'), a sample file/variable/function name, or a one-off value from the reading. If a scenario is needed, define it fully and generically IN THE STEM so it stands alone.
- Test transferable understanding of the concept, not recall of an arbitrary detail from one example.

Do not reveal the answer in the stem.

Do not end the stem with a trailing full stop ('.'). A question mark ('?') or a colon introducing options is fine; a bare trailing period is not.""")

_MARKDOWN_RULES = register("gen.markdown_rules", """\
MARKDOWN FORMATTING

The portal renders the question text and the explanation as MARKDOWN. Write them as clean Markdown:
- Use inline `code` for code identifiers, values, commands, and outputs referenced in prose; **bold** sparingly for key terms; '-' bullet lists for enumerations.
- Separate EVERY block-level element (each paragraph, each list, each fenced code block) with a BLANK LINE. The portal's renderer needs a blank line between blocks — without it, adjacent blocks merge or fail to render.
- Keep the stem a short prose block; do not use a Markdown heading (#) in the stem.

OPTIONS:
- Prose/conceptual options MAY use light inline Markdown (`code`, **bold**). When an option actually contains such Markdown, set its `content_type` to "MARKDOWN"; otherwise leave it "TEXT".
- For CODE_ANALYSIS_* question types, options are literal code or program output — keep them PLAIN TEXT (no Markdown, no backticks), `content_type` "TEXT", so the portal shows them verbatim.

NEVER apply Markdown to a graded exact-answer value (a TEXTUAL / FIB answer or expected output) or to the `code` field — those must stay literal.""")

_OPTION_RULES = register("gen.option_rules", """\
OPTION RULES

Create exactly 4 options unless the question type requires otherwise.

All options must be semantically DISTINCT — no two options may express the same idea in different words, and no distractor may be a paraphrase of the correct answer (it must be genuinely incorrect, not a reworded correct answer).

Options must be SELF-CONTAINED: never reference a source-local example entity the learner cannot see (e.g. "Project A"/"Project B", a sample file/variable name from the reading). Any context an option needs must be stated generically IN THE STEM. (Do not introduce untaught named technologies either — see GROUNDING RULES.)

Each option must be:
- concise
- scannable
- grammatically consistent
- similar in length
- NOT terminated by a full stop — an option is a phrase/value, so it must not end with a trailing '.' (other punctuation that is part of the value is fine)

Do NOT use:
- full explanations
- examples
- conditions
- reasoning

DISTRACTOR RULES

Distractors must:
- be plausible
- represent common misunderstandings
- be grounded in taught concepts
- be technically incorrect
- be the SAME KIND of thing as the correct answer (a version question needs version options; a command question needs commands; a value question needs values) — never mix categories, so no option stands out and all stay comparable

Each distractor must encode a DIFFERENT misconception — over-generalize a taught rule, confuse the concept with a sibling taught concept, or invert cause-and-effect. Never reword the correct answer or paraphrase another option.

Distractors must NOT:
- be absurd
- use untaught concepts
- contain syntax unrelated to the LO
- be obviously wrong

A distractor should fail because it misunderstands the target concept.

OPTION BALANCE RULES

The correct answer must not stand out because of:
- length
- terminology
- formatting
- specificity

All options should appear equally credible at first glance.

Vary the position of the correct answer across questions.

Avoid absolute qualifiers in options ('always', 'never', 'all', 'none') — they read as giveaways or are unfalsifiable. Do NOT use 'All of the above' or 'None of the above' as options unless the Learning Outcome specifically requires that judgment; prefer concrete, comparable alternatives.

The correct option must be the most TECHNICALLY ACCURATE answer the material supports — not the 'commonly recommended' or 'best-practice' one — UNLESS the stem explicitly asks for the standard/recommended practice. (A distractor that is also defensible as 'best practice' would create a second valid answer.)""")

_TRUE_FALSE_RULES = register("gen.true_false_rules", """\
TRUE/FALSE RULES

The statement must be:
- complete
- declarative
- concise
- focused on one concept

The statement must be DECLARATIVE — it must not contain 'what', 'which', or 'select', and must not end with '?'. Do not prefix with 'True or False' or 'The following statement'.

Each statement must test exactly one idea. Avoid combining multiple facts into a single statement.

A false statement must be plausible and grounded in taught concepts.

If the LO targets code, its output, or its behavior, put a short snippet in `code` (taught syntax only, properly formatted, no backticks, not revealing the verdict); otherwise leave `code` empty.""")

_MORE_THAN_ONE_RULES = register("gen.more_than_one_rules", """\
MULTIPLE-CORRECT RULES

Provide 4–6 options.

Requirements:
- 2–3 options must be correct
- at least 1 option must be incorrect

Each statement must be independently evaluable.

Every incorrect option must be CONTRADICTED by the material or misapply a taught concept — never a true taught fact relabeled as false. If you cannot construct a genuinely-incorrect grounded option, REDUCE the option count (keep at least 2 correct and at least 1 incorrect) rather than relabeling a true statement.

Avoid:
- overlapping statements
- nested statements
- statements that imply each other

Do not indicate how many answers are correct.

The explanation must justify every correct statement and explain why every incorrect statement is incorrect.""")

_FIB_RULES = register("gen.fib_rules", """\
FILL-IN-THE-BLANK (EXECUTION-GRADED) RULES

The question is a COMPLETE, RUNNABLE program with exactly one blank. It is graded by EXECUTION: the learner fills the blank, the program is run on the given input, and its stdout is compared to the expected output. The blank text itself is NOT matched — ANY completion that makes the program produce the expected output is correct.

Provide all of:
- code_lines: a complete runnable program (taught syntax only) with the sentinel {{BLANK}} on the ONE line the learner completes.
- blank_answer: one correct completion (used to verify the question runs correctly).
- test_input: the stdin for the run (use "" if the program reads no input).
- test_output: the EXACT stdout the program prints when the blank is correctly filled and run on test_input.

The blank must target the concept the Learning Outcome assesses — never boilerplate (imports, scaffolding, print formatting).

The surrounding code must not reveal the answer, and the program MUST actually run and print test_output. Do NOT use non-runnable shell snippets or pseudo-code.

NEVER make the blank (or the program) an installation, shell, or environment-setup command — e.g. pip install, npm install, mkdir, cd, source .../bin/activate, virtualenv, python -m venv, export VAR=. Those are not runnable input→output programs. If the Learning Outcome is about such a command, it is NOT a FIB — it must be a MULTIPLE_CHOICE.""")

_REARRANGE_RULES = register("gen.rearrange_rules", """\
REARRANGE RULES

Use REARRANGE only for a genuine ORDERED PROCEDURE that has ONE canonical sequence grounded in the material. If the material does not define a strict order, REARRANGE is the wrong type.

Every item must be a CONCRETE, executable step or a real line of code — an action the learner performs. NEVER include a goal, a concept, or a mental state as a step (e.g. 'understand the need for isolation', 'know the syntax' are NOT steps).

Provide 3-6 steps. Each step must be distinct, non-overlapping, a single action, and grounded in the material (no invented steps).

The correct order must be unambiguous — no two steps interchangeable. Do not number the steps or otherwise reveal the order in the text.

Present the steps in an order DIFFERENT from the correct sequence — never list them already sorted (that would make the item trivial). The correct sequence must be the one unique right ordering.""")

_EXPLANATION_RULES = register("gen.explanation_rules", (
    "EXPLANATION RULES:\n"
    "- Give strong reasoning for why the correct answer is correct, using only "
    "facts present in the course material.\n"
    "- Briefly indicate why the wrong choices are wrong, highlighting the "
    "distinction from the correct answer.\n"
    "- Do NOT use the words 'option', 'option 2', etc. — refer to the content "
    "itself.\n"
    "- Use only terminology that appears in the course material."
))

_TYPED_ANSWER_TYPES = {"TEXTUAL", "CODE_ANALYSIS_TEXTUAL", "FIB_CODING"}

_EXACT_ANSWER_RULES = register("gen.exact_answer_rules", """\
EXACT-ANSWER RULES (graded by exact string match)

Use exact-answer formats only when exactly one acceptable answer exists.

The expected answer must be:
- a single word
- two words maximum
- a numeric value
- a single unambiguous token

Do NOT use:
- commands
- code statements
- sentences
- explanations
- long phrases

Avoid answers that may vary because of capitalization, spacing, punctuation, or alternate spellings.

SELF-CHECK: ask "Could two knowledgeable learners provide different correct answers?" If YES, do not use an exact-answer format — reframe the stem or choose a different question type.""")

_CODE_RULES = register("gen.code_rules", """\
CODE RULES

The code snippet must be essential to solving the question. Every line must contribute to the concept being assessed.

Avoid:
- unnecessary variables
- dead code
- unrelated logic
- artificial complexity

The code should resemble realistic learner-facing examples. Use ONLY constructs taught in the material. Do NOT wrap the code in backticks. Do not give the answer away in the code.

When the question asks for a program's result, say "prints" or "the output" — never "displays", "shows", "on the screen", or "on the console". The expected output is exactly the program's stdout.

If a snippet is provided in the `code` field, the stem must REFER to it (e.g. "the given code snippet") and must NOT repeat the code inline in the question text.

ERROR/FIX RULES

Any error must arise directly from the Learning Outcome concept. The learner should be able to explain the issue using only the Learning Outcome and the COURSE MATERIAL.

The correct fix must be the minimum change required. Incorrect fixes must be plausible but still incorrect.""")


_SQL_RULES = register("gen.sql_rules", """\
SQL RULES (apply whenever the question is about SQL)

Use standard, dialect-neutral SQL that runs on common engines (SQLite / PostgreSQL). Do NOT use vendor-only syntax (e.g. TOP, ISNULL, NVL, LIMIT-vs-FETCH, vendor date functions) unless the COURSE MATERIAL explicitly teaches that dialect.

SCHEMA CONTEXT — a query question must be answerable from the query PLUS the schema shown. If the stem or `code` references a table, describe its relevant columns directly in the question, and include a few sample rows whenever the answer depends on the data. Never rely on an unshown table or column.

CORRECTNESS:
- SQL keywords (SELECT, FROM, WHERE, GROUP BY, HAVING, ORDER BY, JOIN, ON, DISTINCT, ...) must be spelled correctly and the query must be syntactically valid.
- In a query with aggregation, EVERY non-aggregated column in the SELECT list must also appear in GROUP BY. Filtering on an aggregate uses HAVING, never WHERE.
- NULL comparisons use IS NULL / IS NOT NULL, never `= NULL`. Aggregates skip NULLs except COUNT(*). AND/OR/NOT follow three-valued logic.
- JOIN questions must state the join type and the ON condition, and must distinguish INNER vs LEFT/RIGHT/FULL behaviour for unmatched rows.
- String and date literals use single quotes; identifiers stay unquoted unless they truly require it. Never wrap an entire query in quotes.

RESULT, NOT "OUTPUT" — a SQL query RETURNS a result set (rows / values); it does not "print". For a SQL code question describe what the query "returns" or "the result" — never "prints" / "displays" / "on the console". A result is deterministic only when row order is fixed by ORDER BY; do not ask about the order of rows unless the query has ORDER BY.

NAMES — prefer realistic table / column names (employees, orders, customers, salary, department) over abstract t1 / c1.

DISTRACTORS — build wrong options from COMMON SQL mistakes: WHERE used where HAVING is required, a column missing from GROUP BY, `= NULL`, INNER-vs-LEFT JOIN row counts, DISTINCT misuse, or single/double-quote confusion.

CODE_LANGUAGE — for any SQL code question set code_language to "SQL". Do NOT assess a SQL "write / complete a query" outcome with an execution-graded FIB_CODING (SQL is not run here); use MULTIPLE_CHOICE or a CODE_ANALYSIS_* item instead.""")


def _course_is_sql() -> bool:
    """True when the CURRENT run's course is configured as a SQL course.

    Deterministic and course-level: the value comes from Course.question_domain
    (set per course), carried on the run-scoped RagAdapter as `domain`. There is no
    per-outcome guessing — a course is SQL or it is not. Returns False when no adapter
    is bound (e.g. unit tests) so callers can use it unconditionally."""
    try:
        return (getattr(scope.get_adapter(), "domain", "") or "").upper() == "SQL"
    except Exception:  # noqa: BLE001 — no adapter bound -> not a SQL run
        return False


# Universal self-review checklist, applied last so the model audits the whole item
# against assessment-validity before returning it.
_FINAL_VALIDATION = register("gen.final_validation", """\
FINAL VALIDATION CHECKLIST

Before returning the question, verify ALL of the following:
- Directly measures the Learning Outcome
- Matches the Bloom level
- Fully grounded in the COURSE MATERIAL (no outside concepts)
- No ambiguity and no accidental clues
- The correct answer is uniquely correct
- Distractors are plausible and grounded in taught concepts
- Answerable using taught content alone
- No grading ambiguity

If any check fails, revise the question before returning it.""")


def _sys_for(qtype: str, lo: dict) -> str:
    """Assemble the system prompt for a type, composing the reusable blocks
    (each fetched live so DB edits take effect)."""
    parts = [
        f"{get_prompt('gen.persona', _PERSONA)} "
        f"{get_prompt('gen.header', _HEADER).format(qtype=qtype)}",
        get_prompt("gen.grounding_rules", _GROUNDING_RULES),
        get_prompt("gen.question_text_rules", _QUESTION_TEXT_RULES),
        get_prompt("gen.markdown_rules", _MARKDOWN_RULES),
        get_prompt("gen.option_rules", _OPTION_RULES),
        get_prompt("gen.explanation_rules", _EXPLANATION_RULES),
    ]
    if qtype == "TRUE_OR_FALSE":
        parts.append(get_prompt("gen.true_false_rules", _TRUE_FALSE_RULES))
    if qtype == "MORE_THAN_ONE_MULTIPLE_CHOICE":
        parts.append(get_prompt("gen.more_than_one_rules", _MORE_THAN_ONE_RULES))
    if qtype == "FIB_CODING":
        parts.append(get_prompt("gen.fib_rules", _FIB_RULES))
    if qtype == "REARRANGE":
        parts.append(get_prompt("gen.rearrange_rules", _REARRANGE_RULES))
    if qtype in CODE_PATH_TYPES:
        parts.append(get_prompt("gen.code_rules", _CODE_RULES))
    if qtype in _TYPED_ANSWER_TYPES:
        parts.append(get_prompt("gen.exact_answer_rules", _EXACT_ANSWER_RULES))
    if _course_is_sql():
        parts.append(get_prompt("gen.sql_rules", _SQL_RULES))
    parts.append(f"Difficulty: {difficulty_of(lo)}.")
    if lo.get("is_scenario") or (lo.get("bloom_level_raw") or "").lower() == "scenario":
        parts.append(get_prompt("gen.scenario_rules", _SCENARIO_RULES))
    parts.append(get_prompt("gen.final_validation", _FINAL_VALIDATION))
    return "\n\n".join(parts)


_SCENARIO_RULES = register("gen.scenario_rules", (
    "SCENARIO FRAMING — this outcome is a SCENARIO (apply-in-a-novel-situation) item. Build the "
    "stem as a SHORT, SELF-CONTAINED, GENERIC situation the learner has NOT seen verbatim in the "
    "material, then ask them to APPLY the taught concept/method to it (predict, choose, diagnose, "
    "or decide). Describe the situation fully in the stem (no outside facts); it must be answerable "
    "using ONLY the taught concept plus the learner's prerequisites. Do NOT reference a source-local "
    "example entity — invent a fresh, plausible, generic case. Keep ONE unambiguous correct answer."
))


def _ground(lo: dict, max_seq: int | None) -> str:
    """Grounding context for generation/review.

    Leads with the PRIMARY span the outcome was grounded in (its evidence quote + the
    section it was drawn from) — the question must be answerable from THAT. Then the
    FULL current-session reading material as supporting BACKGROUND (so the model never
    declares a concept 'not in the material' just because a retrieved slice missed it),
    plus RAG-retrieved related/prior sections for cross-session context.
    """
    query = " ".join(p for p in (lo.get("concept"), lo.get("sub_concept"), lo.get("syntax")) if p)
    secs = rag_api.search_reading_material(query or lo.get("outcome", ""), top_k=6)
    if max_seq is not None:
        secs = [s for s in secs if s.get("seq", 0) <= max_seq]
    related = "\n\n".join(f"[{s.get('seq')} {s.get('unit_name')} > {s.get('section')}]\n{s.get('snippet')}"
                          for s in secs)
    try:
        session_text = (scope.get_adapter().reading_material or "").strip()
    except Exception:  # noqa: BLE001 — no adapter bound (e.g. unit test); fall back to RAG only
        session_text = ""
    # PRIMARY anchor — the exact span this outcome was grounded in. source_evidence is a
    # quote string on the legacy LO (a {quote,section} dict on the new outcome); the
    # section text rides along via source_section_text (added at the bridge).
    se = lo.get("source_evidence")
    ev = se.strip() if isinstance(se, str) else ((se or {}).get("quote", "").strip() if isinstance(se, dict) else "")
    sec = (lo.get("source_section_text") or "").strip()
    parts = []
    primary = []
    if ev:
        primary.append('EVIDENCE — the exact text this outcome is grounded in; the question '
                       f'MUST be answerable from this:\n"{ev}"')
    if sec:
        primary.append("SECTION the outcome was drawn from:\n" + sec)
    if primary:
        parts.append("PRIMARY GROUNDING (anchor the question here):\n" + "\n\n".join(primary))
    if session_text:
        parts.append("BACKGROUND — full current-session reading material (supporting context only):\n"
                     + session_text)
    if related:
        parts.append("RELATED / PRIOR COURSE MATERIAL (retrieved, background):\n" + related)
    return "\n\n---\n\n".join(parts) or "(no matching course material found)"


def _lo_block(lo: dict) -> str:
    keys = ("outcome", "concept", "sub_concept", "description", "learner_action", "syntax", "source_evidence")
    return "\n".join(f"{k}: {lo.get(k)}" for k in keys if lo.get(k))


def _invoke(qtype: str, lo: dict, ctx: str, schema, extra: str = ""):
    sys = _sys_for(qtype, lo)
    user = f"{extra}\nLEARNING OUTCOME (intent):\n{_lo_block(lo)}\n\nCOURSE MATERIAL (ground truth):\n{ctx}"
    return _model().with_structured_output(schema).invoke(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}]
    )


# --- one agent per type (each returns a lean pydantic model) ---------------- #
# Shared code-analysis strategy: a single priority-ordered angle policy for all
# three CODE_ANALYSIS_* types (replaces the old per-type angle text).
_CODE_STRATEGY = """\
Choose the analysis angle that most directly measures the Learning Outcome.

Priority:
1. Output Prediction
2. Error Identification
3. Identify-and-Fix
4. Functionality
5. Logic Analysis
6. Equivalent Code

Use the highest-priority angle that naturally fits the LO; do not choose an angle merely for variety. Use the error/fix angles ONLY when the LO is actually about correctness, syntax, or structure — never manufacture an error for a purely conceptual LO."""

# Per-type instruction blocks (DB-overridable).
register("gen.extra.MULTIPLE_CHOICE", """\
Choose the simplest MCQ structure that directly measures the Learning Outcome.

Preference order:
1. Direct best-answer question
2. Choose the correct statement
3. Choose the incorrect statement
4. Assertion–Reason

Use Assertion–Reason only when the LO explicitly involves causal reasoning.

If the concept is narrow, build distractors from sibling or contrasting taught concepts rather than rewordings of the key; otherwise use a choose-the-incorrect framing.

Generate one stem and four options with EXACTLY one is_correct=true.""")
register("gen.extra.TRUE_OR_FALSE",
         "Write one statement and whether it is true. If the LO is about code, its output, "
         "or its behavior, include a short snippet in `code` and judge a claim about it; "
         "otherwise leave `code` empty for a purely conceptual statement.")
register("gen.extra.MORE_THAN_ONE_MULTIPLE_CHOICE",
         "Provide a question stem and 4-6 options with 2-3 is_correct=true and at least one "
         "is_correct=false. Every false option must be genuinely incorrect per the material "
         "(not a true taught fact relabeled false); if you can't, use fewer options.")
register("gen.extra.TEXTUAL", """\
Provide a question stem and one exact expected answer.

The answer must be:
- one word
- OR two words maximum
- OR a numeric value

Do NOT use commands, code statements, expressions, or sentences.

If the natural answer is longer than two words, choose a different question type.""")
register("gen.extra.CODE_ANALYSIS_MULTIPLE_CHOICE",
         f"{_CODE_STRATEGY} Write a short snippet from taught syntax; give its real "
         "output as correct_output and 3 plausible wrong outputs.")
register("gen.extra.CODE_ANALYSIS_TEXTUAL",
         f"{_CODE_STRATEGY} Write a short snippet whose exact output is brief and "
         "unambiguous (a single value/line), and give that exact real output.")
register("gen.extra.CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE",
         f"{_CODE_STRATEGY} Write a snippet; list all TRUE statements about it and the false ones.")
register("gen.extra.FIB_CODING",
         "Write a stem stating what to accomplish, then a COMPLETE runnable program "
         "(taught syntax only) with the sentinel {{BLANK}} on the ONE line to complete. "
         "Provide blank_answer (a correct completion), test_input (stdin, or \"\" if none), "
         "and test_output = the EXACT stdout the correctly-filled program prints. It is "
         "graded by RUNNING the code and comparing output, so ensure it actually runs and "
         "prints test_output.")
register("gen.extra.REARRANGE",
         "Provide a question stem and 3-6 ordered steps/lines in the CORRECT sequence "
         "(first to last). Each item must be a CONCRETE action or a real code line — never "
         "a concept, goal, or 'understand X'. Use only steps grounded in the material, with "
         "ONE unambiguous canonical order.")


def _extra(qtype: str) -> str:
    return get_prompt(f"gen.extra.{qtype}")


def agent_multiple_choice(lo, ctx):
    return _invoke("MULTIPLE_CHOICE", lo, ctx, MCQLean, _extra("MULTIPLE_CHOICE"))

def agent_true_or_false(lo, ctx):
    return _invoke("TRUE_OR_FALSE", lo, ctx, TrueFalseLean, _extra("TRUE_OR_FALSE"))

def agent_more_than_one(lo, ctx):
    return _invoke("MORE_THAN_ONE_MULTIPLE_CHOICE", lo, ctx, MCQLean, _extra("MORE_THAN_ONE_MULTIPLE_CHOICE"))

def agent_textual(lo, ctx):
    return _invoke("TEXTUAL", lo, ctx, TextualLean, _extra("TEXTUAL"))

def agent_code_mcq(lo, ctx):
    return _invoke("CODE_ANALYSIS_MULTIPLE_CHOICE", lo, ctx, CodeMCQLean, _extra("CODE_ANALYSIS_MULTIPLE_CHOICE"))

def agent_code_textual(lo, ctx):
    return _invoke("CODE_ANALYSIS_TEXTUAL", lo, ctx, CodeTextualLean, _extra("CODE_ANALYSIS_TEXTUAL"))

def agent_code_more_than_one(lo, ctx):
    return _invoke("CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE", lo, ctx, CodeMoreThanOneLean,
                   _extra("CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE"))

def agent_fib_coding(lo, ctx):
    return _invoke("FIB_CODING", lo, ctx, FibCodingLean, _extra("FIB_CODING"))

def agent_rearrange(lo, ctx):
    return _invoke("REARRANGE", lo, ctx, RearrangeLean, _extra("REARRANGE"))


_AGENTS = {
    "MULTIPLE_CHOICE": agent_multiple_choice,
    "TRUE_OR_FALSE": agent_true_or_false,
    "MORE_THAN_ONE_MULTIPLE_CHOICE": agent_more_than_one,
    "TEXTUAL": agent_textual,
    "CODE_ANALYSIS_MULTIPLE_CHOICE": agent_code_mcq,
    "CODE_ANALYSIS_TEXTUAL": agent_code_textual,
    "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE": agent_code_more_than_one,
    "FIB_CODING": agent_fib_coding,
    "REARRANGE": agent_rearrange,
}

_SCHEMA_BY_TYPE = {
    "MULTIPLE_CHOICE": MCQLean,
    "TRUE_OR_FALSE": TrueFalseLean,
    "MORE_THAN_ONE_MULTIPLE_CHOICE": MCQLean,
    "TEXTUAL": TextualLean,
    "CODE_ANALYSIS_MULTIPLE_CHOICE": CodeMCQLean,
    "CODE_ANALYSIS_TEXTUAL": CodeTextualLean,
    "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE": CodeMoreThanOneLean,
    "FIB_CODING": FibCodingLean,
    "REARRANGE": RearrangeLean,
}

_FALLBACK_TYPE = "MULTIPLE_CHOICE"


def _coverage(lo: dict, qtype: str, max_seq: int | None):
    if qtype not in CODE_PATH_TYPES:
        return True, None
    # FIB needs an explicit, taught blank syntax. With no LO syntax the concept-only
    # probe rubber-stamps any taught concept, so FIB invents shell syntax. Force the
    # existing MCQ fallback instead.
    if qtype == "FIB_CODING" and not (lo.get("syntax") or "").strip():
        return False, {"covered": False,
                       "reason": "FIB requires explicit blank syntax taught in the session"}
    cov = lo.get("code_coverage") or rag_api.code_coverage(
        lo.get("concept") or lo.get("outcome", ""), syntax=lo.get("syntax") or None, max_seq=max_seq)
    return bool(cov.get("covered")), cov


# --- creation-layer grounding gate ----------------------------------------- #
# Enforce, AT CREATION, that the stem and EVERY option use only concepts/keywords
# taught in this session or an earlier one (RAG check_concept is scoped to the
# course + prerequisite courses). Ungrounded named terms trigger an in-place
# regeneration here, so the question reaches review already grounded.
MAX_GROUNDING_FIXES = 2
_GROUNDING_TERM_CAP = 14

_GT_BACKTICK = re.compile(r"`([^`]+)`")
_GT_DOTTED = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")     # obj.method / mod.attr
_GT_CALL = re.compile(r"\b[A-Za-z_]\w*\(\)")                        # func()
_GT_VERSIONED = re.compile(r"\b[A-Za-z][\w-]*==?\d[\w.]*\b")        # django==3.0
_GT_PROPER = re.compile(r"[A-Z][A-Za-z]{2,}")                       # Django, Flask
# Capitalized words that are ordinary prose / boolean / IO — never "named entities".
_GT_PROPER_STOP = {
    "The", "This", "That", "These", "Those", "There", "Their", "They", "Then", "Than",
    "True", "False", "None", "Null", "Yes", "Not", "Output", "Outputs", "Input", "Inputs",
    "Error", "Errors", "Value", "Values", "Result", "Results", "Code", "Program",
    "Function", "Functions", "Variable", "Variables", "List", "Lists", "String", "Strings",
    "Number", "Numbers", "Object", "Objects", "Class", "Classes", "Method", "Methods",
    "Loop", "Loops", "When", "Where", "Which", "What", "Why", "How", "Who", "Whose",
    "Its", "Else", "For", "While", "And", "But", "Both", "Each", "Every", "All", "Any",
    "Some", "Use", "Used", "Using", "Return", "Returns", "Print", "Prints", "Allow",
    "Allows", "Ensure", "Ensures", "Avoid", "Avoids", "Reduce", "Reduces", "Create",
    "Creates", "Add", "Adds", "Remove", "Removes", "First", "Second", "Third", "Last",
    "Next", "Previous", "Same", "Different", "Correct", "Incorrect", "Only", "Also",
}


def _named_candidates(text: str) -> set[str]:
    """Named entities a learner would need taught BY NAME — code identifiers, dotted
    calls, versioned tokens, and non-prose proper nouns. Deliberately conservative
    (matches the review-layer policy) so paraphrased prose isn't false-flagged."""
    cands: set[str] = set()
    for span in _GT_BACKTICK.findall(text):
        cands.update(re.findall(r"[A-Za-z_]\w*(?:\.\w+)?(?:\(\))?", span))
    cands.update(_GT_DOTTED.findall(text))
    cands.update(_GT_CALL.findall(text))
    cands.update(_GT_VERSIONED.findall(text))
    for seg in re.split(r"[.\n;:!?]", text):                       # skip sentence-initial caps
        for w in re.findall(r"[A-Za-z][A-Za-z0-9+#-]*", seg)[1:]:
            if _GT_PROPER.fullmatch(w) and w not in _GT_PROPER_STOP:
                cands.add(w)
    return {c.strip() for c in cands if len(c.strip()) >= 2}


def _lean_texts(lean: dict) -> str:
    """All learner-facing text whose terms must be taught — stem + options + answers
    + explanation (NOT the code body; its syntax is gated separately via
    code_coverage / used_syntax / _enforce_code_grounding)."""
    parts = [lean.get("question", ""), lean.get("statement", ""), lean.get("explanation", "")]
    parts += [o.get("content", "") for o in (lean.get("options") or [])]
    parts += list(lean.get("wrong_answers") or [])
    parts += list(lean.get("correct_outputs") or [])
    parts += [lean.get(k, "") for k in ("correct_output", "answer", "expected_output", "blank_answer")]
    parts += list(lean.get("ordered_items") or [])
    return "\n".join(p for p in parts if p)


def _ungrounded_named(text: str) -> list[str]:
    seen: set[str] = set()
    uncovered: list[str] = []
    for term in _named_candidates(text):
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > _GROUNDING_TERM_CAP:
            break
        verdict = (rag_api.check_concept(term).get("verdict") or "").split("\n", 1)[0].upper()
        if "NOT EXPLAINED" in verdict:
            uncovered.append(term)
    return uncovered


def _enforce_grounding(lo: dict, res: dict, max_seq: int | None) -> dict:
    qtype = res["question_type"]
    fix_lo = {**lo, "question_type": qtype}
    ctx = _ground(fix_lo, max_seq)
    lean = res["lean"]
    uncovered = _ungrounded_named(_lean_texts(lean))
    attempts = 0
    while uncovered and attempts < MAX_GROUNDING_FIXES:
        issues = [{
            "severity": "high", "rule": "GROUNDING",
            "problem": (f"the stem/options use terms not taught in this or any earlier "
                        f"session: {', '.join(uncovered)} — a learner can only judge an "
                        f"option using concepts they were actually taught"),
            "suggested_fix": (f"reframe the question and ALL options using ONLY concepts "
                              f"taught in the material; remove {', '.join(uncovered)} and "
                              f"introduce no other untaught named term"),
        }]
        lean = fix_lean(fix_lo, ctx, lean, issues)
        attempts += 1
        uncovered = _ungrounded_named(_lean_texts(lean))
    res["lean"] = lean
    res["grounding"] = {**(res.get("grounding") or {}),
                        "creation_fix_attempts": attempts,
                        "remaining_uncovered_terms": uncovered}
    return res


def _fallback_to_mcq(lo: dict, res: dict, max_seq: int | None, reason: str) -> dict:
    """Convert a code question into a grounded MCQ (the proven-clean path)."""
    fb = {"from": res["question_type"], "reason": reason}
    fb_lo = {**lo, "question_type": _FALLBACK_TYPE}
    lean2 = _AGENTS[_FALLBACK_TYPE](fb_lo, _ground(fb_lo, max_seq)).model_dump()
    res = {**res, "question_type": _FALLBACK_TYPE, "lean": lean2, "fallback": fb}
    return _enforce_grounding(lo, res, max_seq)   # re-ground the new MCQ


def _enforce_code_grounding(lo: dict, res: dict, max_seq: int | None) -> dict:
    """Verify the EMITTED code body uses only taught NAMED constructs; if not, fall
    back to a grounded MCQ instead of shipping invented/untaught syntax. (FIB blanks
    are NOT format-checked here — FIB is graded by execution, so any completion that
    produces the expected output is valid; see _verify_fib.)"""
    lean = res["lean"]
    code = lean.get("code") or "\n".join(lean.get("code_lines") or [])
    uncovered = _ungrounded_named(code) if code.strip() else []
    if not uncovered:
        return res
    return _fallback_to_mcq(lo, res, max_seq, f"code uses untaught constructs: {', '.join(uncovered)}")


# Installation / shell / environment-setup commands — a FIB must NEVER be one of
# these; it must be a runnable program that reads input and produces output.
_INSTALL_CMD_RE = re.compile(
    r"\b(pip3?|conda|apt|apt-get|brew|npm|npx|yarn|pnpm|gem|cargo|gradle|mvn|go)\s+(install|add|i|get)\b"
    r"|python3?\s+-m\s+(venv|pip)"
    r"|\b(virtualenv|sudo|chmod|chown|mkdir|rmdir)\b"
    r"|\bcd\s+\S"
    r"|source\s+\S+/bin/activate"
    r"|\bexport\s+[A-Z_]+=",
    re.I,
)


def _is_install_command(text: str) -> bool:
    return bool(_INSTALL_CMD_RE.search(text or ""))


def _fill_fib(lean: dict) -> str:
    """The runnable program with the blank filled by the model's answer."""
    return "\n".join(lean.get("code_lines") or []).replace("{{BLANK}}", lean.get("blank_answer") or "")


def _verify_fib(lo: dict, res: dict, max_seq: int | None) -> dict:
    """Execution-based FIB check (how the platform grades): fill the blank, run on
    the test input, require stdout == expected output. On mismatch, repair once with
    the run diff; if it still fails (or the language isn't executable), fall back to
    a grounded MCQ."""
    lean = res["lean"]
    # Hard guardrail: a FIB must be a runnable input->output program — never an
    # installation/shell/setup command. Such "command blanks" become an MCQ.
    if _is_install_command(_fill_fib(lean)) or _is_install_command(lean.get("blank_answer") or ""):
        return _fallback_to_mcq(lo, res, max_seq,
                                "FIB used an installation/shell command, not a runnable input->output program")
    from app.core.config import settings
    if not settings.fib_verify:
        return res
    from app.mcq_pipeline.utils import code_exec

    lang = lean.get("code_language") or "PYTHON"
    if not code_exec.language_supported(lang):
        return _fallback_to_mcq(lo, res, max_seq, f"FIB language {lang!r} not executable for verification")

    v = code_exec.verify_output(lang, _fill_fib(lean), lean.get("test_input") or "",
                                lean.get("test_output") or "")
    res["fib_verification"] = v
    if v.get("matched"):
        return res

    # repair once with the execution diff
    fix_lo = {**lo, "question_type": "FIB_CODING"}
    ctx = _ground(fix_lo, max_seq)
    issue = [{
        "severity": "high", "rule": "FIB EXECUTION",
        "problem": (f"Filling the blank with {lean.get('blank_answer')!r} and running the program on the "
                    f"given input did NOT produce the stated expected output. actual={v.get('actual','')[:200]!r} "
                    f"expected={(lean.get('test_output') or '')[:200]!r} stderr={v.get('stderr','')[:150]!r}"),
        "suggested_fix": ("Make code_lines a COMPLETE runnable program; ensure the correct blank completion, "
                          "run on test_input, prints EXACTLY test_output; set test_output to that real output."),
    }]
    lean2 = fix_lean(fix_lo, ctx, lean, issue)
    res["lean"] = lean2
    v2 = code_exec.verify_output(lang, _fill_fib(lean2), lean2.get("test_input") or "",
                                 lean2.get("test_output") or "")
    res["fib_verification"] = {**v2, "repaired": True}
    if v2.get("matched"):
        return res
    return _fallback_to_mcq(lo, res, max_seq,
                            f"FIB failed execution verification after repair (ran={v2.get('ran')}, "
                            f"matched={v2.get('matched')})")


def _verify_code_output(lo: dict, res: dict, max_seq: int | None) -> dict:
    """For an output-prediction CODE_ANALYSIS_TEXTUAL, run the SHOWN code and make the
    expected output the program's REAL stdout — so the answer key can't be a wrong
    LLM guess. Only corrects when the code runs cleanly; an erroring snippet is left
    untouched (it may be an error/behavior question)."""
    from app.core.config import settings
    if not settings.fib_verify:
        return res
    from app.mcq_pipeline.utils import code_exec

    lean = res["lean"]
    code = lean.get("code") or ""
    lang = lean.get("code_language") or "PYTHON"
    if not code.strip() or not code_exec.language_supported(lang):
        return res
    r = code_exec.run_code(lang, code, "", None)
    res["code_exec"] = {"ran": r.get("ran"), "timed_out": r.get("timed_out")}
    if not r.get("ran") or r.get("timed_out"):
        return res
    actual = "\n".join(ln.rstrip() for ln in (r.get("stdout") or "").splitlines()).strip()
    expected = "\n".join(ln.rstrip() for ln in (lean.get("expected_output") or "").splitlines()).strip()
    if actual != expected:
        lean["expected_output"] = (r.get("stdout") or "").strip()
        res["code_exec"]["corrected_expected_output"] = True
    return res


def _opt_issue(rule: str, problem: str) -> dict:
    return {"severity": "high", "rule": rule, "problem": problem,
            "suggested_fix": "fix only this; keep the question grounded and the type unchanged"}


def _enforce_options(lo: dict, res: dict, max_seq: int | None) -> dict:
    """Deterministic option guards for option MCQs: enforce count + exactly-one/2-3
    correct and de-duplicate near-identical options, feeding precise issues to the
    fix loop so these never reach review."""
    qtype = res["question_type"]
    if qtype not in ("MULTIPLE_CHOICE", "MORE_THAN_ONE_MULTIPLE_CHOICE"):
        return res
    fix_lo = {**lo, "question_type": qtype}
    ctx = _ground(fix_lo, max_seq)
    for _ in range(2):                      # separate small option-fix budget
        lean = res["lean"]
        opts = lean.get("options") or []
        n = len(opts)
        ncorrect = sum(1 for o in opts if o.get("is_correct"))
        issues: list[dict] = []
        if qtype == "MULTIPLE_CHOICE":
            if n != 4:
                issues.append(_opt_issue("OPTION RULES", f"must have exactly 4 options (has {n})"))
            if ncorrect != 1:
                issues.append(_opt_issue("OPTION RULES", f"exactly ONE option must be correct (has {ncorrect})"))
        else:
            if not (4 <= n <= 6):
                issues.append(_opt_issue("OPTION RULES", f"must have 4-6 options (has {n})"))
            if ncorrect < 2:
                issues.append(_opt_issue("MORE-THAN-ONE", f"at least 2 options must be correct (has {ncorrect})"))
            if n and ncorrect >= n:
                issues.append(_opt_issue("MORE-THAN-ONE", "at least 1 option must be incorrect"))
        for i in range(n):
            for j in range(i + 1, n):
                a = (opts[i].get("content") or "").strip().lower()
                b = (opts[j].get("content") or "").strip().lower()
                if a and b and difflib.SequenceMatcher(None, a, b).ratio() >= 0.82:
                    issues.append(_opt_issue(
                        "DISTRACTOR RULES",
                        f"two options express the same idea ('{opts[i].get('content')}' / "
                        f"'{opts[j].get('content')}') — make each option distinct and genuinely incorrect"))
        if not issues:
            break
        res["lean"] = fix_lean(fix_lo, ctx, lean, issues)
    return res


def _strip_trailing_period(text: str) -> str:
    """Remove a single trailing full stop from learner-facing text. Preserves an
    ellipsis ('...') and leaves all other punctuation untouched — only a bare
    trailing '.' is dropped (per the no-trailing-period rule for stems/options)."""
    if not isinstance(text, str):
        return text
    t = text.rstrip()
    if t.endswith(".") and not t.endswith(".."):
        t = t[:-1].rstrip()
    return t


def _normalize_lean_text(lean: dict) -> dict:
    """In-place: strip a trailing period from the question stem / statement and from
    each option's content. Graded values (TEXTUAL/FIB answers, expected output) and
    `code` are intentionally left literal."""
    if not isinstance(lean, dict):
        return lean
    for fld in ("question", "statement"):
        if isinstance(lean.get(fld), str):
            lean[fld] = _strip_trailing_period(lean[fld])
    for o in (lean.get("options") or []):
        if isinstance(o, dict) and isinstance(o.get("content"), str):
            o["content"] = _strip_trailing_period(o["content"])
    return lean


def generate_lean(lo: dict, *, max_seq: int | None = None, ctx: str | None = None,
                  fallback_uncovered: bool = True) -> dict:
    """Route the LO to its type-agent and return the lean content (or skip).
    Enforces, AT CREATION (so review rarely intervenes): stem/option grounding,
    code-body grounding (with MCQ fallback), and option count/dedupe. Records every
    RAG call made under `rag_calls`."""
    with scope.recording() as rag_calls:
        res = _generate_lean(lo, max_seq=max_seq, ctx=ctx, fallback_uncovered=fallback_uncovered)
        if res.get("status") == "generated" and res.get("lean"):
            res = _enforce_grounding(lo, res, max_seq)
            if res["question_type"] in CODE_PATH_TYPES:
                res = _enforce_code_grounding(lo, res, max_seq)
            if res["question_type"] == "FIB_CODING":
                res = _verify_fib(lo, res, max_seq)   # run it; repair once; else MCQ
            elif res["question_type"] == "CODE_ANALYSIS_TEXTUAL":
                res = _verify_code_output(lo, res, max_seq)   # set the key to the real stdout
            res = _enforce_options(lo, res, max_seq)
        if res.get("lean"):
            _normalize_lean_text(res["lean"])
    res["rag_calls"] = rag_calls
    return res


def _generate_lean(lo: dict, *, max_seq: int | None = None, ctx: str | None = None,
                   fallback_uncovered: bool = True) -> dict:
    qtype = lo.get("question_type")
    if qtype not in _AGENTS:
        return {"status": "skipped", "question_type": qtype, "reason": f"unknown type {qtype!r}"}

    fallback = None
    covered, cov = _coverage(lo, qtype, max_seq)
    if not covered:
        if not fallback_uncovered:
            return {"status": "skipped", "question_type": qtype,
                    "reason": "syntax/concept not yet covered in the RAG", "grounding": cov}
        fallback = {"from": qtype, "reason": "syntax/concept not yet covered", "grounding": cov}
        qtype = _FALLBACK_TYPE
        lo = {**lo, "question_type": qtype}
        ctx = None

    if ctx is None:
        ctx = _ground(lo, max_seq)
    lean = _AGENTS[qtype](lo, ctx)
    res = {"status": "generated", "question_type": qtype,
           "difficulty": difficulty_of(lo), "lean": lean.model_dump()}
    if fallback:
        res["fallback"] = fallback
    return res


_FIX_PREFIX = register("gen.fix_prefix", (
    "You previously wrote the {qtype} question shown below, and a reviewer flagged "
    "the listed issues. Produce a CORRECTED version that fixes ONLY those issues "
    "and leaves everything else intact. Do not change the question type, do not "
    "flip a correct answer to incorrect (or vice versa), and stay grounded in the "
    "course material.\n"
    "- If a GROUNDING issue lists untaught terms, you MUST remove EVERY listed term "
    "from the stem, ALL options, AND the explanation, and must NOT introduce ANY "
    "replacement term that is absent from the COURSE MATERIAL. Rephrase using only "
    "concepts already present in the material; if that makes the option/stem weaker, "
    "that is acceptable — an ungrounded question is not.\n"
    "- For an MULTIPLE_CHOICE with 'more than one correct answer', keep EXACTLY ONE "
    "option correct and make the rest genuinely incorrect (but still grounded).\n"
    "- For a DISTRACTOR DEPTH issue, the flagged wrong option is wrong for a reason the "
    "material does not teach deeply enough for a learner to recognize. Rebuild that option "
    "so a learner can tell it is wrong using ONLY facts explicitly stated in the material "
    "or simple reasoning from taught concepts; if the material lacks that, replace it with "
    "a recognition-level wrong option built from a DIFFERENT taught concept.\n"
    "- Keep every option a single short phrase — never a full sentence or an "
    "explanation.\n\nPREVIOUS QUESTION:\n{prev}\n\nREVIEWER ISSUES:\n{issues}\n"
))


def fix_lean(lo: dict, ctx: str, prev_lean: dict, issues: list[dict]) -> dict:
    """Targeted regeneration: re-run the SAME type-agent with the previous question
    + the reviewer's issues, asking it to fix only the flagged problems."""
    qtype = lo.get("question_type")
    schema = _SCHEMA_BY_TYPE[qtype]
    issues_txt = "\n".join(
        f"- [{i.get('severity')}] {i.get('rule')}: {i.get('problem')} "
        f"(suggested fix: {i.get('suggested_fix')})" for i in issues) or "(none)"
    extra = get_prompt("gen.fix_prefix", _FIX_PREFIX).format(qtype=qtype, prev=prev_lean, issues=issues_txt)
    lean = _invoke(qtype, lo, ctx, schema, extra)
    return _normalize_lean_text(lean.model_dump())


def generate_for_los(los: list[dict], *, max_seq: int | None = None,
                     fallback_uncovered: bool = True, workers: int = 8,
                     on_progress=None) -> list[dict]:
    """Generate the LEAN question content per LO, concurrently. `on_progress()`
    fires per finished LO."""
    def _one(lo: dict) -> dict:
        res = generate_lean(lo, max_seq=max_seq, fallback_uncovered=fallback_uncovered)
        res["outcome"] = lo.get("outcome")
        if on_progress:
            on_progress()
        return res
    return pmap(_one, los, workers=workers)
