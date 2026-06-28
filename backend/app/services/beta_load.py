"""
app/services/beta_load.py
-------------------------
Portal export + load pipelines, runnable in a background thread (SyncJob types EXPORT/LOAD).

Extracted from the courses API so the endpoints can validate the run up front (fast 409/400),
spawn a background SyncJob, and return immediately — while these functions do the slow work
(build ZIP → upload to S3 → copy/fill the exam-config sheet → submit + poll the load task →
unlock) and stream short progress messages via a `sink`.

Each run writes a `BetaLoad` audit row that now also carries:
- `job_id`  — the SyncJob that performed it (so the Activity task ↔ Loads row link up),
- `content` — a snapshot of the exact `{**result, "questions": [...]}` payload loaded, so the
  loaded content stays viewable even if the run is later regenerated/edited.

The gate helpers (`require_reviewed`, `result_for_load`, `export_filename`) live here so the API
endpoints and the background runners share one source of truth.
"""

from __future__ import annotations

import re
import uuid
from typing import Callable

from fastapi import HTTPException

from app.db.session import SessionLocal
from app.models import BetaLoad, McqRun
from app.services.task_log import ERROR, log_task

Sink = Callable[[dict], None]


def export_filename(run) -> str:
    label = (run.result or {}).get("session_label") or "questions"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_") or "questions"
    return f"{safe}_MCQ_export.zip"


def require_reviewed(run) -> None:
    """Generate ZIP / Prepare & Load are FROZEN until the run is marked reviewed
    (which itself requires every question approved or excluded). 409 otherwise."""
    if (run.review_status or "") != "approved":
        raise HTTPException(
            status_code=409,
            detail="Mark the run reviewed before exporting or loading "
                   "(approve or exclude every question, then 'Mark run reviewed').")


def result_for_load(run, approved_only: bool) -> dict:
    """Gate loading on human approval and return the result payload to export.

    `approved_only=False` (the default "Load all") requires EVERY generated question to be
    approved. `approved_only=True` ("Load approved only") loads just the approved subset and
    only requires at least one. Raises 409 when the gate isn't met."""
    result = run.result or {}
    eligible = [q for q in (result.get("questions") or [])
                if q.get("status") == "generated" and not q.get("excluded")]
    approved = [q for q in eligible if q.get("approval") == "approved"]
    if approved_only:
        if not approved:
            raise HTTPException(status_code=409,
                detail="No questions are approved yet — approve at least one to load.")
        return {**result, "questions": approved}
    if not eligible or len(approved) != len(eligible):
        raise HTTPException(status_code=409,
            detail=f"Approve all {len(eligible)} questions before loading, "
                   f"or use 'Load approved only' ({len(approved)} approved).")
    return result


def run_export(job_id: uuid.UUID, run_id: uuid.UUID, approved_only: bool,
               user_id: uuid.UUID | None, sink: Sink) -> dict:
    """Build the portal-format ZIP and upload it to the beta S3 bucket; snapshot + audit."""
    from app.mcq_pipeline.portal_export import ExportValidationError, build_zip_bytes
    from app.services.beta_s3 import upload_bytes

    with SessionLocal() as session:
        run = session.get(McqRun, run_id)
        if run is None:
            raise RuntimeError("MCQ run not found.")
        require_reviewed(run)
        payload = result_for_load(run, approved_only)
        filename = export_filename(run)

        sink({"message": "Building export ZIP…"})
        try:
            data, info = build_zip_bytes(payload)
        except ExportValidationError as err:
            raise RuntimeError(f"Export validation failed: {'; '.join(err.errors)}") from err
        if info["total_questions"] == 0:
            raise RuntimeError("This run has no generated questions to export.")

        sink({"message": f"Uploading {filename}…"})
        try:
            url = upload_bytes(data, filename)
        except Exception as err:  # noqa: BLE001
            log_task(task_type="EXPORT", event="error", level=ERROR, run_id=run_id,
                     user_id=user_id, job_id=job_id, message=f"Beta S3 upload failed: {err}")
            raise RuntimeError(f"Beta S3 upload failed: {err}") from err

        session.add(BetaLoad(run_id=run_id, user_id=user_id, action="export", status="SUCCESS",
                             s3_url=url, message=filename, job_id=job_id, content=payload))
        session.commit()

    log_task(task_type="EXPORT", event="complete", run_id=run_id, user_id=user_id, job_id=job_id,
             message=f"{info['total_questions']} question(s) → {filename}")
    return {"status": "SUCCESS", "message": f"Exported {info['total_questions']} question(s).",
            "url": url, "filename": filename, "total": info["total_questions"],
            "counts": info["counts"], "batch_id": info["batch_id"]}


