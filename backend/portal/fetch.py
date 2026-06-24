"""Course / topic / unit fetching and HTML parsing.

Ported from the standalone ``fetch_course_topics_units.py`` script and made
reusable: version selection is passed in (instead of an interactive prompt)
and progress is surfaced through an optional ``progress`` callback so a caller
(e.g. a background job) can report status while it runs.
"""
import ast
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from typing import Callable, Optional
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from .client import PortalClient
from .constants import LEARNING_COURSE_URL

# How many portal requests to run concurrently while fetching a course.
DEFAULT_FETCH_CONCURRENCY = int(os.environ.get("PORTAL_FETCH_CONCURRENCY", "8"))

UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

ProgressFn = Callable[[str], None]


def _noop(_message: str) -> None:
    pass


# ── Link builders ─────────────────────────────────────────────────────────────
def build_course_link(course_id: str, learning_url: str = LEARNING_COURSE_URL) -> str:
    return f"{learning_url}?{urlencode([('c_id', course_id)])}"


def build_topic_link(course_id: str, topic_id: str,
                     learning_url: str = LEARNING_COURSE_URL) -> str:
    return f"{learning_url}?{urlencode([('c_id', course_id), ('t_id', topic_id)])}"


def build_unit_link(course_id: str, topic_id: str, unit_id: str,
                    learning_url: str = LEARNING_COURSE_URL) -> str:
    params = [("c_id", course_id), ("s_id", unit_id), ("t_id", topic_id)]
    return f"{learning_url}?{urlencode(params)}"


# ── Generic helpers ─────────────────────────────────────────────────────────────
def normalize_resource_id(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if not text:
        return ""
    match = UUID_PATTERN.search(text)
    return match.group(0) if match else text


def parse_order(order_text: str) -> int:
    text = (order_text or "").strip()
    return int(text) if text.isdigit() else 0


def parse_bool_text(value: str) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes"}


def parse_bool_cell(cell) -> bool:
    if cell is None:
        return False
    img = cell.find("img")
    if img:
        for attr_name in ("alt", "title", "aria-label"):
            if parse_bool_text(img.get(attr_name) or ""):
                return True
        return False
    return parse_bool_text(cell.get_text(strip=True))


# ── Resource-link parsing (fallback hierarchy source) ──────────────────────────
def parse_link_rows(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"id": "result_list"})
    if not table:
        return []
    tbody = table.find("tbody")
    if not tbody:
        return []

    rows = []
    for idx, tr in enumerate(tbody.find_all("tr"), start=1):
        from_cell = tr.find(["th", "td"], {"class": "field-from_resource_id"})
        to_cell = tr.find("td", {"class": "field-to_resource_id"})
        order_cell = tr.find("td", {"class": "field-order"})

        if from_cell is None or to_cell is None or order_cell is None:
            cells = tr.find_all(["th", "td"])
            if from_cell is None and len(cells) >= 1:
                from_cell = cells[0]
            if to_cell is None and len(cells) >= 2:
                to_cell = cells[1]
            if order_cell is None and len(cells) >= 3:
                order_cell = cells[2]

        from_resource_id = normalize_resource_id(from_cell.get_text(strip=True) if from_cell else "")
        to_resource_id = normalize_resource_id(to_cell.get_text(strip=True) if to_cell else "")
        order = parse_order(order_cell.get_text(strip=True) if order_cell else "")

        if from_resource_id:
            rows.append({
                "from_resource_id": from_resource_id,
                "to_resource_id": to_resource_id,
                "order": order,
                "_row_index": idx,
            })
    return rows


def extract_children(rows: list, parent_id: str) -> list:
    filtered = [
        row for row in rows
        if row.get("to_resource_id") == parent_id and row.get("from_resource_id") != parent_id
    ]
    filtered.sort(key=lambda row: (row.get("order", 0), row.get("_row_index", 0)))

    seen, children = set(), []
    for row in filtered:
        from_id = row["from_resource_id"]
        if from_id not in seen:
            seen.add(from_id)
            children.append(from_id)
    return children


