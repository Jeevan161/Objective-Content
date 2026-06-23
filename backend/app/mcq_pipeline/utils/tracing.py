"""
app/mcq_pipeline/tracing.py
---------------------------
We run our OWN node-level tracing now (see `McqTrace` + `ProgressReporter` span emission),
so external LangSmith export is DISABLED. `disable_langsmith()` forces the LangChain/LangSmith
env flags off — overriding any `LANGCHAIN_TRACING_V2=true` left in `.env` — so langchain/langgraph
never ship runs out (and never hit the LangSmith rate limit). Call it at startup and at the top of
each pipeline run.
"""

from __future__ import annotations

import os


def disable_langsmith() -> None:
    """Force-disable any LangChain/LangSmith auto-tracing. Idempotent; safe to call often."""
    for var in ("LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_TRACING"):
        os.environ[var] = "false"
    # Drop the endpoint key so the SDK has nothing to export to even if a flag slips through.
    for var in ("LANGCHAIN_API_KEY", "LANGSMITH_API_KEY"):
        os.environ.pop(var, None)


# Disable on import as well, so simply importing the pipeline package turns external tracing off.
disable_langsmith()
