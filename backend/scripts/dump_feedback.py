"""
backend/scripts/dump_feedback.py
--------------------------------
READ-ONLY export of the reviewer/product feedback stored in RDS, so it can be
analysed to drive prompt/flow improvements. Writes JSON to stdout — no writes,
no deletes.

Run on the EC2 box (DATABASE_URL already points at RDS) or locally with the
SSH tunnel open, from backend/ with the venv:

    .venv/bin/python scripts/dump_feedback.py > feedback_dump.json

Then share feedback_dump.json (it contains reviewer comments + tags + counts;
review the content before sharing if any comments may include sensitive text).
"""
from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import AppFeedback, McqQuestionFeedback


def main() -> None:
    with SessionLocal() as s:
        qfb = s.scalars(select(McqQuestionFeedback)
                        .order_by(McqQuestionFeedback.created_at.desc())).all()
        app_fb = s.scalars(select(AppFeedback)
                           .order_by(AppFeedback.created_at.desc())).all()

        by_action, by_type, by_tag = Counter(), Counter(), Counter()
        rejects = []  # the actual complaints — reject/regenerate rows with a comment
        for r in qfb:
            by_action[r.action] += 1
            if r.question_type:
                by_type[r.question_type] += 1
            for t in (r.tags or []):
                by_tag[t] += 1
            if r.action in ("reject_regenerate", "reject") and (r.comment or "").strip():
                rejects.append({
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "question_type": r.question_type,
                    "tags": r.tags or [],
                    "comment": r.comment,
                    "outcome": r.outcome,
                })

        app_rows = [{
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "rating": getattr(a, "rating", None),
            "category": getattr(a, "category", None),
            "message": getattr(a, "message", None),
        } for a in app_fb]

    out = {
        "question_feedback_total": len(qfb),
        "counts": {
            "by_action": dict(by_action),
            "by_question_type": dict(by_type),
            "by_tag": dict(by_tag.most_common()),
        },
        "reject_comments": rejects,          # <-- the reported issues to analyse
        "app_feedback": app_rows,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
