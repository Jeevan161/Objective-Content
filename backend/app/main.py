"""
app/main.py
-----------
FastAPI application entrypoint (replaces the Django project). Serves the course
sync/extract pipeline + the scoped RAG under /api, for the existing React frontend.

Run:  uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.courses import router as courses_router
from app.api.llm_providers import router as llm_providers_router
from app.api.mcq_prompts import router as mcq_prompts_router
from app.core.config import settings

app = FastAPI(title="Objective Content", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
