"""
app/services/beta_s3.py
-----------------------
Upload an export artifact to the BETA content-loading S3 bucket, mirroring the
Nxtwave config app's flow:

  1. log in to the beta admin (session cookie),
  2. scrape the short-lived AWS credentials it embeds on its upload page
     (``AWS.Credentials('<key>', '<secret>', '<token>')``),
  3. upload the file to the shared media bucket with public-read ACL,
  4. return the public URL.

Credentials/config come from `settings` (env), not hardcoded. The upload streams
bytes straight from memory (no temp file on disk — important for a deployed,
multi-replica/ephemeral-FS service).
"""

from __future__ import annotations

import io
import json
import re
import time
import uuid

import requests
from bs4 import BeautifulSoup

from app.core.config import settings

_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/127.0.6533.89 Safari/537.36")
_CSRF_FIELD = "csrfmiddlewaretoken"

# --- beta admin content-loading (sheet load / unlock) ---
_CONTENT_LOADING_PATH = "/admin/nkb_load_data/contentloading/add/"
_CHANGE_PATH = "/admin/nkb_load_data/contentloading/{}/change/"
_REQUEST_ID_RE = re.compile(r"/contentloading/([a-f0-9\-]+)/change/")
_TASK_TYPE_SHEET_LOADING = "SHEET_LOADING"
_TASK_TYPE_UNLOCK = "UNLOCK_RESOURCES_FOR_USERS"

_CRED_RE = re.compile(r"AWS\.Credentials")
_ACCESS_RE = re.compile(r"AWS\.Credentials\(\s*'([^']+)'")
_SECRET_RE = re.compile(r"AWS\.Credentials\(\s*'[^']+',\s*'([^']+)'")
_TOKEN_RE = re.compile(r"AWS\.Credentials\(\s*'[^']+',\s*'[^']+',\s*'([^']+)'")


def _login() -> requests.Session | None:
    """Authenticate to the beta admin; return a session with the `sessionid` cookie."""
    if not (settings.beta_admin_username and settings.beta_admin_password):
        raise RuntimeError("BETA_ADMIN_USERNAME / BETA_ADMIN_PASSWORD are not set.")
    login_url = f"{settings.beta_admin_base_url}/admin/login/"
    session = requests.Session()
    headers = {"Referer": login_url, "User-Agent": _USER_AGENT}

    page = session.get(login_url, headers=headers, allow_redirects=True, timeout=20)
    if page.status_code != 200:
        return None
    csrf = BeautifulSoup(page.text, "html.parser").find("input", {"name": _CSRF_FIELD})
    if not csrf or not csrf.get("value"):
        return None
    session.post(login_url, headers=headers, allow_redirects=True, timeout=20, data={
        "username": settings.beta_admin_username,
        "password": settings.beta_admin_password,
        _CSRF_FIELD: csrf["value"],
    })
    return session if "sessionid" in session.cookies.get_dict() else None


def _scrape_credentials(session: requests.Session) -> dict | None:
    """Extract the short-lived AWS credentials embedded on the upload page."""
    url = f"{settings.beta_admin_base_url}/admin/nkb_load_data/uploadfile/add/"
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    script = BeautifulSoup(resp.content, "html.parser").find("script", text=_CRED_RE)
    if not script:
        return None
    access = _ACCESS_RE.search(script.text)
    secret = _SECRET_RE.search(script.text)
    token = _TOKEN_RE.search(script.text)
    if not (access and secret and token):
        return None
    return {
        "aws_access_key_id": access.group(1),
        "aws_secret_access_key": secret.group(1),
        "aws_session_token": token.group(1),
    }


def upload_bytes(data: bytes, filename: str, *, content_type: str = "application/zip") -> str:
    """Upload `data` to the beta S3 bucket and return its public URL.

    Raises RuntimeError with a clear message on any step that fails (login, creds,
    upload) so the caller/endpoint can surface it."""
    import boto3  # deferred: keeps the heavy SDK off the import path until needed

    session = _login()
    if session is None:
        raise RuntimeError("Beta admin login failed (check BETA_ADMIN_* credentials).")
    cred = _scrape_credentials(session)
    if cred is None:
        raise RuntimeError("Could not obtain AWS credentials from the beta admin page.")

    s3 = boto3.client(
        "s3",
        aws_access_key_id=cred["aws_access_key_id"],
        aws_secret_access_key=cred["aws_secret_access_key"],
        aws_session_token=cred["aws_session_token"],
        region_name=settings.beta_s3_region,
    )
    key = f"{settings.beta_s3_upload_folder}{uuid.uuid4()}_{filename}"
    s3.upload_fileobj(
        io.BytesIO(data), settings.beta_s3_bucket, key,
        ExtraArgs={"ACL": "public-read", "ContentType": content_type},
    )
    return (f"https://{settings.beta_s3_bucket}.s3.{settings.beta_s3_region}"
            f".amazonaws.com/{key}")


