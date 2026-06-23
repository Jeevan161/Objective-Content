"""
app/services/progress_broker.py
-------------------------------
In-process pub/sub that bridges the MCQ pipeline's progress (produced in background WORKER THREADS
by `_mcq_sink`) to async WebSocket handlers (in the event loop). A publisher fires a lightweight
"changed" trigger for a job_id; each subscribed WebSocket then re-reads the latest job row from the
DB and pushes it. The DB stays the source of truth (so a late-joining socket / page refresh is
correct); this only delivers the "something changed, look now" nudge with no polling latency.

SINGLE-PROCESS ONLY: the job thread and the socket handler must share memory. The app runs as one
uvicorn process (no --workers), so this holds. A multi-worker / multi-replica deploy would need a
shared bus (e.g. Redis pub/sub) instead — swap this module's internals, not its callers.
"""
from __future__ import annotations

import asyncio
import threading

_lock = threading.Lock()
# job_id -> list of (event loop the subscriber lives on, its queue)
_subs: dict[str, list[tuple[asyncio.AbstractEventLoop, "asyncio.Queue"]]] = {}


def subscribe(job_id: str) -> "asyncio.Queue":
    """Register the current async task as a subscriber; returns its trigger queue. Call from an
    async context (captures the running loop so cross-thread publishes can wake it)."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    with _lock:
        _subs.setdefault(job_id, []).append((loop, q))
    return q


def unsubscribe(job_id: str, q: "asyncio.Queue") -> None:
    with _lock:
        lst = _subs.get(job_id)
        if not lst:
            return
        remaining = [(lp, qq) for (lp, qq) in lst if qq is not q]
        if remaining:
            _subs[job_id] = remaining
        else:
            _subs.pop(job_id, None)


def publish(job_id: str) -> None:
    """Notify every subscriber that `job_id` changed. Safe to call from worker threads; never
    raises. Triggers coalesce (a full queue already has a pending nudge), and the subscriber always
    re-reads the latest state, so no update is lost."""
    with _lock:
        targets = list(_subs.get(job_id, ()))
    for loop, q in targets:
        try:
            loop.call_soon_threadsafe(_offer, q)
        except Exception:  # noqa: BLE001 — a dead/closed loop must not break the run
            pass


def _offer(q: "asyncio.Queue") -> None:
    try:
        q.put_nowait(1)
    except asyncio.QueueFull:
        pass  # a nudge is already pending; the handler will re-read the latest state anyway
