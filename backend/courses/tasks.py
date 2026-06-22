"""Lightweight background task runner (thread-based).

Avoids an external broker (Celery/Redis) for this app. Each sync runs in a
daemon thread; progress and result are persisted on the SyncJob row, which the
frontend polls. Suitable for a single-process dev/runserver setup.
"""
import threading

from django.db import connection

from .extraction import run_extraction_job
from .services import run_sync_job


def _run(target, *args):
    try:
        target(*args)
    finally:
        # Each thread gets its own DB connection; close it so it isn't leaked.
        connection.close()


def start_sync_job(job_id):
    thread = threading.Thread(target=_run, args=(run_sync_job, job_id), daemon=True)
    thread.start()
    return thread


def start_extraction_job(job_id, tokens):
    # tokens ({ENV: bearer}) are passed in memory only — never persisted.
    thread = threading.Thread(
        target=_run, args=(run_extraction_job, job_id, tokens), daemon=True
    )
    thread.start()
    return thread
