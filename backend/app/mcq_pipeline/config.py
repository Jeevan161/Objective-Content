"""
app/mcq_pipeline/config.py
--------------------------
Shim replacing the Workflow's standalone `config` module. The vendored agent
modules `import config` and read these names; here they come from app settings so
there is ONE source of truth (OpenRouter key/base + model ids).
"""

from __future__ import annotations

from app.core.config import settings

OPENROUTER_API_KEY = settings.openrouter_api_key
OPENROUTER_BASE_URL = settings.openrouter_base_url
# The strong model drives the LO agents + question generation/review.
AGENT_MODEL = settings.mcq_agent_model
# Chat/embed models (kept for parity; retrieval itself goes through app services).
CHAT_MODEL = settings.rag_chat_model
EMBED_MODEL = settings.embed_model
