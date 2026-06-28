"""
app/mcq_pipeline/portal_export.py
---------------------------------
Convert a finished MCQ run (the pipeline's per-question `lean` output) into the
portal IMPORT format: four type-grouped folders, each a single JSON array, with
freshly generated UUIDs for every `question_id` / `option_id` / `t_id`.

Folder layout (mirrors All_MCQ_TYPES_ZIP):
    Default_new/<batch>.json          MULTIPLE_CHOICE, MORE_THAN_ONE_MULTIPLE_CHOICE,
                                       TRUE_OR_FALSE (as a True/False MCQ), TEXTUAL
    Code Analysis MCQs/<batch>.json   CODE_ANALYSIS_{MULTIPLE_CHOICE,TEXTUAL,MORE_THAN_ONE_*}
    FIB_CODING/<batch>.json           FIB_CODING
    REARRANGE/<batch>.json            REARRANGE

`build_zip_bytes(result)` returns (zip_bytes, counts) so the same logic serves a
download endpoint and a CLI.
"""

from __future__ import annotations

import io
import json
import re
import uuid
import zipfile

GROUP_DEFAULT = "Default_new"
GROUP_CODE = "Code Analysis MCQs"
GROUP_FIB = "FIB_CODING"
GROUP_REARRANGE = "REARRANGE"
GROUPS = [GROUP_DEFAULT, GROUP_CODE, GROUP_FIB, GROUP_REARRANGE]

_BLANK_SENTINEL = "{{BLANK}}"


def _uuid() -> str:
    return str(uuid.uuid4())


def _lang(code_language: str, *, fib: bool = False) -> str:
    c = (code_language or "PYTHON").strip().upper().replace(" ", "")
    if c in ("PY", "PYTHON", "PYTHON3", "PYTHON39", ""):
        return "PYTHON39" if fib else "PYTHON"
    return c


def _key(lo: dict, q: dict, n: int) -> str:
    base = lo.get("outcome") or q.get("outcome") or "question"
    return f"{base}__{n}"


def _slug(value) -> str:
    """snake_case a free-text label for a tag: lowercase, non-alphanumerics → single '_'."""
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _tags(lo: dict, q: dict, n: int) -> list[str]:
    """Per-question tag_names — one prefixed, snake_cased tag per LO facet:
    t_ topic · c_ concept · sc_ sub_concept · lo_ learning_outcome ·
    bl_ blooms_level · bc_ blooms_cat · la_ learner_action. Empty facets are skipped."""
    facets = {
        "t": lo.get("topic"),
        "c": lo.get("concept"),
        "sc": lo.get("sub_concept"),
        "lo": lo.get("outcome") or q.get("outcome"),
        "bl": lo.get("bloom_level_raw") or lo.get("bloom_level"),
        "bc": lo.get("bloom_category"),
        "la": lo.get("learner_action"),
    }
    tags = [f"{prefix}_{slug}" for prefix, val in facets.items() if (slug := _slug(val))]
    return tags or [f"question_{n}"]


def _difficulty(q: dict, lo: dict) -> str:
    return (q.get("difficulty") or lo.get("bloom_category") or "EASY").upper() \
        if (q.get("difficulty") or "").upper() in ("EASY", "MEDIUM", "HARD") else \
        (q.get("difficulty") or "EASY").upper()


# --- per-type builders ----------------------------------------------------- #
def _option_ctype(o: dict) -> str:
    """Render type for an option: the generator now tags each option TEXT/MARKDOWN.
    Normalize (older runs predate the field → default TEXT)."""
    ct = str(o.get("content_type") or "TEXT").strip().upper()
    return "MARKDOWN" if ct == "MARKDOWN" else "TEXT"


def _mcq(q, lean, key, tags, diff, *, qtype):
    return GROUP_DEFAULT, {
        "question_id": _uuid(),
        "question_key": key,
        "skills": [],
        "toughness": diff,
        "question_type": qtype,
        "question": {
            "content": lean.get("question", ""),
            "content_type": "MARKDOWN",
            "tag_names": tags,
            "multimedia": [],
        },
        "options": [
            {"content": o.get("content", ""), "content_type": _option_ctype(o),
             "is_correct": bool(o.get("is_correct")), "multimedia": []}
            for o in lean.get("options", [])
        ],
    }


