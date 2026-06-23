// Client for the Django course API. Dev requests go through Vite's proxy
// (see vite.config.js) to the backend on :8000.
const BASE = '/api'

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const body = await res.json()
      detail = body.detail || JSON.stringify(body)
    } catch {
      // non-JSON error body; keep the default message
    }
    throw new Error(detail)
  }
  return res.status === 204 ? null : res.json()
}

// Step 1: get the versions for a course id (for the popup).
export const fetchVersions = (courseId, environment) =>
  request('/courses/versions/', {
    method: 'POST',
    body: JSON.stringify({ course_id: courseId, environment }),
  })

// Step 2 / Sync: start a background fetch. Pass version fields to choose a
// specific version; omit them to reuse the course's stored version.
export const startSync = (payload) =>
  request('/courses/sync/', {
    method: 'POST',
    body: JSON.stringify(payload),
  })

export const getJob = (jobId) => request(`/courses/jobs/${jobId}/`)

// WebSocket URL for live job progress (replaces polling). Same `/api` prefix, so Vite proxies it
// (ws: true) to the backend in dev; in prod it rides the same host the page was served from.
export const mcqJobWsUrl = (jobId) => {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}${BASE}/courses/mcq/jobs/${jobId}/ws`
}

export const getCourses = () => request('/courses/')
export const getCourse = (courseId) => request(`/courses/${courseId}/`)

// Which environments a course + its prerequisites span (one token needed each).
export const getExtractInfo = (courseId) =>
  request(`/courses/${courseId}/extract-info/`)

// Extract reading-material content for a course + its prerequisites. `tokens` is
// a {ENV: bearerToken} map, used server-side for this run only and never stored.
// Extract reading-material content. `unitIds` (optional) limits the run to those
// learning-set parts (per-unit "Sync content") instead of the whole course.
export const extractContent = (courseId, tokens, unitIds) =>
  request('/courses/extract/', {
    method: 'POST',
    body: JSON.stringify({ course_id: courseId, tokens, unit_ids: unitIds }),
  })

// Ingest the selected learning resources (reading-material unit ids) into the
// RAG index. Covers the course and its prerequisites. Returns a background job
// (job_type RAG) that the UI polls for progress, just like sync/extract.
export const ingestContent = (courseId, unitIds) =>
  request('/courses/build-rag/', {
    method: 'POST',
    body: JSON.stringify({ course_id: courseId, unit_ids: unitIds }),
  })

// Ask a question against a course's RAG index (its prerequisites are included
// server-side). Returns { answer, sources, query, course_ids }.
export const ragAnswer = (courseIds, query) =>
  request('/courses/rag/answer/', {
    method: 'POST',
    body: JSON.stringify({ course_ids: courseIds, query }),
  })

// --- MCQ generation pipeline (LangGraph) ---
// Start a run for a session. `unitId` is a reading-material part's portal unit_id
// within the session. Returns a background job (job_type MCQ) the UI polls.
export const generateMcq = (
  courseId,
  topicId,
  unitId,
  review = true,
  prerequisiteUnitIds = null,
  { questionBudget = null, hitl = false } = {},
) =>
  request('/courses/mcq/generate/', {
    method: 'POST',
    body: JSON.stringify({
      course_id: courseId,
      topic_id: topicId,
      unit_id: unitId,
      review,
      prerequisite_unit_ids: prerequisiteUnitIds,
      question_budget: questionBudget,
      hitl,
    }),
  })

// Resume a HITL-paused run after a human decision at a gate. `decision` carries the action
// (approve/reject), any rejected LO ids + note, and the run context the backend needs to rebuild
// the run-scoped RAG adapter (course/topic/unit/prereqs/budget). The job_id is the checkpoint key.
export const resumeMcq = (jobId, decision) =>
  request(`/courses/mcq/jobs/${jobId}/resume/`, {
    method: 'POST',
    body: JSON.stringify(decision),
  })

// Recent runs (summaries), optionally scoped to a course/session.
export const listMcqRuns = (courseId, unitId) =>
  request(
    `/courses/mcq/runs/?course_id=${encodeURIComponent(courseId)}&unit_id=${encodeURIComponent(unitId)}`,
  )

// Full result of a single run.
export const getMcqRun = (runId) => request(`/courses/mcq/runs/${runId}/`)

// Node-by-node execution trace for a run (our own tracing), by the run's job id.
export const getMcqTrace = (jobId) => request(`/courses/mcq/jobs/${jobId}/trace/`)

// All recent runs (summaries), newest first, across every course — for the Runs page.
export const listAllMcqRuns = (limit = 50) => request(`/courses/mcq/runs/?limit=${limit}`)

// --- Human-in-the-loop review (Gate B) ---
// Regenerate ONE question with reviewer feedback injected; returns the new question.
export const regenerateMcqQuestion = (runId, outcome, feedback, tags = [], reviewer = '') =>
  request(`/courses/mcq/runs/${runId}/questions/${encodeURIComponent(outcome)}/regenerate/`, {
    method: 'POST',
    body: JSON.stringify({ feedback, tags, reviewer }),
  })

// Record a non-regenerating review action (e.g. accept) on a question.
export const submitMcqFeedback = (runId, outcome, { action, tags = [], comment = '', reviewer = '' }) =>
  request(`/courses/mcq/runs/${runId}/questions/${encodeURIComponent(outcome)}/feedback/`, {
    method: 'POST',
    body: JSON.stringify({ action, tags, comment, reviewer }),
  })

// Approve the whole run (review complete).
export const approveMcqRun = (runId, reviewer = '') =>
  request(`/courses/mcq/runs/${runId}/approve/`, {
    method: 'POST',
    body: JSON.stringify({ reviewer }),
  })

// --- MCQ pipeline & prompts (admin) ---
// The ordered pipeline stages, each with the prompts that drive it.
export const getMcqPipeline = () => request('/mcq/pipeline/')

// Save a new active version of a prompt (the pipeline picks it up immediately).
export const updateMcqPrompt = (key, content, description) =>
  request(`/mcq/prompts/${encodeURIComponent(key)}/`, {
    method: 'PUT',
    body: JSON.stringify({ content, description }),
  })

// Reset a prompt back to its code default.
export const resetMcqPrompt = (key) =>
  request(`/mcq/prompts/${encodeURIComponent(key)}/reset/`, { method: 'POST' })

// --- LLM providers / connectors (admin) ---
// All connectors (keys are returned MASKED, never in full). Exactly one is active.
export const getLlmProviders = () => request('/llm/providers/')

// Create or update a connector by name. Omit `api_key` (or send "") to KEEP the
// existing stored key — the UI never receives the real key, only a masked tail.
export const saveLlmProvider = (payload) =>
  request('/llm/providers/', {
    method: 'POST',
    body: JSON.stringify(payload),
  })

// Make this connector the single active one (drives every pipeline LLM call).
export const activateLlmProvider = (name) =>
  request(`/llm/providers/${encodeURIComponent(name)}/activate/`, { method: 'POST' })

// Live connectivity probe: build a model from this connector and make a tiny call.
export const testLlmProvider = (name) =>
  request(`/llm/providers/${encodeURIComponent(name)}/test/`, { method: 'POST' })

// Remove a connector.
export const deleteLlmProvider = (name) =>
  request(`/llm/providers/${encodeURIComponent(name)}/`, { method: 'DELETE' })
