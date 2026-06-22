"""Fetch reading-material content from the CCBP learning-resource API.

This uses the **learning** API with a user-supplied Bearer token (distinct from
the Django-admin login used elsewhere). For a LEARNING_SET unit, the unit_id is
the ``learning_resource_set_id``; posting it to the set endpoint returns the
set's resources, whose Markdown ``content`` is the reading material.

A reading material can be backed by two kinds of content: a step-by-step
**tutorial** or a single **cheat sheet** blob. A resource's ``resource_id``
doubles as its ``tutorial_entity_id``; when the tutorial endpoint returns steps
for it we prefer those (combined, in order) — some resources have *only* a
tutorial and an empty cheat-sheet ``content``. When no tutorial exists we fall
back to the cheat-sheet ``content`` (the original behaviour).

Reference: Portal_Data/Learning Resource Set Data Extraction (common.py,
extract_reading_material.py).
"""
import json
import re
from typing import NamedTuple

import requests
from bs4 import BeautifulSoup

from .constants import ENVIRONMENTS, DEFAULT_ENVIRONMENT


class ReadingMaterial(NamedTuple):
    """Result of extracting a learning-resource set: the combined Markdown plus
    the portal learning_resource ids of every resource in the set (ordered)."""

    content: str
    resource_ids: list[str]

CLIENT_KEY_DETAILS_ID = 1
X_APP_VERSION = "1219"
X_BROWSER_SESSION_ID = "902feabe-9080-4634-a577-4e1080ab3daa"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 60
TUTORIAL_PAGE_SIZE = 50  # tutorial/details is paginated; pull steps in big pages

_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")  # ![alt](url)
_BLANKS_RE = re.compile(r"\n{3,}")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def resource_set_url(environment: str = DEFAULT_ENVIRONMENT) -> str:
    """The learning_resources/set/ endpoint for the given environment."""
    base = ENVIRONMENTS[(environment or DEFAULT_ENVIRONMENT).upper()]["base_url"]
    return f"{base}/api/nkb_learning_resource/learning_resources/set/"


def tutorial_details_url(environment: str = DEFAULT_ENVIRONMENT) -> str:
    """The tutorial/details endpoint for the given environment."""
    base = ENVIRONMENTS[(environment or DEFAULT_ENVIRONMENT).upper()]["base_url"]
    return f"{base}/api/nkb_learning_resource/tutorial/details/v1/"


def build_headers(auth_token: str) -> dict:
    """Headers for the authenticated CCBP learning JSON API."""
    return {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "authorization": f"Bearer {auth_token}",
        "content-type": "application/json",
        "origin": "https://learning.ccbp.in",
        "referer": "https://learning.ccbp.in/",
        "user-agent": USER_AGENT,
        "x-app-version": X_APP_VERSION,
        "x-browser-session-id": X_BROWSER_SESSION_ID,
    }


def build_data_payload(key: str, value: str) -> str:
    """The API expects `data` as a double-encoded JSON string."""
    inner = json.dumps({key: value})
    return json.dumps(
        {"data": json.dumps(inner), "clientKeyDetailsId": CLIENT_KEY_DETAILS_ID}
    )


def clean_content(markdown: str) -> str:
    """Drop image embeds + HTML comments and collapse runs of blank lines."""
    text = _IMAGE_RE.sub("", markdown or "")
    text = _HTML_COMMENT_RE.sub("", text)
    text = _BLANKS_RE.sub("\n\n", text)
    return text.strip()