def fetch_link_rows_for_resource(client: PortalClient, resource_id: str) -> list:
    response = client.get(client.config.resource_links_url, params={"q": resource_id})
    return parse_link_rows(response.text)


def fetch_hierarchy_from_resource_links(client: PortalClient, course_id: str) -> list:
    course_rows = fetch_link_rows_for_resource(client, course_id)
    topic_ids = extract_children(course_rows, course_id)

    hierarchy = []
    for topic_id in topic_ids:
        topic_rows = fetch_link_rows_for_resource(client, topic_id)
        unit_ids = extract_children(topic_rows, topic_id)
        hierarchy.append({"topic_id": topic_id, "unit_ids": unit_ids})
    return hierarchy


# ── Course versions ─────────────────────────────────────────────────────────────
def parse_course_version_rows(html: str, expected_course_id: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"id": "result_list"})
    if not table:
        return []
    tbody = table.find("tbody")
    if not tbody:
        return []

    version_rows = []
    for tr in tbody.find_all("tr"):
        id_cell = tr.find(["th", "td"], {"class": "field-id"})
        course_cell = tr.find("td", {"class": "field-course_id"})
        version_cell = tr.find("td", {"class": "field-version_id"})
        latest_cell = tr.find("td", {"class": "field-is_latest_version"})

        cells = tr.find_all(["th", "td"])
        if id_cell is None and len(cells) >= 1:
            id_cell = cells[0]
        if course_cell is None and len(cells) >= 2:
            course_cell = cells[1]
        if version_cell is None and len(cells) >= 3:
            version_cell = cells[2]
        if latest_cell is None and len(cells) >= 4:
            latest_cell = cells[3]

        row_id = ""
        if id_cell:
            link = id_cell.find("a")
            if link and link.get("href"):
                row_id = normalize_resource_id(link["href"])
            if not row_id:
                row_id = normalize_resource_id(id_cell.get_text(strip=True))

        course_id = normalize_resource_id(course_cell.get_text(strip=True) if course_cell else "")
        version_id = (version_cell.get_text(strip=True) if version_cell else "").strip()
        is_latest = parse_bool_cell(latest_cell)

        if not row_id or not course_id or course_id != expected_course_id:
            continue

        version_rows.append({
            "row_id": row_id,
            "course_id": course_id,
            "version_id": version_id,
            "is_latest_version": is_latest,
        })
    return version_rows


def fetch_course_versions(client: PortalClient, course_id: str) -> list:
    """Return the list of course-version rows for a course id (quick, one request)."""
    response = client.get(client.config.course_version_list_url, params={"q": course_id})
    return parse_course_version_rows(response.text, expected_course_id=course_id)


# ── Hierarchy from a selected version ───────────────────────────────────────────
def extract_hierarchy_text_from_version_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    textarea = soup.find("textarea", {"id": "id_hierarchy"}) or soup.find("textarea", {"name": "hierarchy"})
    if textarea:
        text = (textarea.get_text() or "").strip()
        if text:
            return text

    hierarchy_input = soup.find("input", {"id": "id_hierarchy"}) or soup.find("input", {"name": "hierarchy"})
    if hierarchy_input and hierarchy_input.get("value"):
        return hierarchy_input["value"].strip()

    readonly = soup.select_one(".field-hierarchy .readonly")
    if readonly:
        text = (readonly.get_text() or "").strip()
        if text:
            return text

    match = re.search(r'(\{"topics_hierarchy"\s*:\s*\[.*?\]\})', html, flags=re.DOTALL)
    if match:
        return unescape(match.group(1).strip())
    return ""


