"""
app/core/config.py
------------------
Process-level settings, loaded from environment / .env (pydantic-settings).

Replaces Django's settings.py. Note the portal layer (``portal/``) reads its own
credentials directly from os.environ (PORTAL_PROD_*, PORTAL_BETA_*, …) and is left
untouched — those vars only need to be present in the environment, not re-declared
here. This file holds the DB URL, CORS origins, and the RAG/OpenRouter knobs.
"""

from __future__ import annotations

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env into os.environ so both this config AND the portal layer (which uses
# os.environ.get directly) resolve their values from the same place.
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database (Postgres + pgvector). Port 5545 = the docker-compose service here,
    # kept distinct from the other project's Postgres on 5544.
    database_url: str = (
        "postgresql+psycopg://postgres:postgres@localhost:5545/objective_content"
    )

    # CORS — the Vite dev server.
    frontend_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # --- RAG: OpenRouter (embeddings + chat for the judge) ---
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    embed_model: str = "text-embedding-3-large"
    embed_dimensions: int = 1536
    rag_chat_model: str = "openai/gpt-4o-mini"
    openrouter_site_name: str = "Objective Content RAG"

    # Secret used to derive the Fernet key that encrypts stored LLM provider API keys.
    # Falls back to the DB URL when unset (works out of the box); set for real isolation.
    llm_secret_key: str | None = None

    # Internal OpenAI-compatible proxy, seeded into the inactive 'proxy' connector preset.
    # Base URL + key come from env (PROXY_BASE_URL / PROXY_API_KEY); the key falls back to
    # OPENAI_API_KEY (the name the reference automation project uses).
    proxy_base_url: str | None = None
    proxy_api_key: str | None = None
    openai_api_key: str | None = None

    # --- MCQ pipeline (LangGraph) ---
    # Strong model drives the LO agents + question generation/review.
    mcq_agent_model: str = "openai/gpt-5o"
    # Per-role model overrides for the two question agents. These override ONLY the
    # model id on the ACTIVE connector (currently OpenRouter), reusing its key / base_url
    # / proxy metadata — so they are OpenRouter slugs. Empty string -> use the connector's
    # own model. Generation runs on Sonnet 4.6, review on GPT-4o.
    # NB: OpenRouter slug uses a dot (claude-sonnet-4.6); the native Anthropic id is
    # claude-sonnet-4-6. We route through OpenRouter, so use the dotted slug.
    mcq_generation_model: str = "anthropic/claude-sonnet-4.6"
    mcq_review_model: str = "openai/gpt-4o"
    # Production concurrency knobs (multi-user safety):
    #  - cap simultaneous pipeline jobs so threads/DB connections/LLM rate don't blow up.
    #  - LangGraph checkpointer backend: "postgres" (durable/resumable), "memory", or "none".
    mcq_max_concurrent_jobs: int = 4
    mcq_checkpointer: str = "postgres"

    # --- FIB execution verification (run the filled program, match output) ---
    # FIB_CODING is graded by EXECUTION (fill blank -> run on input -> compare stdout),
    # so we verify generated FIBs the same way. Sandboxed local subprocess.
    fib_verify: bool = True
    fib_exec_timeout: int = 8           # wall-clock seconds per run
    fib_exec_cpu_seconds: int = 10      # RLIMIT_CPU

    # --- SQLAlchemy connection pool (sized for the threaded job runner) ---
    db_pool_size: int = 20
    db_max_overflow: int = 40
    db_pool_timeout: int = 30

    # --- LangSmith tracing (optional; tracing is a no-op when the key is unset) ---
    langchain_tracing_v2: bool = False
    langchain_api_key: str | None = None
    langchain_project: str = "objective-content-mcq"
    langchain_endpoint: str = "https://api.smith.langchain.com"

    app_env: str = "local"

    # --- Beta content-loading S3 export (temporary upload target) ---
    # The beta admin hands out short-lived AWS creds scraped from its upload page;
    # we log in with these and upload the export ZIP to the shared media bucket.
    beta_admin_base_url: str | None = None       # env: BETA_ADMIN_BASE_URL
    beta_admin_username: str | None = None
    beta_admin_password: str | None = None
    beta_s3_bucket: str = "nkb-backend-ccbp-media-static"
    beta_s3_region: str = "ap-south-1"
    beta_s3_upload_folder: str = "ccbp_beta/media/content_loading/uploads/"

    # --- Exam-config Google Sheet (copy template → fill Form tab → submit load) ---
    # A service-account JSON (gitignored file) drives the Drive copy + Sheets edit.
    # The template is a formula-driven workbook whose `Form` tab is the single source
    # of truth; we copy it and write only the Form's input cells (services/beta_sheet).
    google_sa_credentials_file: str = "google_sa_credentials.json"
    mcq_template_spreadsheet_id: str = "1Grps-VAstCkmquzC_oDOAMnpYWrFNsXUXDf5RrzCFm4"
    # Comma-separated emails the prepared sheet is shared with (Editor), in addition
    # to the requester. Defaults to the downstream learning-resource service account
    # plus the reviewer account so the prepared sheets can be opened/reviewed.
    mcq_sheet_share_emails: str = (
        "learningresource@nkblearningbackend.iam.gserviceaccount.com,"
        "jeevansravanth.parisa@nxtwave.co.in"
    )
    # Max seconds to poll the content-loading task before giving up.
    beta_load_poll_timeout: int = 180

    # --- Auth (JWT bearer) ---
    # Set JWT_SECRET in any real deployment; the default is for local only.
    jwt_secret: str = "dev-only-change-me-please-set-JWT_SECRET-in-prod"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7   # 7 days

    # App-level logging level for the stdlib logger (stdout → container logs).
    log_level: str = "INFO"


settings = Settings()