def fetch_resource_set(
    session: requests.Session, set_id: str, auth_token: str,
    environment: str = DEFAULT_ENVIRONMENT,
) -> dict:
    """Return the full API response for a learning_resource_set_id."""
    response = session.post(
        resource_set_url(environment),
        headers=build_headers(auth_token),
        data=build_data_payload("learning_resource_set_id", set_id),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def fetch_admin_content(client, resource_ids: list[str]) -> str:
    """Fetch + clean cheat-sheet content for resources via the **admin panel**,
    using ``client`` (a logged-in PortalClient) — no Bearer token required.

    Combines the cleaned ``content`` of each resource (most reading materials map
    to one). NOTE: the admin learningresource page exposes only the cheat-sheet
    ``content`` field — tutorial-only resources have an empty one here, so this
    returns '' for them (the token API is the only source of tutorial content).
    """
    pieces = []
    for rid in resource_ids:
        url = client.config.learning_resource_detail_url_template.format(rid)
        resp = client.get(url)
        soup = BeautifulSoup(resp.text, "html.parser")
        textarea = soup.find("textarea", {"name": "content"})
        cleaned = clean_content(textarea.text if textarea else "")
        if cleaned:
            pieces.append(cleaned)
    return "\n\n---\n\n".join(pieces)


def fetch_resource_ids(
    session: requests.Session, set_id: str, auth_token: str,
    environment: str = DEFAULT_ENVIRONMENT,
) -> list[str]:
    """Return the learning_resource ids of every resource in a set (ordered).

    Cheaper than :func:`fetch_reading_material` — it skips the tutorial fetch, so
    it's used for learning sets where we only want the ids, not the content.
    """
    data = fetch_resource_set(session, set_id, auth_token, environment)
    resources = sorted(
        data.get("learning_resources_set", []) or [], key=lambda r: r.get("order") or 0
    )
    return [r["resource_id"] for r in resources if r.get("resource_id")]


def fetch_tutorial_steps(
    session: requests.Session, entity_id: str, auth_token: str,
    environment: str = DEFAULT_ENVIRONMENT,
) -> list[dict]:
    """Return all tutorial steps for an entity, ordered, or [] if it has none.

    The endpoint is paginated (``length``/``offset`` with ``total_count``); we
    page through until every step is collected. A resource with no tutorial
    simply returns an empty ``tutorial_steps`` list.
    """
    url = tutorial_details_url(environment)
    headers = build_headers(auth_token)
    steps: list[dict] = []
    offset = 0
    while True:
        response = session.post(
            f"{url}?length={TUTORIAL_PAGE_SIZE}&offset={offset}",
            headers=headers,
            data=build_data_payload("tutorial_entity_id", entity_id),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        page = data.get("tutorial_steps") or []
        steps.extend(page)
        offset += len(page)
        if not page or offset >= (data.get("total_count") or 0):
            break
    steps.sort(key=lambda s: s.get("order") or 0)
    return steps


def fetch_reading_material(
    session: requests.Session, set_id: str, auth_token: str,
    environment: str = DEFAULT_ENVIRONMENT,
) -> ReadingMaterial:
    """Fetch and clean the reading-material Markdown for a learning resource set.

    For each resource in the set we prefer its **tutorial** (steps combined in
    order) when one exists, and otherwise fall back to its cheat-sheet
    ``content``. Cleaned pieces from every resource are concatenated (most sets
    have one). Returns a :class:`ReadingMaterial` with the combined content
    ('' when the set has none) and the ids of every resource in the set.
    """
    data = fetch_resource_set(session, set_id, auth_token, environment)
    resources = sorted(
        data.get("learning_resources_set", []) or [], key=lambda r: r.get("order") or 0
    )
    pieces = []
    resource_ids = []
    for resource in resources:
        entity_id = resource.get("resource_id")
        if entity_id:
            resource_ids.append(entity_id)
        steps = []
        if entity_id:
            try:
                steps = fetch_tutorial_steps(session, entity_id, auth_token, environment)
            except requests.RequestException:
                # A tutorial-endpoint hiccup must not drop the cheat-sheet content.
                steps = []
        if steps:
            cleaned = "\n\n".join(
                c for c in (clean_content(s.get("content") or "") for s in steps) if c
            )
        else:
            cleaned = clean_content(resource.get("content") or "")
        if cleaned:
            pieces.append(cleaned)
    return ReadingMaterial("\n\n---\n\n".join(pieces), resource_ids)
