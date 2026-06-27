"""Question pipeline · Node 8 — generate_questions · execution verification.

FIB questions are graded by RUNNING them (fill the blank, run on the test input, compare
stdout); CODE_ANALYSIS_TEXTUAL keys are corrected to the program's real stdout. Both use
the sandboxed `code_exec`. `fix_lean` (node.py) is imported lazily to avoid a cycle.
"""
from __future__ import annotations

import re

from app.mcq_pipeline.nodes.m08_generate_questions.grounding import _ground, _fallback_to_mcq


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
    from app.mcq_pipeline.nodes.m08_generate_questions.node import fix_lean
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
