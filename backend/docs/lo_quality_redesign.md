# LO Generation — Quality-First Redesign

**Status:** proposed (design doc) · **Date:** 2026-06-19
**Goal:** maximize *assessment quality* of generated Learning Outcomes. Cost, latency,
and LLM-call count are **not** constraints. Reproducibility and faithfulness to the
session beat throughput.

---

## 0. Principle

> Build a **faithful, evidence-bound model** of what the session actually *teaches*
> (every topic & concept, each with a grounded description and a measured taught-depth),
> assess **only what is genuinely explained**, and admit an outcome only when a question
> on it can be **answered from the session ∪ its prerequisite closure**.

Two mechanisms make this "quality-first" rather than "more prompts":
1. **Evidence binding** — every description / claim must trace to a verbatim source quote
   (a deterministic substring check), so the model can't assert what the text doesn't say.
2. **Adversarial verification** — multi-juror panels (independent, diverse lenses,
   majority vote), including a skeptic that *tries to answer* a sample question from only
   the allowed text. Loop until stable, not a fixed retry cap.

---

## 1. Current state (what we build on)

Pipeline today (`lo_graph.py`):
`parse_structure(D) → extract_concepts(A,K=3) → canonicalize(D) → build_dependency_graph(A,K=3)
→ profile_coverage(A,/concept) → plan_allocation(D) → author_outcomes(A,/topic)
→ resolve_prerequisites(D) → coverage_gate(A,/outcome) → validate(D,V1–V13) ↺ repair → finalize → …`

- Concepts are `{concept_id (slug), canonical_name, topic_id, in_scope, procedural, evidence{quote,section}}`
  (+ `depth_category` from `profile_coverage`). **No description.**
- Topics are `{topic_id, title, order, text, has_code}`. **No description.**
- `profile_coverage` drops concepts the RAG calls "NOT EXPLAINED" in course scope —
  scope-based, **not evidence-based per session**, and **not applied to topics**.
- `build_dependency_graph` is **concept-only**; prereqs are in-graph edges or a flat
  `assumed_prior` list.
- `coverage_gate` scores each outcome with a single "covered" rubric (V13).

This redesign **deepens four things** while reusing the graph spine: rich described
objects, an evidence-based *explained-not-named* gate (topics + concepts), a
*topic+concept* dependency graph with prerequisite **provenance**, and a rigorous
*answerability* review gate.

---

## 2. Target knowledge model (schemas)

```python
class TaughtDepth(str, Enum):
    NAMED_ONLY   = "named_only"    # mentioned, never explained  -> NOT in scope
    DEFINED      = "defined"       # a definition/what-it-is      -> Understand max
    EXPLAINED    = "explained"     # how/why, mechanism in prose  -> Understand max
    DEMONSTRATED = "demonstrated"  # worked example / walkthrough -> Understand (+Apply if procedural)
    APPLIED      = "applied"       # runnable code/command shown  -> Apply allowed

class Concept:
    concept_id: str               # stable slug
    name: str                     # canonical display name
    description: str              # 1–2 sentences of WHAT THE SESSION TEACHES about it
    evidence_quotes: list[str]    # verbatim spans the description is built from
    topic_id: str
    explained: bool               # False == named_only -> out of scope
    taught_depth: TaughtDepth
    in_scope: bool
    out_of_scope_reason: str

class Topic:I
    topic_id: str
    name: str
    description: str              # what this section teaches (grounded)
    evidence_span: str
    order: int
    explained: bool               # a heading with no real teaching -> dropped
```

**Hard rule (evidence binding):** `description` is accepted only if each of its claims
maps to one of `evidence_quotes` (markdown-insensitive substring of the source). A
description that can't be bound is rejected and re-authored. This is the structural
defense against "model wrote what it knows, not what the session says."

---

## 3. Pipeline (node by node)

### Stage 1 — Knowledge Builder (study → model)

Replaces bare extraction with deliberate passes:

1. **Topic pass** (`parse_structure` + new `describe_topics`): segment by headings (as
   today) **then** deep-read each topic to produce `name + description + evidence_span`;
   drop recap/summary and any heading with no real teaching (`explained=False`).
2. **Concept pass** (`extract_concepts`, enriched): per topic, deep-read and emit rich
   `Concept` objects `{name, description, evidence_quotes[]}`. K-sample self-consistency
   on the concept *set* (keep concepts appearing in ≥ majority of samples).