def _true_false(q, lean, key, tags, diff):
    # Represented as a True/False MULTIPLE_CHOICE (matches the reference format).
    is_true = bool(lean.get("is_true"))
    stem = lean.get("statement", "")
    code = lean.get("code") or ""
    content = f"{stem}\n\n```\n{code}\n```" if code.strip() else stem
    return GROUP_DEFAULT, {
        "question_id": _uuid(),
        "question_key": key,
        "skills": [],
        "toughness": diff,
        "question_type": "MULTIPLE_CHOICE",
        "question": {"content": content, "content_type": "MARKDOWN",
                     "tag_names": tags, "multimedia": []},
        "options": [
            {"content": "True", "content_type": "TEXT", "is_correct": is_true, "multimedia": []},
            {"content": "False", "content_type": "TEXT", "is_correct": not is_true, "multimedia": []},
        ],
    }


def _textual(q, lean, key, tags, diff):
    return GROUP_DEFAULT, {
        "question_type": "TEXTUAL",
        "question_key": key,
        "question_id": _uuid(),
        "question": {"content": lean.get("question", ""), "content_type": "MARKDOWN",
                     "difficulty": diff, "metadata": "metadata", "multimedia": []},
        "answer": {"answer_type": "EXACT", "content": lean.get("answer", ""),
                   "evaluation_type": "CASE_SENSITIVE", "language": "ENGLISH"},
        "explanation_for_answer": {"content": lean.get("explanation", ""), "content_type": "MARKDOWN"},
    }


def _code_mcq(q, lean, key, tags, diff, *, qtype, more_than_one):
    outputs = lean.get("correct_outputs") if more_than_one else [lean.get("correct_output", "")]
    return GROUP_CODE, {
        "question_key": key,
        "skills": [],
        "toughness": diff,
        "question_type": qtype,
        "question_text": lean.get("question", ""),
        "multimedia": [],
        "content_type": "HTML",
        "tag_names": tags,
        "input_output": [{
            "input": "",
            "question_id": _uuid(),
            "wrong_answers": list(lean.get("wrong_answers", [])),
            "output": list(outputs or []),
        }],
        "code_metadata": [{
            "is_editable": False,
            "language": _lang(lean.get("code_language")),
            "code_data": lean.get("code", ""),
            "default_code": True,
        }],
    }


def _code_textual(q, lean, key, tags, diff):
    return GROUP_CODE, {
        "question_key": key,
        "question_text": lean.get("question", ""),
        "multimedia": [],
        "skills": [],
        "toughness": diff,
        "reference": "",
        "question_type": "CODE_ANALYSIS_TEXTUAL",
        "tag_names": tags,
        "content_type": "HTML",
        "input_output": [{
            "question_id": _uuid(),
            "input": "",
            "output": [lean.get("expected_output", "")],
        }],
        "code_metadata": [{
            "is_editable": False,
            "language": _lang(lean.get("code_language")),
            "code_data": lean.get("code", ""),
            "default_code": True,
        }],
    }


def _fib_blocks(code_lines, blank_answer, *, masked):
    blocks = []
    for i, line in enumerate(code_lines):
        if _BLANK_SENTINEL in line:
            repl = "<InlineBlank>&&&</InlineBlank>" if masked \
                else f"<InlineBlank>{blank_answer}</InlineBlank>"
            blocks.append({"code": line.replace(_BLANK_SENTINEL, repl),
                           "block_type": "INLINE_BLANK",
                           "start_line_number": i + 1, "order": i + 1})
        else:
            blocks.append({"code": line, "block_type": "DEFAULT",
                           "start_line_number": i + 1, "order": i + 1})
    return blocks


def _fib(q, lean, key, tags, diff):
    code_lines = lean.get("code_lines", []) or []
    blank = lean.get("blank_answer", "")
    lang = _lang(lean.get("code_language"), fib=True)
    return GROUP_FIB, {
        "question_key": key,
        "question_text": lean.get("question", ""),
        "question_type": "FIB_CODING",
        "short_text": "",
        "content_type": "HTML",
        "question_id": _uuid(),
        "skills": [],
        "difficulty": diff,
        "tag_names": tags,
        "test_case_evaluation_metrics": [
            {"language": lang, "time_limit_to_execute_in_seconds": 20.0}],
        "cpp_python_time_factor": 0,
        "order_no": 100,
        "question_asked_by_companies_info": [],
        "remarks": "",
        "scores_updated": False,
        "scores_computed": 0,
        "input_output": [{
            "input": [{
                "t_id": _uuid(),
                "input": lean.get("test_input", ""),
                "output": lean.get("test_output", ""),
                "is_hidden": True,
                "score": 1,
            }],
            "average_time_spent": 0.0,
            "order_no": 100,
        }],
        "solution": {"code_blocks": _fib_blocks(code_lines, blank, masked=False)},
        "fib_coding": {
            "language": lang,
            "is_debug_mode_enabled": True,
            "is_run_code_enabled": True,
            "code_blocks": _fib_blocks(code_lines, blank, masked=True),
        },
    }