def run_load(job_id: uuid.UUID, run_id: uuid.UUID, body: dict,
             user_id: uuid.UUID | None, sink: Sink) -> dict:
    """Full beta-load pipeline: build+upload ZIP, copy/fill the exam-config sheet, submit the
    load task, poll it, and unlock on success. Snapshots the loaded payload + audits."""
    from app.mcq_pipeline.portal_export import ExportValidationError, build_zip_bytes
    from app.services import beta_s3, beta_sheet

    with SessionLocal() as session:
        run = session.get(McqRun, run_id)
        if run is None:
            raise RuntimeError("MCQ run not found.")
        require_reviewed(run)
        parent_topic_id = (body.get("topic_id") or "").strip() or run.topic_id
        if not parent_topic_id:
            raise RuntimeError("No topic_id for the exam's parent resource.")
        payload = result_for_load(run, bool(body.get("approved_only")))
        filename = export_filename(run)

    # One id per unit, shared by the exam (Form!B5) AND the questions JSON filename.
    resource_id = str(uuid.uuid4())

    sink({"message": "Building questions ZIP…"})
    try:
        data, info = build_zip_bytes(payload, batch_id=resource_id)
    except ExportValidationError as err:
        raise RuntimeError(f"Export validation failed: {'; '.join(err.errors)}") from err
    if info["total_questions"] == 0:
        raise RuntimeError("This run has no generated questions to load.")

    sink({"message": f"Uploading {filename}…"})
    try:
        s3_url = beta_s3.upload_bytes(data, filename)
    except Exception as err:  # noqa: BLE001
        log_task(task_type="LOAD", event="error", level=ERROR, run_id=run_id, user_id=user_id,
                 job_id=job_id, message=f"Beta S3 upload failed: {err}")
        raise RuntimeError(f"Beta S3 upload failed: {err}") from err

    sink({"message": "Preparing exam-config sheet…"})
    try:
        sheet = beta_sheet.prepare_sheet(
            resource_id=resource_id,
            topic_id=parent_topic_id,
            num_questions=info["total_questions"],
            child_order=body.get("child_order", ""),
            duration_min=body.get("duration_min", 0),
            pass_percentage=(body.get("pass_percentage", 0) or 0) / 100.0,
            show_answer_scoring_mode=body.get("show_answer_scoring_mode", ""),
            should_send_solutions=body.get("should_send_solutions", False),
            share_emails=[e for e in [body.get("loader_email", ""),
                                      (body.get("reviewer_email") or "").strip()] if e],
        )
    except Exception as err:  # noqa: BLE001
        log_task(task_type="LOAD", event="error", level=ERROR, run_id=run_id, user_id=user_id,
                 job_id=job_id, message=f"Sheet preparation failed: {err}")
        raise RuntimeError(f"Sheet preparation failed: {err}") from err

    sink({"message": "Submitting load task…"})
    try:
        request_id = beta_s3.submit_sheet_loading(
            spreadsheet_id=sheet["spreadsheet_id"],
            spread_sheet_name=sheet["title"], s3_url=s3_url)
    except Exception as err:  # noqa: BLE001
        log_task(task_type="LOAD", event="error", level=ERROR, run_id=run_id, user_id=user_id,
                 job_id=job_id, message=f"Sheet-loading submit failed: {err}",
                 detail={"sheet_url": sheet["url"]})
        raise RuntimeError(f"Sheet-loading submit failed: {err}") from err

    sink({"message": "Loading into portal…"})
    status, message = beta_s3.poll_task(request_id)
    unlock_id = None
    if status == "SUCCESS":
        sink({"message": "Unlocking resource…"})
        try:
            unlock_id = beta_s3.submit_unlock(sheet["resource_id"])
        except Exception as err:  # noqa: BLE001
            message = f"Loaded, but unlock failed: {err}"

    with SessionLocal() as session:
        session.add(BetaLoad(run_id=run_id, user_id=user_id, action="load", status=status,
                             resource_id=sheet["resource_id"], sheet_url=sheet["url"],
                             s3_url=s3_url, request_id=request_id, unlock_id=unlock_id or "",
                             message=message, job_id=job_id, content=payload))
        session.commit()
    log_task(task_type="LOAD", event="complete", run_id=run_id, user_id=user_id, job_id=job_id,
             level=(ERROR if status == "FAILURE" else "INFO"),
             message=f"status={status} resource={sheet['resource_id']} {message}".strip())

    return {
        "status": status, "message": message or f"Load {status.lower()}.",
        "sheet_url": sheet["url"], "spreadsheet_id": sheet["spreadsheet_id"],
        "resource_id": sheet["resource_id"], "s3_url": s3_url,
        "request_id": request_id, "unlock_id": unlock_id,
        "total": info["total_questions"], "filename": filename,
    }