3. **Explanation + Depth gate** (`profile_coverage`, made evidence-based) — *the key
   "not just names" fix*, applied to **concepts and topics**: an evidence-bound
   classifier returns `taught_depth`. `NAMED_ONLY → in_scope=False`. The classifier is
   given **only the concept's evidence + its topic text** and must justify the depth with
   a quote; if the description isn't derivable from quotes → re-author or drop.
4. **Canonicalize** (`canonicalize`, D): merge exact/near-duplicates (existing union-find),
   carry `name+description+evidence+explained+taught_depth`.

### Stage 2 — Two-level dependency graph (`build_dependency_graph`, extended)

Graph over **{topics, concepts}**:
- `concept → concept` (prerequisite), `topic → topic` (prerequisite), `topic ⊇ concept`.
- Each prereq edge tagged with **provenance**:
  - `taught_here` — the prereq is an in-scope concept of this session.
  - `taught_earlier` — resolved via **RAG over prior sessions/courses** (the adapter is
    already scoped to course + prerequisite courses): search for the prereq; a strong hit
    in a *prior* unit records `{source_unit, evidence}`.
  - `assumed_prior` — foundational, untaught anywhere in scope.
- K-sample edge voting + deterministic acyclicity (existing `reachable` guard).
This closure is what the answerability gate consumes.

### Stage 3 — Coverage manifest (`profile_coverage` output)
`breadth` = every explained in-scope concept; `depth` = `taught_depth` histogram. Drives
allocation and is surfaced in the artifact.

### Stage 4 — Plan allocation (Understand / Apply), **depth-capped** (`plan_allocation`)
- **Breadth first:** cover every explained in-scope concept ≥ once before depth-stacking.
- **Depth ceiling:** `APPLIED/DEMONSTRATED(procedural)` → Apply allowed; `DEFINED/EXPLAINED`
  → Understand only. No Apply outcome on a concept the session only explains conceptually.
- Adaptive budget stays (cap to genuinely-teachable concepts; never pad).

### Stage 5 — Author outcomes (`author_outcomes`)
Author from rich concept objects (name+description+evidence+depth), one per slot, verb
within the depth ceiling.

### Stage 6 — Review Gate (multi-juror, the quality lever) (`coverage_gate`, upgraded)
Per outcome, an **N-juror panel** (independent, majority vote) judging the four criteria:
- **(a) breadth fit** — maps to a real *explained* in-scope concept.
- **(b) depth fit** — Bloom ≤ the concept's `taught_depth` ceiling (no over-reach).
- **(c) should-be-asked** — worthwhile, session-appropriate, **self-contained** (not a
  passing mention, not source-local trivia like "Project A").
- **(d) answerability closure** — see §4. A **skeptic juror** generates a sample question
  and tries to answer it using **only** the session text + the resolved prerequisite
  evidence; if it cannot, (d) fails.

Verdict + reason per outcome; failures route to repair.

### Stage 7 — Rules + Validate + Loop (`validate` + `repair`)
Deterministic rules (see §5) + juror verdicts → **pass / repair / escalate**. Repair
re-authors the failing outcome within its depth ceiling, or **drops & retargets** to an
uncovered explained concept. **Loop-until-dry** (K consecutive clean rounds) rather than a
hard retry cap; escalate to `NEEDS_REVIEW` only when a clean set is unreachable.

---

## 4. Answerability closure (the rigorous gate)

For an outcome `O` on concept `C` (answerability reaches **session + prior sessions via RAG**):

1. **Decompose** — list the atomic facts a correct answer to a question on `O` depends on
   (LLM → list of short claims), e.g. "knows what a virtual environment isolates",
   "knows the `pip install pkg==ver` form".
2. **Cover each fact** against, in order:
   - this session's text (evidence substring / semantic hit), then
   - a **prerequisite** concept's evidence — `taught_here` or `taught_earlier`
     (RAG `check_concept`/`search_reading_material` over the course scope; a hit in a
     *prior* unit counts, with provenance), then
   - `assumed_prior` foundational knowledge.
3. **Verdict** — if **every** required fact is covered by (session ∪ prereq closure) →
   answerable. Any uncovered fact → **fail (d)**, with the offending fact named.
4. **Adversarial confirm** — the skeptic juror must actually produce a correct answer from
   only the allowed text; inability overrides a naive "covered".

