# syntax=docker/dockerfile:1
# =============================================================================
# Single-image deployment: builds the React SPA, then serves it from the FastAPI
# backend in ONE process on ONE port. The SPA, the /api routes, and the WebSocket
# all share the same origin (the frontend calls /api relative to its own host),
# so no reverse proxy / CORS config is needed.
#
# Build from the REPO ROOT (this file's directory):
#     docker build -t objective-content .
# Run (pass config as env — never bake secrets into the image):
#     docker run -p 8000:8000 --env-file backend/.env objective-content
# Then open http://localhost:8000  (API under http://localhost:8000/api).
# =============================================================================

# ---------- Stage 1: build the React frontend ----------
FROM node:22-slim AS frontend
WORKDIR /frontend

# Install deps first so this layer is cached unless the manifest/lockfile changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

# Build the production bundle → /frontend/dist. The app talks to "/api" relative
# to its own origin and derives the WebSocket URL from window.location, so there
# is no build-time API URL to inject.
COPY frontend/ ./
RUN npm run build

# ---------- Stage 2: backend + bundled frontend ----------
FROM python:3.12-slim

# psycopg[binary] ships its own libpq, so no system postgres client is needed.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FRONTEND_DIST_DIR=/app/frontend_dist

WORKDIR /app

# Python deps first for better layer caching. The BuildKit cache mount persists
# pip's wheels across builds; --timeout/--retries ride out slow/flaky PyPI links.
COPY backend/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 10 -r requirements.txt

# Backend app + portal package + alembic config/migrations.
COPY backend/app ./app
COPY backend/portal ./portal
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini

# Built SPA — served by FastAPI via the StaticFiles mount in app/main.py
# (FRONTEND_DIST_DIR points here).
COPY --from=frontend /frontend/dist ./frontend_dist

EXPOSE 8000

# Apply DB migrations, then serve the API + SPA on one port. Point DATABASE_URL at
# your managed Postgres (with the pgvector extension) via the runtime environment.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