def parse_topics_hierarchy(hierarchy_text: str) -> list:
    raw_text = unescape((hierarchy_text or "").strip())
    if not raw_text:
        return []

    parsed = None
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(raw_text)
            break
        except Exception:
            continue

    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except Exception:
            parsed = None

    if not isinstance(parsed, dict):
        return []

    topics = parsed.get("topics_hierarchy", [])
    if not isinstance(topics, list):
        return []

    hierarchy = []
    for item in topics:
        if not isinstance(item, dict):
            continue
        topic_id = normalize_resource_id(str(item.get("topic_id", "")))
        if not topic_id:
            continue

        seen_units, unit_ids = set(), []
        for unit_id in item.get("unit_ids", []):
            normalized = normalize_resource_id(str(unit_id))
            if normalized and normalized not in seen_units:
                seen_units.add(normalized)
                unit_ids.append(normalized)

        hierarchy.append({"topic_id": topic_id, "unit_ids": unit_ids})
    return hierarchy


def fetch_hierarchy_from_selected_version(client: PortalClient, selected_version_row: dict) -> list:
    row_id = selected_version_row["row_id"]
    response = client.get(client.config.course_version_detail_url_template.format(row_id))
    hierarchy_text = extract_hierarchy_text_from_version_html(response.text)
    return parse_topics_hierarchy(hierarchy_text)


# ── Field extraction ────────────────────────────────────────────────────────────
def extract_input_value(html: str, input_id: Optional[str] = None, input_name: Optional[str] = None) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    attrs = {}
    if input_id:
        attrs["id"] = input_id
    if input_name:
        attrs["name"] = input_name
    if not attrs:
        return None
    input_el = soup.find("input", attrs)
    if input_el and input_el.get("value"):
        return input_el["value"].strip()
    return None


