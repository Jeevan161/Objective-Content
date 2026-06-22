"""
app/services/openrouter.py
--------------------------
Thin OpenRouter client (ported from the Workflow POC). OpenRouter is
OpenAI-compatible and exposes both /embeddings and /chat/completions, so one API
key serves embeddings (for indexing/queries) and chat (for the judge).

    embed_texts(texts)  -> list[list[float]]   L2-normalized, batched
    embed_query(text)   -> list[float]
    chat(messages)      -> str
"""

from __future__ import annotations

import time

import numpy as np
import requests

from app.core.config import settings

# LangSmith tracing decorator — wraps embed/chat so retrieval shows up nested in
# the MCQ pipeline trace. A no-op when langsmith isn't installed; langsmith itself
# only emits when LANGCHAIN_TRACING_V2 is enabled, so it's safe to always apply.
try:
    from langsmith import traceable
except Exception:  # noqa: BLE001
    def traceable(*dargs, **dkwargs):  # type: ignore[no-redef]
        def deco(fn):
            return fn
        return deco(dargs[0]) if dargs and callable(dargs[0]) else deco


def _headers() -> dict:
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (see .env).")
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    if settings.openrouter_site_name:
        headers["X-Title"] = settings.openrouter_site_name
    return headers


def _post(path: str, payload: dict, *, retries: int = 4, timeout: int = 120) -> dict:
    """POST to OpenRouter with simple exponential backoff on rate limits / 5xx."""
    url = f"{settings.openrouter_base_url}{path}"
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.post(url, headers=_headers(), json=payload, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, requests.HTTPError) as exc:
            last_exc = exc
            time.sleep(2 ** attempt)  # 1s, 2s, 4s, 8s …
    raise RuntimeError(f"OpenRouter request failed after {retries} attempts: {last_exc}")


@traceable(name="openrouter.embed", run_type="embedding")
def embed_texts(texts: list[str], *, batch_size: int = 64) -> list[list[float]]:
    """Embed many texts -> list of L2-normalized vectors (cosine == dot product)."""
    out: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        data = _post(
            "/embeddings",
            {
                "model": settings.embed_model,
                "input": batch,
                "dimensions": settings.embed_dimensions,
                "encoding_format": "float",
            },
        )
        items = sorted(data["data"], key=lambda d: d.get("index", 0))
        for item in items:
            vec = np.asarray(item["embedding"], dtype=np.float32)
            norm = float(np.linalg.norm(vec)) or 1.0
            out.append((vec / norm).tolist())
    return out


def embed_query(text: str) -> list[float]:
    """Embed one query string -> a single normalized vector."""
    return embed_texts([text])[0]


@traceable(name="openrouter.chat", run_type="llm")
def chat(messages: list[dict], *, temperature: float = 0.2) -> str:
    """Send chat messages, return the assistant's text."""
    data = _post(
        "/chat/completions",
        {"model": settings.rag_chat_model, "messages": messages, "temperature": temperature},
    )
    return data["choices"][0]["message"]["content"] or ""
