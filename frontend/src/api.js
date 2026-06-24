// Client for the FastAPI backend. Dev requests go through Vite's proxy
// (see vite.config.js) to the backend on :8000.
const BASE = '/api'

const TOKEN_KEY = 'auth_token'
export const getToken = () => localStorage.getItem(TOKEN_KEY)
export const setToken = (t) => (t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY))

async function request(path, options = {}) {
  const token = getToken()
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  })
  if (res.status === 401) {
    // Token missing/expired — drop it and let the app fall back to the login gate.
    setToken(null)
    window.dispatchEvent(new Event('auth:logout'))
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const body = await res.json()
      detail = body.detail || JSON.stringify(body)
    } catch {
      // non-JSON error body; keep the default message
    }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return res.status === 204 ? null : res.json()
}

// Step 1: get the versions for a course id (for the popup).
export const fetchVersions = (courseId, environment) =>
  request('/courses/versions/', {
    method: 'POST',
    body: JSON.stringify({ course_id: courseId, environment }),
  })

// Look a course id up in BOTH environments at once → { course_id, environments: { PROD, BETA } },
// each { present, versions, course_name, error }. Powers the add-course availability view.
export const lookupCourse = (courseId) =>
  request('/courses/lookup/', {
    method: 'POST',
    body: JSON.stringify({ course_id: courseId }),
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
// The reviewer is taken from the authenticated user server-side — never sent from the client.
// Regenerate ONE question with reviewer feedback injected; returns the new question.
export const regenerateMcqQuestion = (runId, outcome, feedback, tags = []) =>
  request(`/courses/mcq/runs/${runId}/questions/${encodeURIComponent(outcome)}/regenerate/`, {
    method: 'POST',
    body: JSON.stringify({ feedback, tags }),
  })

// Record a non-regenerating review action (e.g. accept) on a question.
export const submitMcqFeedback = (runId, outcome, { action, tags = [], comment = '' }) =>
  request(`/courses/mcq/runs/${runId}/questions/${encodeURIComponent(outcome)}/feedback/`, {
    method: 'POST',
    body: JSON.stringify({ action, tags, comment }),
  })

// Set a human approval decision (approved | rejected | pending) on one question; the
// approved count gates loading. Returns { approval, approved_count, eligible_count }.
export const setMcqQuestionApproval = (runId, outcome, approval) =>
  request(`/courses/mcq/runs/${runId}/questions/${encodeURIComponent(outcome)}/approval/`, {
    method: 'POST',
    body: JSON.stringify({ approval }),
  })

// Exclude a question from export/load (or include it again). It stays in the list,
// shaded out, but drops from the approval tally and is never loaded.
export const setMcqQuestionExclusion = (runId, outcome, excluded) =>
  request(`/courses/mcq/runs/${runId}/questions/${encodeURIComponent(outcome)}/exclude/`, {
    method: 'POST',
    body: JSON.stringify({ excluded }),
  })

// Approve the whole run (review complete).
export const approveMcqRun = (runId) =>
  request(`/courses/mcq/runs/${runId}/approve/`, {
    method: 'POST',
    body: JSON.stringify({}),
  })

// Build the portal-format export ZIP for a run and upload it to the beta S3 bucket.
// `approvedOnly` exports just the approved subset (else every question must be approved).
// Returns { url, filename, counts, total, batch_id }.
export const exportMcqRunZip = (runId, approvedOnly = false) =>
  request(`/courses/mcq/runs/${runId}/export-beta/?approved_only=${approvedOnly}`, {
    method: 'POST',
  })

// Full beta-load pipeline: build+upload the ZIP, copy+fill the exam-config sheet,
// submit the load, poll it, and unlock. Can take up to a couple of minutes.
// Returns { status, message, sheet_url, resource_id, request_id, total, ... }.
export const prepareAndLoadMcqRun = (runId, fields) =>
  request(`/courses/mcq/runs/${runId}/prepare-and-load/`, {
    method: 'POST',
    body: JSON.stringify(fields),
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

// --- Auth ---
export const registerUser = (email, password, name = '') =>
  request('/auth/register', { method: 'POST', body: JSON.stringify({ email, password, name }) })

export const loginUser = (email, password) =>
  request('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) })

export const fetchMe = () => request('/auth/me')

// Per-connector API keys: list all connectors + whether the user has a key for each.
export const fetchMyKeys = () => request('/auth/me/keys')
export const setConnectorKey = (providerId, apiKey) =>
  request(`/auth/me/keys/${providerId}`, { method: 'PUT', body: JSON.stringify({ api_key: apiKey }) })
export const clearConnectorKey = (providerId) =>
  request(`/auth/me/keys/${providerId}`, { method: 'DELETE' })

// --- Admin ---
export const adminListUsers = () => request('/admin/users')
export const adminApproveUser = (id) => request(`/admin/users/${id}/approve`, { method: 'POST' })
export const adminDeactivateUser = (id) => request(`/admin/users/${id}/deactivate`, { method: 'POST' })
export const adminSetRole = (id, role) =>
  request(`/admin/users/${id}/role`, { method: 'POST', body: JSON.stringify({ role }) })
export const adminStats = () => request('/admin/stats')
export const adminLogs = (level = '', limit = 200) =>
  request(`/admin/logs?limit=${limit}${level ? `&level=${level}` : ''}`)
// All application-level feedback submissions (admin).
export const adminAppFeedback = (limit = 200) => request(`/admin/feedback?limit=${limit}`)
// All MCQ reviewer feedback actions (admin).
export const adminMcqFeedback = (limit = 300) => request(`/admin/mcq-feedback?limit=${limit}`)

// --- Application feedback (any signed-in user) ---
// rating: 1–5 (emoji), category, helpful (true/false/null), message.
export const submitAppFeedback = (payload) =>
  request('/feedback', { method: 'POST', body: JSON.stringify(payload) })

// Get reading material content for a unit (course + session/unit_id)
export const getUnitContent = (courseId, unitId) =>
  request(`/courses/${courseId}/units/${encodeURIComponent(unitId)}/content/`)
