"""
app/api/analytics.py
--------------------
Throughput analytics for the admin/manager/lead dashboard.

A single rich endpoint (`GET /api/admin/analytics`) returns every figure the dashboard
and the downloadable HTML report need, for a date range with optional course/user filters:
headline KPIs + percentages, a day/week/month time-series, course-wise and user-wise
breakdowns, and the session-level regeneration view (full-session regenerations carry the
reviewer's mandatory reason).

Metric conventions (see the approved plan):
- "generated" counts DISTINCT questions — the latest-version run per (course_id, unit_id)
  session within the range — so regenerated batches are not double-counted.
- "reviewed"/"approved"/"rejected" are read off each kept run's `result['questions'][]`
  (`approval` in {approved, rejected}, minus excluded for approved).
- "regen_requests" counts per-question `reject_regenerate` reviewer actions.
- "session regenerations" are runs with `version > 1`; their reason lives in
  `result['regen_reason']`.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_elevated
from app.db.session import get_session
from app.models import Course, McqQuestionFeedback, McqRun, User

router = APIRouter(prefix="/api", tags=["analytics"])


def _parse_dt(value: str | None, *, fallback: datetime) -> datetime:
    """Parse an ISO date/datetime; treat naive values as UTC. Falls back on blank/invalid."""
    if not value:
        return fallback
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return fallback
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _bucket_key(dt: datetime, bucket: str) -> str:
    """Truncate a timestamp to the start of its day / ISO-week / month, as an ISO date string."""
    d = dt.astimezone(timezone.utc).date()
    if bucket == "month":
        return d.replace(day=1).isoformat()
    if bucket == "week":
        return (d - timedelta(days=d.weekday())).isoformat()  # Monday of the week
    return d.isoformat()  # day


def _pct(part: float, whole: float) -> float:
    return round((part / whole) * 100, 1) if whole else 0.0


def _question_stats(run: McqRun) -> tuple[int, int, int, int]:
    """(generated, reviewed, approved, rejected) for one run from its JSONB questions."""
    questions = (run.result or {}).get("questions") or []
    generated = run.question_count or len(questions)
    reviewed = approved = rejected = 0
    for q in questions:
        approval = (q or {}).get("approval")
        if approval == "approved":
            reviewed += 1
            if not (q or {}).get("excluded"):
                approved += 1
        elif approval == "rejected":
            reviewed += 1
            rejected += 1
    return generated, reviewed, approved, rejected


@router.get("/admin/analytics")
def analytics(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None, alias="to"),
    bucket: str = Query("day"),
    course_id: str | None = Query(None),
    user_id: str | None = Query(None),
    _: User = Depends(require_elevated),
    session: Session = Depends(get_session),
) -> dict:
    """Throughput analytics for a date range, filterable by course and user."""
    now = datetime.now(timezone.utc)
    start = _parse_dt(from_, fallback=now - timedelta(days=30))
    end = _parse_dt(to, fallback=now)
    if bucket not in ("day", "week", "month"):
        bucket = "day"
    user_uuid: uuid.UUID | None = None
    if user_id:
        try:
            user_uuid = uuid.UUID(str(user_id))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="user_id must be a valid UUID.")

    # --- load runs in range (+ optional filters) ---------------------------- #
    run_stmt = select(McqRun).where(McqRun.created_at >= start, McqRun.created_at <= end)
    if course_id:
        run_stmt = run_stmt.where(McqRun.course_id == course_id)
    if user_uuid is not None:
        run_stmt = run_stmt.where(McqRun.created_by == user_uuid)
    runs = list(session.scalars(run_stmt).all())

    # Distinct-latest set: keep the highest-version run per (course_id, unit_id) session.
    latest_by_session: dict[tuple[str, str], McqRun] = {}
    for r in runs:
        key = (r.course_id, r.unit_id)
        cur = latest_by_session.get(key)
        if cur is None or (r.version or 0) >= (cur.version or 0):
            latest_by_session[key] = r
    kept = list(latest_by_session.values())

    # --- name lookups ------------------------------------------------------- #
    course_names = dict(session.execute(select(Course.course_id, Course.course_name)).all())
    user_rows = session.scalars(select(User)).all()
    user_names = {u.id: (u.name or u.email or str(u.id)) for u in user_rows}
    user_emails = {u.id: (u.email or "") for u in user_rows}

    # --- aggregate over kept runs ------------------------------------------- #
    tot_gen = tot_rev = tot_app = tot_rej = 0
    ts: dict[str, dict[str, int]] = defaultdict(lambda: {"generated": 0, "reviewed": 0, "approved": 0})
    by_course: dict[str, dict[str, int]] = defaultdict(
        lambda: {"generated": 0, "reviewed": 0, "approved": 0, "regen_requests": 0})
    by_user: dict[uuid.UUID | None, dict[str, int]] = defaultdict(
        lambda: {"generated": 0, "reviewed": 0, "approved": 0, "regen_requests": 0})

    for r in kept:
        gen, rev, app_, rej = _question_stats(r)
        tot_gen += gen
        tot_rev += rev
        tot_app += app_
        tot_rej += rej
        b = _bucket_key(r.created_at, bucket)
        ts[b]["generated"] += gen
        ts[b]["reviewed"] += rev
        ts[b]["approved"] += app_
        by_course[r.course_id]["generated"] += gen
        by_course[r.course_id]["reviewed"] += rev
        by_course[r.course_id]["approved"] += app_
        by_user[r.created_by]["generated"] += gen
        by_user[r.created_by]["reviewed"] += rev
        by_user[r.created_by]["approved"] += app_

    # --- per-question regeneration requests (reject_regenerate) in range ---- #
    fb_stmt = (select(McqQuestionFeedback)
               .where(McqQuestionFeedback.action == "reject_regenerate",
                      McqQuestionFeedback.created_at >= start,
                      McqQuestionFeedback.created_at <= end))
    feedback = list(session.scalars(fb_stmt).all())
    # Resolve each feedback's run for course/user attribution + filter honouring.
    fb_run_ids = {f.run_id for f in feedback if f.run_id}
    fb_runs: dict[uuid.UUID, McqRun] = {}
    if fb_run_ids:
        fb_runs = {r.id: r for r in session.scalars(
            select(McqRun).where(McqRun.id.in_(fb_run_ids))).all()}
    regen_requests = 0
    regen_by_user_q: dict[uuid.UUID | None, int] = defaultdict(int)
    for f in feedback:
        run = fb_runs.get(f.run_id) if f.run_id else None
        if course_id and (run is None or run.course_id != course_id):
            continue
        if user_uuid is not None and (run is None or run.created_by != user_uuid):
            continue
        regen_requests += 1
        cid = run.course_id if run else ""
        if cid in by_course:
            by_course[cid]["regen_requests"] += 1
        uid = run.created_by if run else None
        by_user[uid]["regen_requests"] += 1
        regen_by_user_q[uid] += 1

    # --- session regenerations (version > 1 runs) with their reason --------- #
    regen_by_session = []
    regen_by_user_s: dict[uuid.UUID | None, int] = defaultdict(int)
    for r in runs:
        if (r.version or 1) <= 1:
            continue
        regen_by_user_s[r.created_by] += 1
        regen_by_session.append({
            "run_id": str(r.id),
            "course_id": r.course_id,
            "course_name": course_names.get(r.course_id, r.course_id),
            "unit_id": r.unit_id,
            "version": r.version,
            "created_by_name": user_names.get(r.created_by, "—") if r.created_by else "—",
            "reason": (r.result or {}).get("regen_reason", ""),
            "question_count": r.question_count or 0,
            "created_at": r.created_at,
        })
    regen_by_session.sort(key=lambda x: x["created_at"], reverse=True)

    # --- shape outputs ------------------------------------------------------ #
    timeseries = [{"bucket": k, **v} for k, v in sorted(ts.items())]

    by_course_rows = sorted(
        ({"course_id": cid, "course_name": course_names.get(cid, cid), **vals}
         for cid, vals in by_course.items()),
        key=lambda x: x["generated"], reverse=True)

    by_user_rows = sorted(
        ({"user_id": str(uid) if uid else None,
          "name": user_names.get(uid, "—") if uid else "—",
          "email": user_emails.get(uid, "") if uid else "",
          **vals}
         for uid, vals in by_user.items()),
        key=lambda x: x["generated"], reverse=True)

    all_uids = set(regen_by_user_q) | set(regen_by_user_s)
    regen_by_user = sorted(
        ({"user_id": str(uid) if uid else None,
          "name": user_names.get(uid, "—") if uid else "—",
          "question_regens": regen_by_user_q.get(uid, 0),
          "session_regens": regen_by_user_s.get(uid, 0)}
         for uid in all_uids),
        key=lambda x: (x["session_regens"] + x["question_regens"]), reverse=True)

    return {
        "range": {"from": start.isoformat(), "to": end.isoformat(), "bucket": bucket},
        "filters": {"course_id": course_id or None, "user_id": user_id or None},
        "kpis": {
            "generated": tot_gen,
            "reviewed": tot_rev,
            "approved": tot_app,
            "rejected": tot_rej,
            "regen_requests": regen_requests,
            "session_regens": len(regen_by_session),
            "generation_events": len(runs),
            "sessions": len(kept),
        },
        "percentages": {
            "review_rate": _pct(tot_rev, tot_gen),
            "approval_rate": _pct(tot_app, tot_rev),
            "approved_of_generated": _pct(tot_app, tot_gen),
            "regen_rate": _pct(regen_requests, tot_gen),
        },
        "timeseries": timeseries,
        "by_course": by_course_rows,
        "by_user": by_user_rows,
        "regen_by_session": regen_by_session,
        "regen_by_user": regen_by_user,
    }
