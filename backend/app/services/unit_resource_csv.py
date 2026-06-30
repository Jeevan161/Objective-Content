"""
app/services/unit_resource_csv.py
---------------------------------
Token-free discovery of a course's learning-resource ids via the content-loading
admin's ``GET_UNIT_RESOURCE_DETAILS`` task.

Why this exists
===============
Reading-material content is scraped token-free from the portal admin
(``portal.learning_resource.fetch_admin_content``) — but that only works once a
UnitPart already knows its individual ``learning_resource_id``\\ s, which until now
could *only* be discovered with a per-environment Bearer token (the learning API).

``GET_UNIT_RESOURCE_DETAILS`` breaks that chicken-and-egg: submitted with just a
``course_id`` to the same content-loading admin we already log into for sheet
loading, it returns a CSV (presigned S3 link) listing every unit with its
``learning_resource_id``. We parse that map and hand the ids to the existing admin
scraper — so extraction needs no Bearer token at all.

The CSV columns are::

    course_id, course_title, topic_id, topic_title, topic_order,
    unit_id, unit_title, unit_type, unit_order,
    learning_resource_id, slide_urls, unit_duration, unit_link

where ``unit_id`` is the learning_resource **set** id (== ``UnitPart.unit_id``) and
``learning_resource_id`` is the individual resource id (== ``UnitPart.resource_ids``).
"""

from __future__ import annotations

import ast
import csv
import io
import json
import re
import time

import requests
from bs4 import BeautifulSoup

from app.core.config import settings

_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/127.0.6533.89 Safari/537.36")
_CSRF_FIELD = "csrfmiddlewaretoken"
_CONTENT_LOADING_PATH = "/admin/nkb_load_data/contentloading/add/"
_CHANGE_PATH = "/admin/nkb_load_data/contentloading/{}/change/"
_REQUEST_ID_RE = re.compile(r"/contentloading/([a-f0-9\-]{36})/change/")
_TASK_TYPE = "GET_UNIT_RESOURCE_DETAILS"
LEARNING_SET_TYPE = "LEARNING_SET"


# --------------------------------------------------------------------------- #
# Environment → content-loading admin (base url + credentials).
# BETA is wired today; PROD falls back to BETA's host until a PROD content-loading
# admin is configured (optional settings), so the same code path serves both.
# --------------------------------------------------------------------------- #
def _resolve_admin(environment: str) -> tuple[str, str, str]:
    env = (environment or "BETA").upper()
    prod_base = getattr(settings, "prod_admin_base_url", None)
    if env == "PROD" and prod_base:
        return (
            prod_base,
            getattr(settings, "prod_admin_username", None) or settings.beta_admin_username,
            getattr(settings, "prod_admin_password", None) or settings.beta_admin_password,
        )
    return (settings.beta_admin_base_url, settings.beta_admin_username,
            settings.beta_admin_password)


def _login(base_url: str, username: str, password: str) -> requests.Session:
    """Authenticate to a content-loading admin; return a session with `sessionid`."""
    if not (base_url and username and password):
        raise RuntimeError("Content-loading admin base url / credentials are not set.")
    login_url = f"{base_url}/admin/login/"
    session = requests.Session()
    headers = {"Referer": login_url, "User-Agent": _USER_AGENT}
    page = session.get(login_url, headers=headers, allow_redirects=True, timeout=20)
    if page.status_code != 200:
        raise RuntimeError(f"Admin login page returned {page.status_code}.")
    csrf = BeautifulSoup(page.text, "html.parser").find("input", {"name": _CSRF_FIELD})
    if not csrf or not csrf.get("value"):
        raise RuntimeError("CSRF token not found on the admin login page.")
    session.post(login_url, headers=headers, allow_redirects=True, timeout=20, data={
        "username": username, "password": password, _CSRF_FIELD: csrf["value"],
    })
    if "sessionid" not in session.cookies.get_dict():
        raise RuntimeError("Admin login failed — session cookie missing (check creds).")
    return session


def _csrf(session: requests.Session, url: str) -> str:
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    el = BeautifulSoup(resp.text, "html.parser").find("input", {"name": _CSRF_FIELD})
    token = el["value"] if el and el.get("value") else session.cookies.get("csrftoken")
    if not token:
        raise RuntimeError("Could not get a CSRF token for the content-loading form.")
    return token


