"""
app/api/mcq_prompts.py
----------------------
Read/edit the MCQ pipeline's prompts and view how they map onto the pipeline
stages. Powers the "MCQ Pipeline" admin UI.

  GET  /api/mcq/pipeline/         stages (in order) + the prompts driving each
  GET  /api/mcq/prompts/          flat list of every prompt (current + default)
  PUT  /api/mcq/prompts/{key}/    save a new active version of a prompt
  POST /api/mcq/prompts/{key}/reset/   reset a prompt to its code default

Importing the pipeline package below triggers every `register()` call so all
prompt keys are known even if the pipeline hasn't run yet this process.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import app.mcq_pipeline.graph  # noqa: F401 — import triggers all MCQ prompt register() calls
# The Classroom Quiz prompts (cq.reading_material, cq.variants.*) are registered in m00/m10, which
# the MCQ graph never imports (CQ runs at the runner level). Import them here too so they register
# and show up under the Classroom Quiz pipeline family.
import app.mcq_pipeline.nodes.m00_generate_reading_material  # noqa: F401
import app.mcq_pipeline.nodes.m10_generate_variants  # noqa: F401
from app.mcq_pipeline.prompts import catalog as prompt_catalog, store as prompt_store

router = APIRouter(prefix="/api/mcq")


class PromptUpdate(BaseModel):
    content: str
    description: str | None = None


@router.get("/pipeline/")
def get_pipeline(family: str = "mcq") -> dict:
    """The pipeline stages in order for `family` ('mcq' | 'cq'), each with the full prompt objects
    that drive it. `unassigned` holds any prompt that maps to no stage in this family (never
    hidden). The Classroom Quiz family reuses the MCQ stages, wrapped by reading_material (m00)
    and generate_variants (m10)."""
    family = family if family in ("mcq", "cq") else "mcq"
    prompts = {p["key"]: p for p in prompt_store.list_prompts()}
    stages, unassigned = prompt_catalog.build_catalog(list(prompts.keys()), family=family)
    enriched = [
        {**stage, "prompts": [prompts[k] for k in stage["prompt_keys"] if k in prompts]}
        for stage in stages
    ]
    shown = [p for stage in enriched for p in stage["prompts"]]
    overridden = sum(1 for p in shown if p["overridden"])
    return {
        "family": family,
        "stages": enriched,
        "unassigned": [prompts[k] for k in unassigned if k in prompts],
        "counts": {"stages": len(enriched), "prompts": len(shown), "overridden": overridden},
    }


@router.get("/prompts/")
def list_prompts() -> list[dict]:
    return prompt_store.list_prompts()


@router.put("/prompts/{key}/")
def update_prompt(key: str, body: PromptUpdate) -> dict:
    if not prompt_store.is_registered(key):
        raise HTTPException(status_code=404, detail="Unknown prompt key.")
    if prompt_store.is_informational(key):
        raise HTTPException(
            status_code=400,
            detail="This is read-only reference documentation for a deterministic "
                   "stage — its behavior is fixed in code and cannot be edited here.")
    if not (body.content or "").strip():
        raise HTTPException(status_code=400, detail="Prompt content cannot be empty.")
    return prompt_store.set_prompt(key, body.content, description=body.description)


@router.post("/prompts/{key}/reset/")
def reset_prompt(key: str) -> dict:
    if prompt_store.is_informational(key):
        raise HTTPException(
            status_code=400,
            detail="Read-only reference documentation cannot be reset/edited.")
    try:
        return prompt_store.reset_prompt(key)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown prompt key.")
