"""
app/services/quiz_scopes.py
---------------------------
Segment a published Google Slides deck into per-quiz "scopes" — the unit of work
for the Classroom Quiz pipeline. One scope == the slides taught between two
"Quiz Time!" checkpoints (or up to "Key Takeaways" for the final stretch).

Scope rule (per the spec):
  - Content begins on the slide AFTER the LAST "Agenda for Today's Session" slide.
  - Quiz #1  = (last Agenda slide + 1)  ..  1st "Quiz Time!" slide
  - Quiz #2  = (slide after 1st Quiz)   ..  2nd "Quiz Time!" slide
  - ... and so on for every "Quiz Time!" slide.
  - The final scope runs from (slide after last Quiz Time) .. "Key Takeaways" slide.
  - A deck with NO "Quiz Time!" slides yields exactly ONE scope (Agenda+1 .. Key Takeaways).

This module is the importable core (used by the pipeline + API); `backend/quiz_scopes.py`
is a thin CLI wrapper around `scope_slides()`.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

AGENDA_MARKER = "Agenda for Today"   # tolerant of the curly apostrophe in "Today's"
QUIZ_MARKER = "Quiz Time"
END_MARKER = "Key Takeaways"

# aria-labels that are just embedded asset filenames, not slide copy
ASSET_RE = re.compile(r"\.(png|jpe?g|gif|svg|webp)$", re.IGNORECASE)


@dataclass
class Scope:
    """One quiz-worth of slides. `slide_text` is the readable copy fed to the
    reading-material node (m00); `slides` keeps the raw per-slide breakdown."""

    scope_no: int
    kind: str            # "Quiz Time!" | "Key Takeaways" — what closes the scope
    slide_start: int
    slide_end: int
    slides: list[dict] = field(default_factory=list)

    @property
    def slide_text(self) -> str:
        """Readable copy of every slide in the scope, slide-delimited so the
        reading-material node can see the original teaching order/boundaries."""
        out: list[str] = []
        for s in self.slides:
            txt = slide_text(s)
            if txt:
                out.append(f"--- Slide {s['n']} ---\n" + "\n".join(txt.split(" | ")))
        return "\n\n".join(out)

    def to_dict(self) -> dict:
        return {
            "scope_no": self.scope_no,
            "kind": self.kind,
            "slide_start": self.slide_start,
            "slide_end": self.slide_end,
            "slide_text": self.slide_text,
        }


def normalize_url(url: str) -> str:
    """Drop the per-slide anchor (slide=id...) so we always get the full filmstrip
    in canonical presentation order."""
    parts = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "slide"]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(q), "")
    )


def fetch(url: str) -> str:
    req = urllib.request.Request(normalize_url(url), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def decode(raw: str) -> str:
    """Decode Google's \\xNN hex escapes, then HTML entities; collapse newlines/space."""
    s = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), raw)
    s = s.replace("\\/", "/")
    s = html.unescape(s)
    return " ".join(s.split())


def parse_slides(data: str) -> list[dict]:
    """Return ordered list of slides: [{'n': int, 'labels': [str,...]}].

    Each rendered slide is one inline SVG block (escaped as \\x3csvg ...). We split
    on that boundary and collect the aria-label text boxes inside each block, in order.
    """
    blocks = re.split(r"\\x3csvg", data)[1:]  # drop preamble before the first slide
    slides = []
    for i, block in enumerate(blocks, 1):
        labels = [decode(l) for l in re.findall(r"aria-label\\x3d\\x22(.*?)\\x22", block)]
        slides.append({"n": i, "labels": [l for l in labels if l]})
    return slides


def slide_text(slide: dict) -> str:
    """Readable copy for a slide (drops asset-filename labels)."""
    return " | ".join(l for l in slide["labels"] if not ASSET_RE.search(l))


def find_marker_slides(slides: list[dict], marker: str) -> list[int]:
    return [s["n"] for s in slides if any(marker in l for l in s["labels"])]


def segment_scopes(slides: list[dict]) -> list[Scope]:
    """Pure segmentation: slides -> ordered Scopes. Raises ValueError if the deck has
    no Agenda slide (we can't locate where content begins). A deck with no 'Quiz Time!'
    slides collapses to a single scope (Agenda+1 .. Key Takeaways)."""
    total = len(slides)
    agenda = find_marker_slides(slides, AGENDA_MARKER)
    if not agenda:
        raise ValueError("Could not find an 'Agenda for Today's Session' slide.")

    quizzes = sorted(set(find_marker_slides(slides, QUIZ_MARKER)))
    end = find_marker_slides(slides, END_MARKER)
    end_slide = min(end) if end else total

    last_agenda = max(agenda)
    # Each quiz slide closes a scope; the final scope closes at Key Takeaways (or the
    # last slide). When there are no quiz slides, `closers` == [end_slide] -> one scope.
    closers = list(quizzes)
    if end_slide > (quizzes[-1] if quizzes else 0):
        closers.append(end_slide)

    scopes: list[Scope] = []
    start = last_agenda + 1
    for closer in closers:
        if closer < start:
            continue
        scope_no = len(scopes) + 1
        kind = "Quiz Time!" if closer in quizzes else "Key Takeaways"
        body = [s for s in slides if start <= s["n"] <= closer]
        scopes.append(Scope(scope_no=scope_no, kind=kind,
                            slide_start=start, slide_end=closer, slides=body))
        start = closer + 1
    return scopes


def scope_slides(url: str) -> list[Scope]:
    """Fetch a published Slides deck and segment it into quiz scopes. The single
    entry point used by the pipeline + API."""
    return segment_scopes(parse_slides(fetch(url)))
