"""
app/services/beta_sheet.py
--------------------------
Prepare the exam-config Google Sheet for an MCQ run by COPYING a formula-driven
template and filling only its ``Form`` tab. The template's other tabs
(ResourcesData / Units / Exam) read every value from ``Form`` via formulas, so the
Form tab is the single source of truth — we write a handful of column-B input cells
and the rest cascades automatically.

This replaces the config app's row-by-row sheet building (mcq_json_preparation +
tt_gsheet) with a single template copy. Auth uses a service account
(``settings.google_sa_credentials_file``); the template
(``settings.mcq_template_spreadsheet_id``) must be shared with that account as Editor.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.core.config import settings

# backend root: app/services/beta_sheet.py -> parents[2]. Relative credential paths
# are resolved against this so the lookup doesn't depend on the server's CWD.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

FORM_TAB = "Form"

# Form input cells (column B). Everything else in the workbook is defaulted in the
# template or computed by formulas, so these are the only cells we write.
CELL_RESOURCE_ID = "B5"               # exam / unit / resource id  (fresh per export)
CELL_COMMON_UNIT_ID = "B9"            # shared content id          (fresh per export)
CELL_PARENT_RESOURCE_ID = "B14"       # the session's topic id (parent of this unit)
CELL_CHILD_ORDER = "B15"             # position among the topic's children
CELL_DURATION_MIN = "B22"            # minutes; feeds duration_in_sec = B22*60
CELL_NUM_QUESTIONS = "B30"           # drives total score + instruction text
CELL_PASS_PCT = "B40"                # fraction (0.8 = 80%); feeds min_score_to_pass
CELL_SHOW_ANSWER_SCORING_MODE = "B51"
CELL_SHOULD_SEND_SOLUTIONS = "B56"

# The sheets the downstream content-loading task reads (in template/portal order).
DATA_SETS_TO_LOAD = ["ResourcesData", "Units", "Exam"]


def _credentials_path() -> Path:
    """Absolute path to the service-account JSON, resolving relative paths against
    the backend root (so it works regardless of the server's working directory)."""
    p = Path(settings.google_sa_credentials_file).expanduser()
    if not p.is_absolute():
        p = _BACKEND_ROOT / p
    if not p.is_file():
        raise RuntimeError(
            f"Google service-account file not found: {p} "
            f"(set GOOGLE_SA_CREDENTIALS_FILE to an absolute path or place it at the backend root)."
        )
    return p


def _client():
    """Authorized gspread client backed by the service-account JSON (env)."""
    import gspread  # deferred: keeps the Google SDK off the import path until needed

    return gspread.service_account(filename=str(_credentials_path()))


def prepare_sheet(
    *,
    topic_id: str,
    num_questions: int,
    child_order: int,
    duration_min: int,
    pass_percentage: float,          # fraction, e.g. 0.8 for 80%
    show_answer_scoring_mode: str = "INCORRECT",
    should_send_solutions: str = "yes",
    title: str = "MCQ Practice",
    share_emails: list[str] | None = None,
    resource_id: str | None = None,
) -> dict:
    """Copy the template, fill the Form tab, share it, and return its identifiers.

    `resource_id` becomes Form!B5 (the exam/unit id). Pass the SAME id used to name
    the questions JSON in the ZIP so the loader matches them; if omitted, a fresh one
    is generated.

    Returns ``{spreadsheet_id, url, resource_id, title}``. Raises RuntimeError with a
    clear message on any failing step so the caller/endpoint can surface it.
    """
    if not topic_id:
        raise RuntimeError(
            "Run has no topic_id — cannot set the parent resource id (Form!B14)."
        )

    gc = _client()
    resource_id = resource_id or str(uuid.uuid4())
    common_unit_id = str(uuid.uuid4())
    sheet_title = f"{title} - {resource_id}"

    try:
        sh = gc.copy(
            settings.mcq_template_spreadsheet_id,
            title=sheet_title,
            copy_permissions=False,
        )
    except Exception as err:  # noqa: BLE001 — surface the failing step
        raise RuntimeError(f"Failed to copy the exam-config template sheet: {err}") from err

    # Fill only the Form tab's input cells; formulas populate ResourcesData/Units/Exam.
    form = sh.worksheet(FORM_TAB)
    form.batch_update([
        {"range": CELL_RESOURCE_ID, "values": [[resource_id]]},
        {"range": CELL_COMMON_UNIT_ID, "values": [[common_unit_id]]},
        {"range": CELL_PARENT_RESOURCE_ID, "values": [[topic_id]]},
        {"range": CELL_CHILD_ORDER, "values": [[child_order]]},
        {"range": CELL_DURATION_MIN, "values": [[duration_min]]},
        {"range": CELL_NUM_QUESTIONS, "values": [[num_questions]]},
        {"range": CELL_PASS_PCT, "values": [[pass_percentage]]},
        {"range": CELL_SHOW_ANSWER_SCORING_MODE, "values": [[show_answer_scoring_mode]]},
        {"range": CELL_SHOULD_SEND_SOLUTIONS, "values": [[should_send_solutions]]},
    ])

    # Share with the configured editors + the requester. A sharing failure shouldn't
    # abort the prep — the service account still owns the sheet and the URL works.
    emails = [e.strip() for e in (settings.mcq_sheet_share_emails or "").split(",") if e.strip()]
    for e in (share_emails or []):
        if e and e not in emails:
            emails.append(e)
    for email in emails:
        try:
            sh.share(email, perm_type="user", role="writer")
        except Exception:  # noqa: BLE001
            pass

    return {
        "spreadsheet_id": sh.id,
        "url": sh.url,
        "resource_id": resource_id,
        "title": sheet_title,
    }