def _rearrange(q, lean, key, tags, diff):
    items = lean.get("ordered_items", []) or []
    n = len(items)
    options = []
    for i, content in enumerate(items):
        options.append({
            "option_id": _uuid(),
            "content": content,
            "content_type": "TEXT",
            "display_order": n - i,      # shown reversed so the order isn't given away
            "correct_order": i + 1,      # the true sequence (first → last)
        })
    return GROUP_REARRANGE, {
        "question_id": _uuid(),
        "question_key": key,
        "skills": [],
        "toughness": diff,
        "question_type": "REARRANGE",
        "explanation_for_answer": {"content": lean.get("explanation", ""), "content_type": "MARKDOWN"},
        "question": {"content": lean.get("question", ""), "content_type": "MARKDOWN",
                     "tag_names": tags, "multimedia": []},
        "options": options,
    }


def _convert(q: dict, lo: dict, n: int):
    qt = q.get("question_type")
    lean = q.get("lean") or {}
    key, tags, diff = _key(lo, q, n), _tags(lo, q, n), _difficulty(q, lo)
    if qt in ("MULTIPLE_CHOICE", "MORE_THAN_ONE_MULTIPLE_CHOICE"):
        return _mcq(q, lean, key, tags, diff, qtype=qt)
    if qt == "TRUE_OR_FALSE":
        return _true_false(q, lean, key, tags, diff)
    if qt == "TEXTUAL":
        return _textual(q, lean, key, tags, diff)
    if qt in ("CODE_ANALYSIS_MULTIPLE_CHOICE", "CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE"):
        return _code_mcq(q, lean, key, tags, diff, qtype=qt,
                         more_than_one=qt.endswith("MORE_THAN_ONE_MULTIPLE_CHOICE"))
    if qt == "CODE_ANALYSIS_TEXTUAL":
        return _code_textual(q, lean, key, tags, diff)
    if qt == "FIB_CODING":
        return _fib(q, lean, key, tags, diff)
    if qt == "REARRANGE":
        return _rearrange(q, lean, key, tags, diff)
    return None, None


def build_groups(result: dict) -> dict[str, list]:
    """Group a run's GENERATED questions into the four portal folders."""
    groups: dict[str, list] = {g: [] for g in GROUPS}
    los = {lo.get("outcome"): lo for lo in (result.get("final_los") or [])}
    n = 0
    for q in (result.get("questions") or []):
        if q.get("status") != "generated" or not q.get("lean") or q.get("excluded"):
            continue
        n += 1
        group, obj = _convert(q, los.get(q.get("outcome"), {}), n)
        if obj is not None:
            groups[group].append(obj)
    return groups


class ExportValidationError(ValueError):
    """Raised when the export fails the rename_zip-style validation (a question is
    missing its id). Carries the per-item error list."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _question_id(folder: str, item: dict):
    """The id field a portal item is keyed by — nested for Code Analysis, top-level
    for every other group."""
    if folder == GROUP_CODE:
        return ((item.get("input_output") or [{}])[0] or {}).get("question_id")
    return item.get("question_id")


def validate_groups(groups: dict[str, list]) -> tuple[int, list[str]]:
    """rename_zip-style check: every item must carry a non-empty question_id.
    Returns (total_questions, errors)."""
    errors: list[str] = []
    total = 0
    for folder in GROUPS:
        for item in groups[folder]:
            total += 1
            if not _question_id(folder, item):
                errors.append(f"missing question_id in {folder}: "
                              f"{item.get('question_key') or item.get('question_type') or '?'}")
    return total, errors


def build_zip_bytes(result: dict, *, batch_id: str | None = None) -> tuple[bytes, dict]:
    """Build the portal ZIP. Mirrors rename_zip: all four folders use ONE shared
    `<uuid>.json` filename, every question_id is validated, and NO ZIP is produced
    if validation fails (raises ExportValidationError).

    `batch_id` names that shared JSON file. For a sheet-backed load it MUST be the
    exam's resource id (Form!B5) so the loader can match the questions file to the
    exam unit; for a standalone ZIP export it's left unset and a fresh UUID is used.

    Returns (zip_bytes, info) where info = {counts, total_questions, batch_id}.
    """
    groups = build_groups(result)
    total, errors = validate_groups(groups)
    if errors:
        raise ExportValidationError(errors)

    batch = batch_id or _uuid()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for folder in GROUPS:
            z.writestr(f"{folder}/{batch}.json",
                       json.dumps(groups[folder], indent=4, ensure_ascii=False))
    info = {"counts": {g: len(groups[g]) for g in GROUPS},
            "total_questions": total, "batch_id": batch}
    return buf.getvalue(), info
