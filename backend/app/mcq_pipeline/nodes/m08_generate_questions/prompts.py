"""Question pipeline · Node 8 — generate_questions · prompts + type constants.

Every reusable guideline block is registered with `prompt_store` (DB-overridable) and
fetched at call time, so editing a `gen.*` prompt updates BOTH generation here and the
review pass (which re-applies the same blocks). Also holds the small type-set constants
and the difficulty mapper that the rest of the package shares.
"""
from __future__ import annotations

from app.mcq_pipeline.prompts.store import register

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

_TYPED_ANSWER_TYPES = {"TEXTUAL", "CODE_ANALYSIS_TEXTUAL", "FIB_CODING"}

_DIFFICULTY = {"remember": "EASY", "understand": "MEDIUM", "apply": "MEDIUM",
               "scenario": "HARD", "implement": "HARD"}


def difficulty_of(lo: dict) -> str:
    # Prefer the 4-tier bloom_level_raw (carries 'scenario'); fall back to legacy bloom_category.
    tier = (lo.get("bloom_level_raw") or lo.get("bloom_category") or "").lower()
    return _DIFFICULTY.get(tier, "MEDIUM")


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
- Embed EVERY detail needed to answer directly in the stem. Never defer to the source — not in the STEM, the OPTIONS, or the EXPLANATION — with phrases like 'according to the material', 'as described in the course material', 'as stated in the course material', 'based on the lesson', 'in the reading/passage', 'as discussed', or 'from this session'. State the fact directly instead (write "Python powers the backends of large-scale systems", never "Python powers ... as described in the course material").
- Never reference a source-local example entity the learner cannot see — a scenario label ('Project A'/'Project B'), a sample file/variable/function name, or a one-off value from the reading. If a scenario is needed, define it fully and generically IN THE STEM so it stands alone.
- Test transferable understanding of the concept, not recall of an arbitrary detail from one example.