def extract_textarea_value(html: str, textarea_id: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    textarea_el = soup.find("textarea", {"id": textarea_id})
    return (textarea_el.get_text() or "").strip() if textarea_el else ""


def extract_selected_option_text(html: str, select_id: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    select_el = soup.find("select", {"id": select_id})
    if not select_el:
        return ""
    selected = select_el.find("option", selected=True)
    return (selected.get_text() or "").strip() if selected else ""


def parse_course_details(html: str, course_id: str,
                         learning_url: str = LEARNING_COURSE_URL) -> dict:
    return {
        "course_id": course_id,
        "course_name": extract_input_value(html, input_id="id_title") or "",
        "description": extract_textarea_value(html, "id_description"),
        "duration": extract_input_value(html, input_id="id_duration_in_sec") or "",
        "multimedia_url": extract_input_value(html, input_id="id_multimedia_url") or "",
        "course_category": extract_selected_option_text(html, "id_course_category"),
        "course_link": build_course_link(course_id, learning_url),
    }


def parse_topic_name(html: str) -> str:
    return extract_input_value(html, input_id="id_title") or ""


def extract_unit_type(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    unit_type_input = soup.find("input", {"name": "unit_type"})
    if unit_type_input and unit_type_input.get("value"):
        return unit_type_input["value"].strip()
    unit_type_select = soup.find("select", {"name": "unit_type"})
    if unit_type_select:
        selected = unit_type_select.find("option", selected=True)
        if selected:
            return selected.get_text(strip=True)
    return None


def extract_name_by_field(html: str, field_name: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    input_el = soup.find("input", {"name": field_name})
    if input_el and input_el.get("value"):
        return input_el["value"].strip()
    return None


def extract_generic_unit_name(unit_html: str) -> Optional[str]:
    for input_id in ("id_title", "id_name"):
        value = extract_input_value(unit_html, input_id=input_id)
        if value:
            return value
    for input_name in ("title", "name"):
        value = extract_input_value(unit_html, input_name=input_name)
        if value:
            return value
    return None


def fetch_unit_name(client: PortalClient, unit_id: str, unit_type: str, unit_html: str) -> Optional[str]:
    url_template = client.config.unit_name_url_map.get(unit_type)
    field_name = client.config.unit_name_field_map.get(unit_type)
    if url_template and field_name:
        try:
            response = client.get(url_template.format(unit_id))
            name = extract_name_by_field(response.text, field_name)
            if name:
                return name
        except Exception:
            pass
    return extract_generic_unit_name(unit_html)


# ── Unit grouping ────────────────────────────────────────────────────────────────
# Units are grouped into containers by UNIT TYPE:
#   • Session  — LEARNING_SET + QUIZ. A session begins at a (non-reading-material)
#                LEARNING_SET and absorbs its reading material and following
#                quizzes, up to the next session's LEARNING_SET.
#   • Practice — PRACTICE + QUESTION_SET, grouped as a consecutive run.
# Anything else becomes a single-part container.
SESSION_TYPES = {"LEARNING_SET", "QUIZ"}
PRACTICE_TYPES = {"PRACTICE", "QUESTION_SET"}
READING_MATERIAL_SUFFIX = "| Reading Material"
QUIZ_LETTER_RE = re.compile(r"^(.*?)[\s\-_:|]+([A-Za-z])$")


def _part(label: str, unit: dict) -> dict:
    return {
        "label": label,
        "unit_id": unit.get("unit_id", ""),
        "unit_type": unit.get("unit_type", ""),
        "name": unit.get("unit_name", ""),
        "link": unit.get("unit_link", ""),
        "error": unit.get("error", ""),
    }


def _quiz_letter(name: str):
    """Return a quiz's trailing single letter (A/B/C…) or None."""
    m = QUIZ_LETTER_RE.match((name or "").strip())
    return m.group(2).upper() if m else None


def _is_reading_material(unit: dict) -> bool:
    name = (unit.get("unit_name") or "").strip().lower()
    return unit.get("unit_type") == "LEARNING_SET" and name.endswith(
        READING_MATERIAL_SUFFIX.lower()
    )


def _session_part_label(unit: dict) -> str:
    if unit.get("unit_type") == "QUIZ":
        return _quiz_letter(unit.get("unit_name", "")) or "Quiz"
    if _is_reading_material(unit):
        return "Reading Material"
    return "Learning Resource"


def _practice_part_label(unit: dict) -> str:
    upper = (unit.get("unit_name") or "").upper()
    if "MCQ" in upper:
        return "MCQ"
    if "CODING" in upper:
        return "Coding"
    return "Practice" if unit.get("unit_type") == "PRACTICE" else "Question Set"


def group_units(units: list) -> list:
    """Group a topic's units into Session / Practice / single containers by type."""
    result = []
    i, n = 0, len(units)

    while i < n:
        unit = units[i]
        utype = unit.get("unit_type")

        if utype in SESSION_TYPES:
            parts = [_part(_session_part_label(unit), unit)]
            label = unit.get("unit_name", "") if not _is_reading_material(unit) else ""
            j = i + 1
            while j < n and units[j].get("unit_type") in SESSION_TYPES:
                nxt = units[j]
                # A new (non-reading-material) LEARNING_SET starts the next session.
                if nxt.get("unit_type") == "LEARNING_SET" and not _is_reading_material(nxt):
                    break
                parts.append(_part(_session_part_label(nxt), nxt))
                j += 1
            result.append({
                "kind": "SESSION",
                "label": label or unit.get("unit_name", "") or "Session",
                "parts": parts,
            })
            i = j

        elif utype in PRACTICE_TYPES:
            parts, j = [], i
            while j < n and units[j].get("unit_type") in PRACTICE_TYPES:
                parts.append(_part(_practice_part_label(units[j]), units[j]))
                j += 1
            result.append({"kind": "PRACTICE", "label": "Practice", "parts": parts})
            i = j

        else:
            result.append({
                "kind": "SINGLE",
                "label": unit.get("unit_name", "") or unit.get("unit_id", ""),
                "parts": [_part("Open", unit)],
            })
            i += 1

    return result


# ── Single-resource fetchers (safe to run in worker threads) ────────────────────
def fetch_topic_name(client: PortalClient, topic_id: str) -> str:
    try:
        response = client.get(client.config.topic_detail_url_template.format(topic_id))
        return parse_topic_name(response.text)
    except Exception:
        return ""


def fetch_unit_detail(client: PortalClient, course_id: str, topic_id: str, unit_id: str) -> dict:
    unit_data = {
        "unit_id": unit_id,
        "unit_link": build_unit_link(course_id, topic_id, unit_id, client.config.learning_course_url),
    }
    try:
        response = client.get(client.config.unit_detail_url_template.format(unit_id))
        unit_html = response.text
        unit_type = extract_unit_type(unit_html) or "UNKNOWN"
        unit_data["unit_type"] = unit_type
        unit_name = fetch_unit_name(client, unit_id, unit_type, unit_html)
        if unit_name:
            unit_data["unit_name"] = unit_name
    except Exception as err:
        unit_data["unit_type"] = "ERROR"
        unit_data["error"] = str(err)
    return unit_data


# ── Orchestration ───────────────────────────────────────────────────────────────
def build_course_data(
    client: PortalClient,
    course_id: str,
    selected_version_row: Optional[dict] = None,
    progress: Optional[ProgressFn] = None,
    max_workers: int = DEFAULT_FETCH_CONCURRENCY,
) -> dict:
    """Fetch full course data (details + topics + units) concurrently.

    ``selected_version_row`` is one of the dicts returned by
    :func:`fetch_course_versions`. When omitted, the hierarchy is derived from
    resource links. Topic and unit detail requests are issued in parallel
    (``max_workers`` at a time) instead of sequentially. ``progress`` is called
    with human-readable status strings — only from this (the calling) thread, so
    callers needn't make their callback thread-safe.
    """
    progress = progress or _noop
    topic_hierarchy = []

    if selected_version_row:
        progress(f"Reading hierarchy from version {selected_version_row.get('version_id', '')}…")
        topic_hierarchy = fetch_hierarchy_from_selected_version(client, selected_version_row)
        if not topic_hierarchy:
            progress("Selected version has no parseable hierarchy; falling back to resource links.")

    if not topic_hierarchy:
        progress("Deriving hierarchy from resource links…")
        topic_hierarchy = fetch_hierarchy_from_resource_links(client, course_id)

    progress("Fetching course details…")
    course_detail_response = client.get(client.config.course_detail_url_template.format(course_id))
    course_details = parse_course_details(course_detail_response.text, course_id,
                                          client.config.learning_course_url)
    if selected_version_row:
        course_details["selected_course_version"] = {
            "courseversion_id": selected_version_row.get("row_id", ""),
            "version_id": selected_version_row.get("version_id", ""),
            "is_latest_version": bool(selected_version_row.get("is_latest_version", False)),
        }

    topics = [t for t in topic_hierarchy if t.get("topic_id")]
    total_topics = len(topics)
    total_units = sum(len(t.get("unit_ids", [])) for t in topics)
    progress(
        f"Found {total_topics} topic(s), {total_units} unit(s). "
        f"Fetching concurrently ({max_workers} at a time)…"
    )

    # Progress is updated only from this thread (in the as-completed loop below),
    # so the callback stays single-writer even though fetches run in parallel.
    done = 0

    def note_unit_done(_result):
        nonlocal done
        done += 1
        if done == total_units or done % 5 == 0:
            progress(f"Fetched {done}/{total_units} unit(s)…")
        return _result

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        # Kick off every topic-name and unit-detail request at once.
        topic_name_futures = {
            t["topic_id"]: executor.submit(fetch_topic_name, client, t["topic_id"])
            for t in topics
        }
        unit_futures = {
            (t["topic_id"], unit_id): executor.submit(
                fetch_unit_detail, client, course_id, t["topic_id"], unit_id
            )
            for t in topics
            for unit_id in t.get("unit_ids", [])
        }

        # Wait on units as they finish, reporting incremental progress.
        unit_results = {}
        for key, future in unit_futures.items():
            unit_results[key] = note_unit_done(future.result())

        # Reassemble in the original topic / unit order.
        topics_payload = []
        for t in topics:
            topic_id = t["topic_id"]
            units_payload = [unit_results[(topic_id, uid)] for uid in t.get("unit_ids", [])]
            topics_payload.append({
                "topic_id": topic_id,
                "topic_name": topic_name_futures[topic_id].result(),
                "topic_link": build_topic_link(course_id, topic_id, client.config.learning_course_url),
                "units": group_units(units_payload),
            })

    return {"course_details": course_details, "topics": topics_payload}