# --------------------------------------------------------------------------- #
# Content-loading: submit a sheet-loading / unlock task and poll its status.
# These reuse the same beta-admin session login as the S3 upload above.
# --------------------------------------------------------------------------- #
def _csrf(session: requests.Session, url: str) -> str | None:
    """CSRF token for a beta-admin form (input field, falling back to the cookie)."""
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    el = BeautifulSoup(resp.text, "html.parser").find("input", {"name": _CSRF_FIELD})
    if el and el.get("value"):
        return el["value"]
    return session.cookies.get("csrftoken")


def _submit_content_loading(session: requests.Session, task_type: str, input_data: dict) -> str:
    """POST a content-loading task and return the request id parsed from the redirect."""
    url = f"{settings.beta_admin_base_url}{_CONTENT_LOADING_PATH}"
    csrf = _csrf(session, url)
    if not csrf:
        raise RuntimeError("Could not get a CSRF token for the content-loading form.")
    resp = session.post(
        url,
        headers={"Referer": url, "User-Agent": _USER_AGENT},
        allow_redirects=True, timeout=60,
        data={
            "csrfmiddlewaretoken": csrf,
            "task_type": task_type,
            "input_data": json.dumps(input_data),
            "_continue": "Save and view",
        },
    )
    resp.raise_for_status()
    if resp.history:
        match = _REQUEST_ID_RE.search(resp.url)
        if match:
            return match.group(1)
    raise RuntimeError(f"Content-loading submit ({task_type}) did not redirect to a request id.")


def _logged_in_session() -> requests.Session:
    session = _login()
    if session is None:
        raise RuntimeError("Beta admin login failed (check BETA_ADMIN_* credentials).")
    return session


def submit_sheet_loading(*, spreadsheet_id: str, spread_sheet_name: str, s3_url: str) -> str:
    """Kick off the SHEET_LOADING task for a prepared exam-config sheet + questions ZIP."""
    return _submit_content_loading(_logged_in_session(), _TASK_TYPE_SHEET_LOADING, {
        "spread_sheet_name": spread_sheet_name,
        "spreadsheet_id": spreadsheet_id,
        "data_sets_to_be_loaded": ["ResourcesData", "Units", "Exam"],
        "exam_questions_dir_path_url": s3_url,
        "is_json_converted": False,
    })


def submit_unlock(resource_id: str) -> str:
    """Unlock a loaded resource for users (UNLOCK_RESOURCES_FOR_USERS)."""
    return _submit_content_loading(_logged_in_session(), _TASK_TYPE_UNLOCK, {
        "resource_ids": [resource_id],
    })


def poll_task(request_id: str, *, timeout: int | None = None, interval: int = 4) -> tuple[str, str]:
    """Poll a content-loading task to completion.

    Returns (status, message) where status is "SUCCESS" | "FAILURE" | "INCOMPLETE".
    Bounded by `timeout` seconds (default settings.beta_load_poll_timeout) so it never
    blocks the request thread indefinitely the way the config app's loop does.
    """
    timeout = timeout or settings.beta_load_poll_timeout
    session = _logged_in_session()
    url = f"{settings.beta_admin_base_url}{_CHANGE_PATH.format(request_id)}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            div = BeautifulSoup(resp.content, "html.parser").find(
                "div", class_="form-row field-task_output_url")
            ro = div.find("div", class_="readonly") if div else None
            if ro:
                try:
                    data = json.loads(ro.get_text(strip=True))
                except json.JSONDecodeError:
                    data = None
                if data:
                    response_data = data.get("response") or {}
                    if data.get("sheet_loading_status") == "SUCCESS":
                        return "SUCCESS", ""
                    if response_data.get("status") == "SUCCESS":
                        return "SUCCESS", ""
                    if data.get("output") and not data.get("exception"):
                        return "SUCCESS", ""
                    if data.get("exception"):
                        return "FAILURE", str(data["exception"])
        except Exception:  # noqa: BLE001 — transient page/parse errors: keep polling
            pass
        time.sleep(interval)
    return "INCOMPLETE", f"Timed out after {timeout}s waiting for the load task."
