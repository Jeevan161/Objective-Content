FROM python:3.12-slim

# psycopg[binary] ships its own libpq, so no system postgres client is needed.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first for better layer caching. The BuildKit cache mount persists
# pip's downloaded wheels ACROSS builds, so changing requirements.txt re-resolves
# but reuses what's already downloaded (and a retry after a network blip resumes
# instead of starting over). --timeout/--retries ride out slow/flaky PyPI links.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 10 -r requirements.txt

# App + portal package + alembic config/migrations.
COPY app ./app
COPY portal ./portal
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts

EXPOSE 8000

# Run migrations, then serve. (Compose waits for Postgres via depends_on healthcheck.)
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