This is exactly "whether all the required details are covered in the session and its
prerequisites," made operational and graph-backed.

---

## 5. Validation rules (deterministic) — additions

Keep V1–V13; add:
- **V14 description grounded** — every concept/topic `description` claim traces to an
  evidence quote (substring check).
- **V15 explained-only** — no outcome targets a `NAMED_ONLY`/out-of-scope concept.
- **V16 depth ceiling** — outcome Bloom ≤ concept `taught_depth` ceiling.
- **V17 breadth** — every explained in-scope concept covered ≥ once (before depth-stacking).
- **V18 answerability** — every outcome passed the §4 closure (all required facts covered).
- (existing V13 becomes the juror should-be-asked / depth / breadth verdict aggregate.)

---

## 6. Juror prompt sketches (DB-backed via prompt_store)

- `lo.kb.describe_concept` — "From ONLY this topic text + evidence, write a 1–2 sentence
  description of what the session teaches about <concept>. Every claim must be supported
  by a verbatim quote you also return. If the text only NAMES it without explaining, say
  named_only."
- `lo.depth.classify` — "Given the concept's evidence, classify taught_depth
  (named_only/defined/explained/demonstrated/applied) and justify with a quote."
- `lo.review.breadth_depth` / `lo.review.should_ask` / `lo.review.answerability` — one
  per criterion; each juror returns `{verdict, reason, offending_fact?}`.
- `lo.review.skeptic` — "Generate one question for this outcome, then answer it using ONLY
  the provided session + prerequisite text. If you cannot answer correctly, the outcome is
  not answerable; return the missing fact."

All multi-sampled (K jurors), majority vote, conservative tie-break (fail).

---

## 7. Mapping to the current code (incremental)

| Change | File(s) |
|---|---|
| Rich `Concept`/`Topic` schema (description, evidence_quotes, explained, taught_depth) | `lo_schemas.py` |
| Topic descriptions + drop unexplained headings | `lo_nodes.parse_structure` + new `describe_topics` |
| Concept descriptions (deep-read per topic) | `lo_nodes.extract_concepts` |
| Evidence-based explained+depth gate (concepts **and** topics) | `lo_nodes.profile_coverage` (repurpose) |
| Evidence binding (substring check) | `lo_concept_graph` (helper) |
| Topic+concept graph + prereq provenance (cross-session RAG) | `lo_nodes.build_dependency_graph` |
| Depth-capped Understand/Apply allocation | `lo_nodes.plan_allocation` |
| 4-criterion multi-juror review gate + answerability closure | `lo_nodes.coverage_gate` / `_score_coverage` |
| V14–V18 rules; loop-until-dry | `lo_nodes.validate` + `lo_graph.route_after_validate` |
| Juror/builder prompts | `prompt_store` (seed + DB-editable) |

State (`lo_state.LOState`) gains nothing structurally new — the richer fields live inside
the existing `concept_inventory` / `sections` / `lo_reviews` channels (still REPLACE-channel;
keep the "return the complete inventory" contract).

---

## 8. Rollout phases (for when we implement)

- **P1 — Knowledge model:** rich described+explained+depth concepts & topics + evidence
  binding (Stages 1, schema, V14/V15). Verify LO quality lifts before the gate work.
- **P2 — Two-level graph + provenance** (Stage 2) incl. cross-session prereq resolution.
- **P3 — Depth-capped allocation** (Stage 4, V16/V17).
- **P4 — Multi-juror review gate + answerability** (Stage 6, §4, V18) + loop-until-dry.

Each phase is independently shippable and testable against a known session.

---

## 9. Risks / open questions

- **Run-to-run reproducibility:** richer modeling still rides on LLM extraction; mitigate
  with K-sampling + evidence binding, and consider **caching the knowledge model per
  session** (re-use unless source text changes — keyed by `source_fingerprint`).
- **Answerability false-negatives:** over-strict closure may reject good outcomes; tune the
  skeptic and treat `assumed_prior` foundational knowledge generously.
- **Cross-session RAG quality:** depends on prior sessions being ingested; degrade to
  "session + explicit prereq selection" when not ingested.
- **Topic graph value:** if topics are coarse, the topic-level graph may add little over
  the concept graph — validate on real courses in P2.
- **Loop-until-dry termination:** cap dry-rounds + total rounds to guarantee halting;
  escalate otherwise.
