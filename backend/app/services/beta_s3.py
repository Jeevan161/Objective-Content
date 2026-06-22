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
import re
import uuid

import requests
from bs4 import BeautifulSoup

from app.core.config import settings

_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/127.0.6533.89 Safari/537.36")
_CSRF_FIELD = "csrfmiddlewaretoken"

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
