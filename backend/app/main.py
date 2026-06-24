"""
app/main.py
-----------
FastAPI application entrypoint (replaces the Django project). Serves the course
sync/extract pipeline + the scoped RAG under /api, for the existing React frontend.

Run:  uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.courses import router as courses_router
from app.api.llm_providers import router as llm_providers_router
from app.api.mcq_prompts import router as mcq_prompts_router
from app.core.config import settings

# App-level logging to stdout (container logs); per-task rows also land in TaskLog.
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("app")

app = FastAPI(title="Objective Content", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _log_unhandled_errors(request: Request, call_next):
    """Log any unhandled exception with request context, then return a clean 500.
    (HTTPExceptions raised by handlers are NOT caught here — FastAPI handles those.)"""
    try:
        return await call_next(request)
    except Exception:  # noqa: BLE001
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error."})


app.include_router(auth_router)
app.include_router(courses_router)
app.include_router(mcq_prompts_router)
app.include_router(llm_providers_router)


@app.on_event("startup")
def _disable_external_tracing() -> None:
    """We use our own node-span tracing (McqTrace); force LangSmith/LangChain auto-export OFF so it
    never ships runs out (or hits the LangSmith rate limit), regardless of what's in .env."""
    try:
        from app.mcq_pipeline.utils.tracing import disable_langsmith

        disable_langsmith()
    except Exception:  # noqa: BLE001
        pass


@app.on_event("startup")
def _seed_mcq_prompts() -> None:
    """Seed the editable MCQ prompts from code defaults on boot (idempotent,
    best-effort — a no-op if the pipeline deps or table aren't available)."""
    try:
        from app.mcq_pipeline.prompts.store import seed_prompts

        seed_prompts()
    except Exception:  # noqa: BLE001
        pass


@app.on_event("startup")
def _seed_llm_providers() -> None:
    """Seed the LLM connectors on boot: an active 'openrouter' mirroring the current
    settings (zero behavior change) + an inactive 'proxy' preset. Idempotent / best-effort."""
    try:
        from app.mcq_pipeline.utils.llm import seed_providers

        seed_providers()
    except Exception:  # noqa: BLE001
        pass


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# --- Serve the built frontend (single-container deploy) --------------------- #
# In production we bundle the React build and serve it from FastAPI so the SPA,
# the /api routes, and the WebSocket all share one origin and port. The directory
# only exists inside the Docker image; in local dev the frontend runs on Vite and
# this mount is skipped. Mounted LAST so /api and /health always take precedence.
_frontend_dist = os.environ.get(
    "FRONTEND_DIST_DIR",
    str(Path(__file__).resolve().parent.parent / "frontend_dist"),
)
if os.path.isdir(_frontend_dist):
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
    logger.info("Serving bundled frontend from %s", _frontend_dist)
