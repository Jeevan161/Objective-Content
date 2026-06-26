import { useEffect, useState } from 'react'
import { ArrowLeft, ListChecks, BookOpen, Layers, FileText, Sparkles, AlertTriangle, Database } from 'lucide-react'
import { getCourse, generateMcq, resumeMcq, listMcqRuns, getMcqRun, mcqJobWsUrl, updateCourseSettings } from '../api'
import { EmptyState, Spinner } from './ui'
import { useToast } from './Toast'
import { useAuth } from '../auth/AuthContext'
import McqProgress from './McqProgress'
import McqResults from './McqResults'
import McqReviewGate from './McqReviewGate'
import McqScopeModal from './McqScopeModal'
import Select from './Select'

const TERMINAL = ['SUCCESS', 'FAILURE']

// A unit counts as a "session" if it's an explicit SESSION container or a
// standalone learning set — matching how the Courses view tags sessions.
function isSession(unit) {
  return unit.kind === 'SESSION' || (unit.kind === 'SINGLE' && unit.parts[0]?.unit_type === 'LEARNING_SET')
}

// A session's identifier for the pipeline = its Reading Material part's portal
// unit_id (the backend resolves the whole session from it).
function sessionUnitId(unit) {
  const rm = (unit.parts || []).find((p) => p.label === 'Reading Material' && p.unit_id)
  return rm?.unit_id || ''
}

