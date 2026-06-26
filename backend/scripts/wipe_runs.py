"""
backend/scripts/wipe_runs.py
----------------------------
DESTRUCTIVE: delete ALL MCQ runs + their questions so you can start fresh.

Deletes:  mcq_runs (questions live in run.result JSONB), mcq_question_feedback,
          mcq_traces, sync_jobs WHERE job_type='MCQ', and (best-effort) the
          LangGraph checkpoint tables. Detaches beta_loads from the deleted runs.

KEEPS (resource + config data — never touched):
          courses, topics, units, unit_parts, rag_chunks, users, user_llm_keys,
          llm_providers, mcq_prompts, app_feedback, task_logs, and non-MCQ
          sync_jobs (SYNC / EXTRACT / RAG).

Usage (run in the app container so it hits RDS):
    # dry run — just COUNTS, deletes nothing:
    docker compose -f docker-compose.prod.yml exec app python scripts/wipe_runs.py
    # actually delete:
    docker compose -f docker-compose.prod.yml exec app python scripts/wipe_runs.py --yes
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, func, select, text, update  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.models import BetaLoad, McqQuestionFeedback, McqRun, McqTrace, SyncJob  # noqa: E402

_CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


def main() -> None:
    do_delete = "--yes" in sys.argv
    with SessionLocal() as s:
        runs = s.scalar(select(func.count()).select_from(McqRun))
        fb = s.scalar(select(func.count()).select_from(McqQuestionFeedback))
        traces = s.scalar(select(func.count()).select_from(McqTrace))
        mcq_jobs = s.scalar(select(func.count()).select_from(SyncJob).where(SyncJob.job_type == "MCQ"))
        other_jobs = s.scalar(select(func.count()).select_from(SyncJob).where(SyncJob.job_type != "MCQ"))
        print(f"TO DELETE: mcq_runs={runs}  question_feedback={fb}  traces={traces}  mcq_jobs={mcq_jobs}")
        print(f"KEPT     : non-MCQ jobs (sync/extract/rag)={other_jobs}  + all courses/units/rag/users/prompts")

        if not do_delete:
            print("\nDRY RUN — nothing deleted. Re-run with --yes to delete. Resource data is NOT touched.")
            return

        # FK-safe order. beta_loads.run_id is ON DELETE SET NULL, but detach explicitly first.
        # COMMIT the main deletes on their own — do NOT bundle the best-effort checkpoint
        # cleanup into this transaction, or a missing checkpoint table would roll it all back.
        s.execute(update(BetaLoad).values(run_id=None))
        s.execute(delete(McqQuestionFeedback))          # also cascades from mcq_runs; explicit is clearer
        s.execute(delete(McqTrace))
        s.execute(delete(McqRun))
        s.execute(delete(SyncJob).where(SyncJob.job_type == "MCQ"))
        s.commit()
        print("\nDELETED all runs/questions/feedback/traces/MCQ-jobs. Resource data kept.")

    # Best-effort: clear LangGraph checkpoints (orphaned after runs are gone). Each in its
    # OWN session so a missing table can't undo the deletes above.
    for tbl in _CHECKPOINT_TABLES:
        try:
            with SessionLocal() as s2:
                s2.execute(text(f"DELETE FROM {tbl}"))
                s2.commit()
        except Exception:  # noqa: BLE001 — table may not exist on this deployment
            pass
    print("Fresh start ready.")


if __name__ == "__main__":
    main()
