"""
app/mcq_pipeline/
-----------------
The MCQ-authoring pipeline (vendored from the Objective Content Automation
``Workflow`` project) wired into this backend.

The agent/graph/prompt logic is vendored largely verbatim; only the "plumbing"
is replaced so it runs against THIS app:
- `config`      → app settings (OpenRouter + model ids)
- `rag_api`     → scoped pgvector retrieval (`app.services.rag_search`) via `scope`
- `prompt_store`→ DB-backed, editable prompts (code constants are the seed/fallback)
- `progress`    → structured live stage board written onto the SyncJob row
- `tracing`     → LangSmith full-coverage tracing

Entry point: `runner.run_mcq_pipeline(...)`.
"""
