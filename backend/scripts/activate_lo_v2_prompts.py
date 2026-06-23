"""
backend/scripts/activate_lo_v2_prompts.py
-----------------------------------------
Activate the LO-pipeline v2 prompt changes in the DB-backed prompt store.

Run with the backend venv + Postgres reachable:

    cd backend && PYTHONPATH=. .venv/bin/python scripts/activate_lo_v2_prompts.py

What it does (idempotent, safe to re-run):
  * SEEDS new prompt keys that have no DB row yet — `lo.rubric` (the unified R1-R8 Judge, renamed
    from the old `lo.coverage_rubric`) and `gen.scenario_rules` (scenario stem framing). These also
    seed automatically on app startup, so this is belt-and-suspenders.
  * RESETS the two CHANGED keys — `lo.author_sys` and `lo.repair_sys` — to their new code defaults.
    Their existing active rows hold the OLD text (with `<N_RU>` / `<BLOOM_LEVEL>` placeholders the
    v2 code no longer substitutes), so without this the live prompts would be broken. reset_prompt
    touches ONLY these two; any other prompts / human customizations are left untouched.

Note: the old `lo.coverage_rubric` row is now orphaned (the code reads `lo.rubric`). It is harmless;
deactivate or delete it from the admin UI if you want a clean list.
"""
from __future__ import annotations

# Importing the graph triggers every register() so all v2 code defaults are known.
import app.mcq_pipeline.graph  # noqa: F401
from app.mcq_pipeline.prompts import store as prompt_store

CHANGED_KEYS = ["lo.author_sys", "lo.repair_sys"]


def main() -> None:
    seeded = prompt_store.seed_prompts()
    print(f"seed_prompts: inserted {seeded} new prompt row(s) (new keys -> code default).")
    for key in CHANGED_KEYS:
        if not prompt_store.is_registered(key):
            print(f"  ! {key}: not registered — skipped")
            continue
        row = prompt_store.reset_prompt(key)
        print(f"  reset {key} -> active v{row.get('version')}")
    prompt_store.refresh()
    print("Done. lo.rubric / gen.scenario_rules are active (new keys); lo.author_sys / "
          "lo.repair_sys reset to v2 defaults.")


if __name__ == "__main__":
    main()