def _submit(session: requests.Session, base_url: str, course_id: str) -> str:
    """POST a GET_UNIT_RESOURCE_DETAILS task; return the request id from the redirect.

    ``input_data`` is a LIST of dicts (the server iterates it and reads ``course_id``
    off each) — a plain dict raises ``'str' object has no attribute 'get'`` server-side.
    """
    url = f"{base_url}{_CONTENT_LOADING_PATH}"
    resp = session.post(
        url, headers={"Referer": url, "User-Agent": _USER_AGENT},
        allow_redirects=True, timeout=60,
        data={
            _CSRF_FIELD: _csrf(session, url),
            "task_type": _TASK_TYPE,
            "input_data": json.dumps([{"course_id": course_id}]),
            "_continue": "Save and view",
        },
    )
    resp.raise_for_status()
    match = _REQUEST_ID_RE.search(resp.url)
    if not match:
        raise RuntimeError("GET_UNIT_RESOURCE_DETAILS submit did not redirect to a request id.")
    return match.group(1)


def _field_readonly(soup: BeautifulSoup, field: str) -> str | None:
    div = soup.find("div", class_=f"form-row field-{field}")
    ro = div.find("div", class_="readonly") if div else None
    return ro.get_text(strip=True) if ro else None


def _poll_csv_url(session: requests.Session, base_url: str, request_id: str,
                  *, timeout: int, interval: int = 4) -> str:
    """Poll the change page until SUCCESS and return the presigned CSV S3 url."""
    change_url = f"{base_url}{_CHANGE_PATH.format(request_id)}"
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        soup = BeautifulSoup(session.get(change_url, timeout=30).text, "html.parser")
        last_status = _field_readonly(soup, "task_status") or last_status
        out = _field_readonly(soup, "task_output_url")
        if out:
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                data = None
            if data:
                csv_url = (data.get("response") or {}).get("unit_resource_details_csv_s3_url")
                if csv_url:
                    return csv_url
                if last_status == "FAILURE" or data.get("exception"):
                    raise RuntimeError(
                        f"GET_UNIT_RESOURCE_DETAILS task failed "
                        f"(see {data.get('exception') or change_url})."
                    )
        if last_status == "FAILURE":
            raise RuntimeError(f"GET_UNIT_RESOURCE_DETAILS task failed ({change_url}).")
        time.sleep(interval)
    raise RuntimeError(
        f"Timed out after {timeout}s waiting for GET_UNIT_RESOURCE_DETAILS ({change_url})."
    )


def parse_csv(data: bytes) -> dict[str, dict]:
    """Parse the unit_resource_details CSV → ``{unit_id: {...}}`` for LEARNING_SET rows.

    Keyed by ``unit_id`` (the learning_resource set id), matching ``UnitPart.unit_id``.
    """
    out: dict[str, dict] = {}
    reader = csv.DictReader(io.StringIO(data.decode("utf-8", "replace")))
    for row in reader:
        if (row.get("unit_type") or "").strip() != LEARNING_SET_TYPE:
            continue
        unit_id = (row.get("unit_id") or "").strip()
        lrid = (row.get("learning_resource_id") or "").strip()
        if not unit_id or not lrid:
            continue
        out[unit_id] = {
            "learning_resource_id": lrid,
            "slide_urls": _parse_slide_urls(row.get("slide_urls")),
            "unit_title": (row.get("unit_title") or "").strip(),
        }
    return out


def _parse_slide_urls(raw: str | None) -> list[str]:
    """The CSV stores slide_urls as a Python-list literal string, e.g. "['https://…']"."""
    if not raw or not raw.strip():
        return []
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return []
    return [str(u) for u in value] if isinstance(value, (list, tuple)) else []


def fetch_unit_resource_map(course_id: str, *, environment: str = "BETA",
                            timeout: int | None = None) -> dict[str, dict]:
    """Submit + poll + download + parse, returning ``{unit_id: {...}}`` for a course.

    Reuses the content-loading admin session (no Bearer token). Raises RuntimeError
    with a clear message if any step fails.
    """
    base_url, username, password = _resolve_admin(environment)
    timeout = timeout or settings.beta_load_poll_timeout
    session = _login(base_url, username, password)
    request_id = _submit(session, base_url, course_id)
    csv_url = _poll_csv_url(session, base_url, request_id, timeout=timeout)
    resp = requests.get(csv_url, timeout=120)
    resp.raise_for_status()
    return parse_csv(resp.content)