// MCQ generation: pick course → topic → session, then run the LangGraph pipeline
// (live progress) and render the generated questions + LangSmith trace.
function McqGenerationPage({ courses, onBack, onTrackJob }) {
  const toast = useToast()
  const { user } = useAuth()
  const [courseId, setCourseId] = useState('')
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(false)
  const [topicId, setTopicId] = useState('')
  const [sessionId, setSessionId] = useState('') // reading-material part unit_id
  const [job, setJob] = useState(null)
  const [run, setRun] = useState(null)
  const [scopeOpen, setScopeOpen] = useState(false)
  const [hitl, setHitl] = useState(false)
  const [budget, setBudget] = useState('') // '' = default ceiling (20)
  const [runParams, setRunParams] = useState(null) // {prereqUnitIds, questionBudget} of the active run, for resume
  const [resuming, setResuming] = useState(false)
  const [savingDomain, setSavingDomain] = useState(false)

  // Load the full hierarchy when a course is chosen; reset downstream picks.
  // NOTE: deps are [courseId] ONLY. `toast`'s identity changes on every toast
  // add/dismiss; including it here re-ran this effect on every toast and wiped
  // the topic/session selections (and, in turn, the in-flight job) mid-run.
  /* eslint-disable react-hooks/set-state-in-effect -- sync UI with the course fetch */
  useEffect(() => {
    if (!courseId) {
      setDetail(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setDetail(null)
    setTopicId('')
    setSessionId('')
    getCourse(courseId)
      .then((d) => {
        if (!cancelled) setDetail(d)
      })
      .catch((e) => {
        if (!cancelled) toast.push({ kind: 'error', title: 'Could not load course', message: e.message })
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId])
  /* eslint-enable react-hooks/set-state-in-effect */

  const allCourses = courses || []
  const topics = detail?.topics || []
  const selectedTopic = topics.find((t) => t.topic_id === topicId) || null
  // Only sessions that actually have a reading-material part are selectable.
  const sessions = (selectedTopic?.units || []).filter((u) => isSession(u) && sessionUnitId(u))
  const selectedSession = sessions.find((u) => sessionUnitId(u) === sessionId) || null
  const ready = Boolean(courseId && topicId && sessionId)

  // MCQ generation needs the session's reading material extracted — ingestion is
  // NOT required. Content presence is what gates the action.
  const readingParts = (selectedSession?.parts || []).filter((p) => p.label === 'Reading Material')
  const sessionHasContent = readingParts.some((p) => p.has_content)
  const paused = job?.status === 'AWAITING_REVIEW' // waiting at a HITL gate
  const running = Boolean(job && !TERMINAL.includes(job.status) && !paused)
  // Only the user who added (synced) this course may generate for it. Admins bypass;
  // unowned (legacy) courses stay open. `detail.created_by` is null until loaded.
  const ownsCourse = !detail || user?.role === 'admin'
    || !detail.created_by || detail.created_by === user?.id
  const canGenerate = ready && sessionHasContent && ownsCourse && !running && !paused
  // Job is "live" (stream over a socket) while it's neither finished nor paused at a gate.
  const streamable = Boolean(job?.id) && !TERMINAL.includes(job?.status) && !paused

  // Load the latest existing run for a freshly selected session.
  /* eslint-disable react-hooks/set-state-in-effect -- reset + async fetch on selection change */
  useEffect(() => {
    setRun(null)
    setJob(null)
    if (!courseId || !sessionId) return
    let cancelled = false
    listMcqRuns(courseId, sessionId)
      .then((runs) => {
        if (cancelled || !runs || !runs[0]) return null
        return getMcqRun(runs[0].id).then((r) => {
          if (!cancelled) setRun(r)
        })
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [courseId, sessionId])
  /* eslint-enable react-hooks/set-state-in-effect */

  // Live job progress over a WebSocket (replaces ~1s polling). The server pushes the serialized
  // job on every change and closes when it settles (terminal / awaiting review), so this re-opens
  // automatically after a HITL resume (status flips back to RUNNING). Unexpected drops reconnect.
  useEffect(() => {
    if (!streamable) return
    const url = mcqJobWsUrl(job.id)
    let ws = null
    let stopped = false
    let retry = null
    const open = () => {
      ws = new WebSocket(url)
      ws.onmessage = async (e) => {
        let msg
        try {
          msg = JSON.parse(e.data)
        } catch {
          return
        }
        if (msg.type !== 'job') return // keepalive ping / non-job frame
        const updated = msg.data
        setJob(updated)
        if (updated.status === 'SUCCESS') {
          stopped = true
          ws.close()
          const runs = await listMcqRuns(courseId, sessionId)
          if (runs && runs[0]) setRun(await getMcqRun(runs[0].id))
        } else if (TERMINAL.includes(updated.status) || updated.status === 'AWAITING_REVIEW') {
          stopped = true
          ws.close()
        }
        // SUCCESS/FAILURE toasts are handled by the global Activity poller.
      }
      ws.onclose = () => {
        if (!stopped) retry = setTimeout(open, 1500) // unexpected drop → reconnect
      }
    }
    open()
    return () => {
      stopped = true
      if (retry) clearTimeout(retry)
      if (ws) ws.close()
    }
  }, [job?.id, streamable, courseId, sessionId])

  // Course-level question DOMAIN ('' = general, 'SQL'). Persisted on the course, so it
  // applies to EVERY generation run of this course — not guessed per outcome.
  const questionDomain = detail?.question_domain || ''

  async function handleDomainChange(value) {
    if (!courseId || value === questionDomain) return
    setSavingDomain(true)
    try {
      const res = await updateCourseSettings(courseId, value)
      setDetail((d) => (d ? { ...d, question_domain: res.question_domain } : d))
      toast.push({
        kind: 'success',
        title: 'Course domain updated',
        message: res.question_domain === 'SQL'
          ? 'SQL question rules will now apply to every generation run of this course.'
          : 'Using general question rules for this course.',
      })
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not update domain', message: e.message })
    } finally {
      setSavingDomain(false)
    }
  }

  async function handleGenerate(prereqUnitIds) {
    setScopeOpen(false)
    setRun(null)
    const qb = budget.trim() === '' ? null : Math.max(1, parseInt(budget, 10) || 0) || null
    setRunParams({ prereqUnitIds, questionBudget: qb })
    try {
      const j = await generateMcq(courseId, topicId, sessionId, true, prereqUnitIds, {
        questionBudget: qb,
        hitl,
      })
      setJob(j)
      onTrackJob?.(j) // also surface it in the global Activity drawer
      toast.push({
        kind: 'info',
        title: 'MCQ generation started',
        message: hitl
          ? `Generating from “${selectedSession?.label}” — it will pause for your review at each gate.`
          : `Generating from “${selectedSession?.label}” — watch the live progress below.`,
      })
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not start MCQ generation', message: e.message })
    }
  }

  // Submit a human decision at a HITL gate; the backend resumes the paused run from its
  // checkpoint and returns the now-RUNNING job (the poll effect then takes over again).
  async function handleDecision(decision) {
    if (!job) return
    setResuming(true)
    try {
      const j = await resumeMcq(job.id, {
        ...decision,
        course_id: courseId,
        topic_id: topicId,
        unit_id: sessionId,
        prerequisite_unit_ids: runParams?.prereqUnitIds ?? null,
        question_budget: runParams?.questionBudget ?? null,
        review: true,
      })
      setJob(j)
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not submit review', message: e.message })
    } finally {
      setResuming(false)
    }
  }

  if (allCourses.length === 0) {
    return (
      <div className="mcq-page">
        <McqHeader onBack={onBack} />
        <EmptyState
          icon={ListChecks}
          title="No courses yet"
          hint="Add a course and extract its reading material first. Then come back here to generate MCQs from a session — ingestion isn't required."
        />
      </div>
    )
  }

  return (
    <div className="mcq-page">
      <McqHeader onBack={onBack} />

      <div className="mcq-setup">
        <div className="mcq-field">
          <label className="section-label">
            <BookOpen size={13} /> Course
          </label>
          <Select
            value={courseId}
            onChange={setCourseId}
            placeholder="Select a course…"
            options={allCourses.map((c) => ({ value: c.course_id, label: c.course_name || c.course_id }))}
          />
        </div>

        <div className="mcq-field">
          <label className="section-label">
            <Layers size={13} /> Topic
          </label>
          <Select
            value={topicId}
            disabled={!detail || loading}
            onChange={(v) => { setTopicId(v); setSessionId('') }}
            placeholder={loading ? 'Loading topics…' : 'Select a topic…'}
            options={topics.map((t) => ({ value: t.topic_id, label: t.topic_name || t.topic_id }))}
          />
        </div>

        <div className="mcq-field">
          <label className="section-label">
            <FileText size={13} /> Session
          </label>
          <Select
            value={sessionId}
            disabled={!selectedTopic}
            onChange={setSessionId}
            placeholder={
              !selectedTopic
                ? 'Select a topic first…'
                : sessions.length === 0
                  ? 'No sessions with reading material'
                  : 'Select a session…'
            }
            options={sessions.map((u) => ({ value: sessionUnitId(u), label: u.label }))}
          />
        </div>

        <div className="mcq-field">
          <label className="section-label">
            <Database size={13} /> Domain
          </label>
          <Select
            value={questionDomain}
            disabled={!detail || loading || savingDomain || !ownsCourse}
            onChange={handleDomainChange}
            dataTip="Course-level setting: activates domain-specific question rules (e.g. SQL) for every generation run of this course"
            options={[{ value: '', label: 'General' }, { value: 'SQL', label: 'SQL' }]}
          />
        </div>
      </div>

      {loading && (
        <div className="mcq-loading">
          <Spinner size={14} /> Loading course hierarchy…
        </div>
      )}

      {ready && (
        <div className="mcq-summary">
          <div className="mcq-summary-path">
            <strong>{detail?.course_name || courseId}</strong>
            <span>›</span>
            <span>{selectedTopic?.topic_name || topicId}</span>
            <span>›</span>
            <span>{selectedSession?.label}</span>
            {!sessionHasContent && (
              <span className="mcq-no-content">
                <AlertTriangle size={12} /> No reading material content — extract this session first
              </span>
            )}
          </div>
          <div className="mcq-options">
            <label className="mcq-opt">
              <span>Questions</span>
              <input
                className="input mcq-opt-budget"
                type="number"
                min="5"
                step="5"
                placeholder="20"
                value={budget}
                disabled={running || paused}
                onChange={(e) => setBudget(e.target.value)}
                data-tip="Target number of questions (default 20; stepped down by 5s if the material is thin)"
              />
            </label>
            <label className="mcq-opt mcq-opt-check" data-tip="Pause for human approval of the division and the final outcomes">
              <input
                type="checkbox"
                checked={hitl}
                disabled={running || paused}
                onChange={(e) => setHitl(e.target.checked)}
              />
              <span>Human review</span>
            </label>
          </div>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!canGenerate}
            data-tip={
              !ownsCourse
                ? 'Only the user who added this course can generate MCQs for it'
                : sessionHasContent
                  ? undefined
                  : 'This session has no extracted reading material content — run Extract on the course first'
            }
            onClick={() => setScopeOpen(true)}
          >
            {running ? <Spinner size={14} /> : <Sparkles size={15} />}
            {running ? 'Generating…' : run ? 'Re-generate MCQs' : 'Generate MCQs'}
          </button>
          {!ownsCourse && (
            <p className="mcq-owner-note">
              <AlertTriangle size={13} /> Only the user who added this course can generate MCQs for it.
            </p>
          )}
        </div>
      )}

      {scopeOpen && (
        <McqScopeModal
          course={detail || { course_id: courseId, course_name: detail?.course_name }}
          prerequisites={detail?.prerequisites || []}
          currentUnitId={sessionId}
          onClose={() => setScopeOpen(false)}
          onConfirm={(prereqUnitIds) => handleGenerate(prereqUnitIds)}
        />
      )}

      {running && <McqProgress progress={job.progress} />}

      {paused && (
        <McqReviewGate review={job.progress?.review} busy={resuming} onDecide={handleDecision} />
      )}

      {job?.status === 'FAILURE' && (
        <div className="mcq-fail">
          <AlertTriangle size={14} /> {job.error || 'Generation failed.'}
        </div>
      )}

      {run && !running && !paused && <McqResults key={run.id} run={run} courseId={courseId} unitId={sessionId} />}
    </div>
  )
}

function McqHeader({ onBack }) {
  return (
    <header className="topbar">
      <div className="mcq-head-left">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onBack} data-tip="Back to Generation Studio">
          <ArrowLeft size={14} /> Studio
        </button>
        <div>
          <h1>MCQ Generation</h1>
          <p className="topbar-sub">
            Pick a course, topic and session to generate MCQ practice.
          </p>
        </div>
      </div>
    </header>
  )
}

export default McqGenerationPage
