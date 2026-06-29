#!/usr/bin/env python3
"""
Thin CLI over `app.services.quiz_scopes` — segment a published Google Slides deck
into "Quiz scopes" and print each scope's slides.

The segmentation logic now lives in `app/services/quiz_scopes.py` so the Classroom
Quiz pipeline and API can import it; this file just renders it on the command line.

Usage:
    python3 quiz_scopes.py "<published pubembed/pub URL>"
"""

import sys

from app.services.quiz_scopes import (
    AGENDA_MARKER,
    END_MARKER,
    QUIZ_MARKER,
    fetch,
    find_marker_slides,
    parse_slides,
    segment_scopes,
)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python3 quiz_scopes.py <published-slides-url>")
    url = sys.argv[1]

    slides = parse_slides(fetch(url))
    total = len(slides)
    agenda = find_marker_slides(slides, AGENDA_MARKER)
    quizzes = sorted(set(find_marker_slides(slides, QUIZ_MARKER)))
    end = find_marker_slides(slides, END_MARKER)
    end_slide = min(end) if end else total

    if not agenda:
        sys.exit("Could not find an 'Agenda for Today's Session' slide.")

    print(f"Total slides            : {total}")
    print(f"Last Agenda slide       : {max(agenda)}")
    print(f"'Quiz Time!' slides     : {quizzes}")
    print(f"'Key Takeaways' slide   : {end_slide}")
    print("-" * 60)

    for scope in segment_scopes(slides):
        print("=" * 72)
        print(f"QUIZ SCOPE #{scope.scope_no}: slides {scope.slide_start}-{scope.slide_end}  "
              f"(closes at '{scope.kind}')")
        print("=" * 72)
        if scope.slide_text:
            print(scope.slide_text)
        print()


if __name__ == "__main__":
    main()
