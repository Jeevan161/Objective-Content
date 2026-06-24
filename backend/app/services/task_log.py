"""
app/services/task_log.py
------------------------
Per-task backend logging: persist a `TaskLog` row (queryable from the admin
dashboard for crash diagnosis) AND mirror the line to stdout via the stdlib
logger (container logs). Each call opens its own short-lived session and commits
independently, so logging never participates in (or breaks) a caller's transaction.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger("app.task")

INFO, WARNING, ERROR = "INFO", "WARNING", "ERROR"


def log_task(
    *,
    task_type: str,
    event: str,
    message: str = "",
    level: str = INFO,
    job_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    detail: dict | None = None,
) -> None:
    """Record one task log line (DB + stdout). Best-effort: never raises."""
    # stdout first, so a log line survives even if the DB write fails.
    logger.log(
        getattr(logging, level, logging.INFO),
        "[%s] %s — %s%s",
        task_type, event, message,
        f" (job={job_id})" if job_id else "",
    )
    try:
        from app.db.session import SessionLocal
        from app.models import TaskLog
        with SessionLocal() as s:
            s.add(TaskLog(
                task_type=task_type, event=event[:64], message=message,
                level=level, job_id=job_id, run_id=run_id, user_id=user_id,
                detail=detail or {},
            ))
            s.commit()
    except Exception:  # noqa: BLE001 — logging must never break the caller
        logger.exception("failed to persist TaskLog row")
