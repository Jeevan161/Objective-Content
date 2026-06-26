"""
backend/scripts/reset_prompts.py
--------------------------------
Push updated CODE-DEFAULT prompt text to the ACTIVE DB row for the keys whose
defaults changed in a release. MCQ prompts are DB-backed: editing the Python
default is INERT until it's pushed to the active `mcq_prompts` row (seed_prompts
only inserts MISSING keys, it never updates an existing row). Brand-new keys
(e.g. review.feedback_intent_sys) seed automatically and are NOT listed here.

Run in the app container after deploying (so it hits RDS):

    docker compose -f docker-compose.prod.yml exec app python scripts/reset_prompts.py
    # or locally over the tunnel: .venv/bin/python scripts/reset_prompts.py

Idempotent. Only resets keys that are actually overridden vs the current default.
"""
from __future__ import annotations

import os
import sys

# Make `app` importable no matter the cwd/invocation (running `python scripts/x.py`
# puts scripts/ on sys.path, not the backend root). Add the backend root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.mcq_pipeline.graph  # noqa: E402,F401 — import triggers every register() so defaults exist
from app.mcq_pipeline.prompts.store import default_for, get_prompt, reset_prompt

# Keys whose code DEFAULT changed and must be pushed to the active DB row.
KEYS = [
    "gen.option_rules",        # distractor self-reveal + alignment/single-correct
    "gen.markdown_rules",      # markdown on options + no AI-looking dashes
    "review.distractor_audit", # negation/giveaway distractors -> HIGH answer-giveaway
]


def main() -> None:
    for key in KEYS:
        default = default_for(key)
        if default is None:
            print(f"SKIP  {key}: no registered default (unknown key)")
            continue
        if get_prompt(key) == default:
            print(f"OK    {key}: already at current default")
            continue
        reset_prompt(key)
        print(f"RESET {key}: active row updated to current default")


if __name__ == "__main__":
    main()
