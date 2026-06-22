# Course Fetcher — React + Django

Fetch a course's details (topics + units) from the admin portal, choose a
version via a popup, persist everything to the database, and re-sync on demand.

## Layout

```
backend/                 Django + DRF API
  portal/                Reusable portal-fetching package (modular)
    client.py            PortalClient (login + authenticated GET)
    constants.py         URLs + credentials (env-overridable)
    fetch.py             Parsing + build_course_data(selected_version, progress)
  courses/               App: models, serializers, views, services, tasks
  config/                Project settings + urls
frontend/                React (Vite)
  src/api.js             API client
  src/App.jsx            Add Course flow + version popup + job polling
  src/components/        Modal, VersionPopup, CourseCard
```

## How it works

1. **Add Course** → enter a course ID → backend fetches the course's versions
   (`POST /api/courses/versions/`, quick/synchronous).
2. A **version popup** lists the versions; pick one (or, if none exist, fetch via
   resource links).
3. That starts a **background sync job** (`POST /api/courses/sync/`, returns a
   job id). The fetch runs in a worker thread and reports progress on the job row.
4. The frontend **polls** `GET /api/courses/jobs/<id>/` every 2s until the job
   finishes; on success the course (topics + units) is saved to the DB.
5. Each saved course has a **Sync** button that re-runs the fetch, reusing the
   course's previously selected version.

## Run

Backend (port 8000 was in use on this machine — use any free port):

```bash
cd backend
venv/bin/python manage.py migrate          # already applied
venv/bin/python manage.py runserver 8011
```

Frontend (the Vite proxy forwards `/api` → `http://localhost:8000`; update
`frontend/vite.config.js` if you run the backend on a different port):

```bash
cd frontend
npm run dev
```

## Environments (PROD / BETA)

Both portals are supported and chosen in the **Add Course** dialog (a PROD/BETA
toggle). The selection is sent with the request, stored on the course, and
reused by its **Sync** button. The version popup, job banner, and course cards
all show which environment is in use.

Definitions live in `backend/portal/constants.py` (`ENVIRONMENTS`) and can be
overridden via environment variables:

| Env  | Base URL var            | Username var             | Password var             |
|------|-------------------------|--------------------------|--------------------------|
| PROD | `PORTAL_PROD_BASE_URL`  | `PORTAL_PROD_USERNAME`   | `PORTAL_PROD_PASSWORD`   |
| BETA | `PORTAL_BETA_BASE_URL`  | `PORTAL_BETA_USERNAME`   | `PORTAL_BETA_PASSWORD`   |

`PORTAL_LEARNING_COURSE_URL` (shared, link-building only) can also be overridden.

## Notes

- The fetch/parse logic was ported from `Portal_Data/fetch_course_topics_units.py`
  and made modular; the interactive CLI version prompt is replaced by the popup.
- Background jobs use a daemon thread (no Celery/Redis). Good for a single-process
  dev server; for production, swap `courses/tasks.py` for a real task queue.
- Within a job, the portal requests run **concurrently** (a thread pool issues
  topic/unit fetches in parallel instead of one-by-one), so large courses sync
  much faster. Tune the parallelism with `PORTAL_FETCH_CONCURRENCY` (default 8).
  Progress is reported only from the job thread, so the result order is stable.