NO PHANTOM CODE — only CODE_ANALYSIS_* questions display a snippet (in the `code` field). For a MULTIPLE_CHOICE / TRUE_OR_FALSE / MORE_THAN_ONE_MULTIPLE_CHOICE, NEVER refer to a snippet the question does not show: no "the following code", "the given program", "the code below", or "what does this output". If the outcome needs a snippet shown, it must be a CODE_ANALYSIS_* question, not this type. For a CODE_ANALYSIS_* question, put the snippet in the `code` field and have the stem REFER to it ("the given code snippet") — never paste the code (or a ``` fenced block) into the stem.

Do not reveal the answer in the stem.

Do not end the stem with a trailing full stop ('.'). A question mark ('?') or a colon introducing options is fine; a bare trailing period is not.""")

_MARKDOWN_RULES = register("gen.markdown_rules", """\
MARKDOWN FORMATTING

The portal renders the question text and the explanation as MARKDOWN. Write them as clean Markdown, matching the house style of the question bank:
- INLINE CODE (REQUIRED) — wrap EVERY code-level token you mention in prose in `backticks`: variable and identifier names, function/method names, keywords, operators, literal values, and program output. This applies to the STEM, the OPTIONS, and the EXPLANATION. Examples: "store the result in `result`", "compare `a` and `b`", "the loop calls `range()`", "it prints `10`". Never write a bare variable name or value in prose without backticks. Backtick ONLY genuine code tokens — NEVER wrap an ordinary English word or concept name in backticks (write "the request-response flow", NOT "the `request-response flow`"; "object-oriented programming", NOT "`object-oriented programming`"). Backticks around plain prose read as machine-generated and are rejected.
- BOLD THE PIVOTAL VALUE — when the question turns on ONE specific value, result, or output, put THAT term in **bold** (e.g. "the snippet prints **10**", "the expression evaluates to **True**", "this returns **equal**"). Use bold ONLY for that pivot, not decoratively, and not on every key term.
- Keep the stem a SINGLE SHORT PARAGRAPH. Do NOT use headings (#), and do NOT use bullet or numbered lists in the stem (the question bank never does) — write the prompt as running prose. Reserve a '-' list only for a genuine enumeration in the EXPLANATION.
- Real code goes in the dedicated `code` field, NEVER as a fenced ``` block inside the stem.
- Separate EVERY block-level element (each paragraph, each list) with a BLANK LINE — the portal's renderer needs a blank line between blocks or they merge.

OPTIONS:
- Prose/conceptual options MAY use light inline Markdown (`code`, **bold**). When an option actually contains such Markdown, set its `content_type` to "MARKDOWN"; otherwise leave it "TEXT".
- For CODE_ANALYSIS_* question types, options are literal code or program output — keep them PLAIN TEXT (no Markdown, no backticks), `content_type` "TEXT", so the portal shows them verbatim.

For PROSE/CONCEPTUAL MCQs ONLY (MULTIPLE_CHOICE / MORE_THAN_ONE_MULTIPLE_CHOICE — never CODE_ANALYSIS_*): when an option names a code identifier, keyword, command, value, or output, wrap it in `backticks` and set that option's `content_type` to "MARKDOWN". A conceptual MCQ whose options mention technical terms should use Markdown options, not bare text. (CODE_ANALYSIS_* options stay PLAIN TEXT per the rule above — this never overrides it.)

PLAIN, HUMAN TYPOGRAPHY — write the stem, options, and explanation the way a person would type them: use a normal hyphen '-' (or rephrase) and straight quotes. Do NOT use en/em dashes ('–', '—'), curly/smart quotes, or other "AI-looking" typography anywhere in the question — reviewers reject these as machine-generated.

NEVER apply Markdown to a graded exact-answer value (a TEXTUAL / FIB answer or expected output) or to the `code` field — those must stay literal.""")

_OPTION_RULES = register("gen.option_rules", """\
OPTION RULES

Create exactly 4 options unless the question type requires otherwise.

All options must be semantically DISTINCT — no two options may express the same idea in different words, and no distractor may be a paraphrase of the correct answer (it must be genuinely incorrect, not a reworded correct answer).

Options must be SELF-CONTAINED: never reference a source-local example entity the learner cannot see (e.g. "Project A"/"Project B", a sample file/variable name from the reading). Any context an option needs must be stated generically IN THE STEM. (Do not introduce untaught named technologies either — see GROUNDING RULES.)

Each option must be:
- a CONCISE PHRASE, not a full sentence — give just the distinguishing content
- NOT prefixed with a leading article ('The', 'A', 'An') — write "Set of elements common to both sets", not "The set of elements common to both sets". Drop the article from EVERY option so they stay parallel and scannable.
- scannable
- grammatically consistent (all options the same grammatical form)
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
- be a NEGATION, OPPOSITE, or BLANKET DENIAL of the correct answer. If the key is "stores data, processes requests, and returns results", do NOT write distractors like "does not store or process any data", "only displays static content", or "has no role in processing" — a learner eliminates these WITHOUT understanding the concept, which gives the answer away.

Every distractor must be a SPECIFIC, self-standing claim of the SAME polarity, scope, and richness as the correct answer — a different plausible answer a learner could genuinely choose, NOT a weakened, reversed, or "none of this happens" version of the key. Each must require understanding the concept to reject.

A distractor should fail because it misunderstands the target concept — never because it is self-evidently false or the mirror image of the key.

OPTION BALANCE RULES

The correct answer must not stand out because of:
- length
- terminology
- formatting
- specificity

All options should appear equally credible at first glance. In particular, the correct
option must NOT be identifiable just because it is the only POSITIVE, the only COMPREHENSIVE,
or the LONGEST statement while the distractors are short denials — that pattern gives the answer away.

Vary the position of the correct answer across questions.

Avoid giveaway qualifiers in options — absolutes and blanket-scope words ('always', 'never', 'all', 'none', 'only', 'solely', 'just', 'merely', 'any', 'no', 'nothing', 'does not', 'cannot') read as giveaways or are unfalsifiable. Never make a distractor wrong simply by attaching such a word to (or negating) the key's idea. Do NOT use 'All of the above' or 'None of the above' as options unless the Learning Outcome specifically requires that judgment; prefer concrete, comparable alternatives.

The correct option must be the most TECHNICALLY ACCURATE answer the material supports — not the 'commonly recommended' or 'best-practice' one — UNLESS the stem explicitly asks for the standard/recommended practice. (A distractor that is also defensible as 'best practice' would create a second valid answer.)

ANSWER VALIDITY & ALIGNMENT

Every option must answer the EXACT question asked — same subject and scope as the stem. Do not offer options about a neighbouring topic the stem did not ask about (an option that is true-but-about-something-else reads as "not aligned with the question").

Exactly ONE option may be defensibly correct. Before finalizing, test EACH distractor with: "could a knowledgeable person argue this is also correct, or correct 'in a general sense'?" If yes, rewrite it so it is clearly and specifically WRONG on the taught concept — never leave a second arguably-correct option (this is the single most common rejection: "X is also valid" / "this is subjective" / "option B is too similar to the correct one").

The correct answer must be fully determined by the stem alone: it must NOT rely on a detail the stem never stated (a specific entity, value, dataset, or constraint). If the key needs such a detail, put that detail in the stem — never let the answer introduce context the question didn't give.""")

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

The program must be SELF-CONTAINED: the ONLY external input is test_input (stdin). It must not read files, hit the network, touch a database, read environment variables, or import an untaught third-party library — define every value it needs inline (see CODE RULES). Do NOT copy the program verbatim from the material: rename variables/identifiers and change literal values so it tests understanding, not recall of the example.

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

SELF-CONTAINED CODE (no external resource) — the snippet must be a COMPLETE, RUNNABLE program that depends on NOTHING the learner cannot see. It must run with only the language's built-ins plus constructs taught in the material. Therefore the code must NOT:
- read or write files (open(), pathlib, fs.*, file handles), or rely on a file existing on disk;
- access the network (requests, urllib, http, socket, fetch, axios, any URL);
- connect to a database or external service (sqlite3/psycopg2/pymysql/sqlalchemy/mysql, any connect()/cursor);
- read environment variables or run shell/OS commands (os.environ, os.system, subprocess);
- import any third-party / non-standard library that the material does not teach.
All data the program needs must be defined INLINE as literals in the code (or supplied via the test stdin). If the concept seems to need an external resource, simulate it with an in-memory literal (e.g. a list/dict standing in for the file or table) so the snippet stands alone.

NOT COPIED FROM THE SOURCE — never reproduce a code snippet verbatim from the COURSE MATERIAL. The snippet must test UNDERSTANDING of the taught construct/pattern, not memory of a specific example. Keep the taught construct/pattern identical, but RE-EXPRESS the surface: rename every variable, function, and identifier to fresh, generic names and change the literal values (numbers, strings, list contents). A learner who memorized the example must still have to reason about your version.

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

_SCENARIO_RULES = register("gen.scenario_rules", (
    "SCENARIO FRAMING — this outcome is a SCENARIO (apply-in-a-novel-situation) item. Build the "
    "stem as a SHORT, SELF-CONTAINED, GENERIC situation the learner has NOT seen verbatim in the "
    "material, then ask them to APPLY the taught concept/method to it (predict, choose, diagnose, "
    "or decide). Describe the situation fully in the stem (no outside facts); it must be answerable "
    "using ONLY the taught concept plus the learner's prerequisites. Do NOT reference a source-local "
    "example entity — invent a fresh, plausible, generic case. Keep ONE unambiguous correct answer."
))

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
