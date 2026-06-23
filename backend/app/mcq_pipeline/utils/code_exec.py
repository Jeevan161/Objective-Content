"""
app/mcq_pipeline/code_exec.py
-----------------------------
Sandboxed local code execution for FIB_CODING verification.

FIB_CODING is graded by EXECUTION: the student fills the blank, the program is run
on a given stdin, and the actual stdout is compared to the expected output. We
verify generated FIBs the same way — fill the model's `blank_answer`, run, and
require the output to match — so a malformed FIB never ships.

Sandboxing here is "reasonable, not bulletproof": a non-shell subprocess in a fresh
temp dir, with CPU + file-size rlimits, its own process group, and a wall-clock
timeout. It does NOT block network or fully isolate the host — for untrusted code at
scale, run this behind a container / nsjail / seccomp. The code we execute is our
own grounded-LLM output, so the risk is moderate, but treat that as a TODO.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

from app.core.config import settings

# language token (any case) -> runner key
_LANG_ALIASES = {
    "PYTHON": "python", "PYTHON3": "python", "PYTHON39": "python", "PY": "python",
    "JS": "node", "JAVASCRIPT": "node", "NODE": "node",
    "JAVA": "java",
}
_BINARIES = {"python": "python3", "node": "node", "java": "java"}


def _runner(language: str) -> str | None:
    return _LANG_ALIASES.get((language or "").strip().upper())


def language_supported(language: str) -> bool:
    """True only if we both recognize the language AND its runtime is installed."""
    rk = _runner(language)
    if rk is None:
        return False
    if rk == "java":
        return bool(shutil.which("javac") and shutil.which("java"))
    return bool(shutil.which(_BINARIES[rk]))


def _limits():
    """preexec: own process group + CPU and file-size caps. (No RLIMIT_AS — it
    breaks the JVM/Node which reserve large virtual address space.)"""
    import resource
    try:
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU,
                           (settings.fib_exec_cpu_seconds, settings.fib_exec_cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
    except Exception:  # noqa: BLE001 — best-effort limits
        pass


def _exec(argv: list[str], cwd: str, stdin: str, timeout: int) -> dict:
    try:
        p = subprocess.run(
            argv, cwd=cwd, input=(stdin or ""), capture_output=True, text=True,
            timeout=timeout, preexec_fn=_limits,
            env={"PATH": os.environ.get("PATH", ""), "HOME": cwd, "TMPDIR": cwd},
        )
        return {"ran": True, "stdout": p.stdout, "stderr": p.stderr,
                "exit_code": p.returncode, "timed_out": False}
    except subprocess.TimeoutExpired as e:
        return {"ran": False, "stdout": e.stdout or "", "stderr": "execution timed out",
                "exit_code": None, "timed_out": True}
    except Exception as e:  # noqa: BLE001 — runner missing / spawn failure
        return {"ran": False, "stdout": "", "stderr": str(e), "exit_code": None, "timed_out": False}


def _java_class_name(code: str) -> str:
    m = re.search(r"public\s+class\s+([A-Za-z_]\w*)", code) or re.search(r"\bclass\s+([A-Za-z_]\w*)", code)
    return m.group(1) if m else "Main"


def run_code(language: str, code: str, stdin: str = "", timeout: int | None = None) -> dict:
    """Run `code` in `language` on `stdin`. Returns
    {ran, stdout, stderr, exit_code, timed_out, supported}."""
    rk = _runner(language)
    if rk is None or not language_supported(language):
        return {"ran": False, "supported": False, "stdout": "", "stderr": "unsupported language",
                "exit_code": None, "timed_out": False}
    timeout = timeout or settings.fib_exec_timeout
    with tempfile.TemporaryDirectory(prefix="fibexec_") as d:
        if rk == "python":
            path = os.path.join(d, "main.py")
            open(path, "w").write(code)
            res = _exec([_BINARIES["python"], path], d, stdin, timeout)
        elif rk == "node":
            path = os.path.join(d, "main.js")
            open(path, "w").write(code)
            res = _exec([_BINARIES["node"], path], d, stdin, timeout)
        else:  # java
            cls = _java_class_name(code)
            open(os.path.join(d, f"{cls}.java"), "w").write(code)
            comp = _exec(["javac", f"{cls}.java"], d, "", timeout)
            if not comp.get("ran") or comp.get("exit_code"):   # didn't run, or nonzero exit
                return {"ran": False, "supported": True, "stdout": "",
                        "stderr": "compile error: " + (comp.get("stderr") or ""),
                        "exit_code": comp.get("exit_code"), "timed_out": comp.get("timed_out", False)}
            res = _exec(["java", cls], d, stdin, timeout)
        res["supported"] = True
        return res


def _normalize(s: str) -> str:
    return "\n".join(line.rstrip() for line in (s or "").splitlines()).strip()


def verify_output(language: str, code: str, stdin: str, expected: str,
                  timeout: int | None = None) -> dict:
    """Run `code` on `stdin` and report whether stdout matches `expected`
    (trailing-whitespace-insensitive). Returns {supported, ran, matched, actual, ...}."""
    res = run_code(language, code, stdin, timeout)
    matched = bool(res.get("ran") and not res.get("timed_out")
                   and _normalize(res.get("stdout")) == _normalize(expected))
    return {
        "supported": res.get("supported", False),
        "ran": res.get("ran", False),
        "timed_out": res.get("timed_out", False),
        "matched": matched,
        "actual": res.get("stdout", ""),
        "stderr": res.get("stderr", ""),
    }
