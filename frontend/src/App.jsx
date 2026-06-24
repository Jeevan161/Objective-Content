import { useCallback, useEffect, useMemo, useState } from 'react'
import { Plus, Search, LayoutGrid, ChevronsUpDown, ChevronsDownUp } from 'lucide-react'
import { startSync, getJob, getCourses, extractContent, ingestContent } from './api'
import Sidebar from './components/Sidebar'
import MobileBar from './components/MobileBar'
import JobsDrawer from './components/JobsDrawer'
import AddCourseWizard from './components/AddCourseWizard'
import TokenModal from './components/TokenModal'
import IngestModal from './components/IngestModal'
import ChatPage from './components/ChatPage'
import GenerationStudio from './components/GenerationStudio'
import McqGenerationPage from './components/McqGenerationPage'
import McqRunsPage from './components/McqRunsPage'
import ReviewQueuePage from './components/ReviewQueuePage'
import PipelinePage from './components/PipelinePage'
import LLMProvidersPage from './components/LLMProvidersPage'
import CourseCard from './components/CourseCard'
import AdminDashboard from './components/AdminDashboard'
import AccountModal from './components/AccountModal'
import FeedbackForm from './components/FeedbackForm'
import AuthGate from './components/AuthGate'
import { AuthProvider, useAuth } from './auth/AuthContext'
import { ToastProvider, useToast } from './components/Toast'
import { EmptyState, Skeleton, Spinner } from './components/ui'

const TERMINAL = ['SUCCESS', 'FAILURE']

function useTheme() {
  const [theme, setTheme] = useState(() => localStorage.getItem('oc-theme') || 'dark')
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('oc-theme', theme)
  }, [theme])
  return [theme, () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))]
}

// Desktop sidebar collapse (icon-only rail), persisted across sessions.
function useNavCollapsed() {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem('oc-nav-collapsed') === '1',
  )
  useEffect(() => {
    localStorage.setItem('oc-nav-collapsed', collapsed ? '1' : '0')
  }, [collapsed])
  return [collapsed, () => setCollapsed((c) => !c)]
}

// Animates a number from 0 to `target` with an ease-out curve.
function useCountUp(target, duration = 650) {
  const [value, setValue] = useState(0)
  useEffect(() => {
    let raf
    const start = performance.now()
    function tick(now) {
      const p = Math.min(1, (now - start) / duration)
      setValue(Math.round(target * (1 - Math.pow(1 - p, 3))))
      if (p < 1) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [target, duration])
  return value
}

function StatCard({ label, value, accent, onClick, active }) {
  const display = useCountUp(value)
  const Tag = onClick ? 'button' : 'div'
  return (
    <Tag
      className={`stat-card ${onClick ? 'clickable' : ''} ${active ? 'active' : ''}`}
      onClick={onClick}
      style={accent ? { '--stat-accent': accent } : undefined}
      {...(onClick ? { type: 'button', 'data-tip': active ? 'Clear filter' : `Show only ${label} courses` } : {})}
    >
      <div className="stat-value" style={accent ? { color: accent } : undefined}>
        {display}
      </div>
      <div className="stat-label">{label}</div>
    </Tag>
  )
}

function Workspace() {
  const toast = useToast()
  const { user } = useAuth()
  const [theme, toggleTheme] = useTheme()
  const [navCollapsed, toggleNavCollapsed] = useNavCollapsed()
  const [page, setPage] = useState('courses')
  const [accountOpen, setAccountOpen] = useState(false)
  const [feedbackOpen, setFeedbackOpen] = useState(false)

  const [courses, setCourses] = useState(null) // null = first load
  const [query, setQuery] = useState('')
  const [envFilter, setEnvFilter] = useState(null) // null | 'PROD' | 'BETA'

  // Add-course wizard. `prereqFor` carries the parent course id when adding a
  // prerequisite (null = plain "Add Course").
  const [wizard, setWizard] = useState(null) // null | { prereqFor, defaultEnv }

  // Extraction token modal (course or null).
  const [extractFor, setExtractFor] = useState(null)
  // Ingest selection modal (course or null).
  const [ingestFor, setIngestFor] = useState(null)

  // Background jobs being polled (supports several concurrent ones).
  const [jobs, setJobs] = useState([])
  const [drawerOpen, setDrawerOpen] = useState(false)
  // Mobile navigation drawer (the off-canvas sidebar).
  const [navOpen, setNavOpen] = useState(false)
  // Bulk expand/collapse signal broadcast to every course card.
  const [allExpanded, setAllExpanded] = useState(false)
  const [expandSignal, setExpandSignal] = useState(null)
  // Bumped on each sync success so expanded cards re-fetch their detail.
  const [dataVersion, setDataVersion] = useState(0)

  const refreshCourses = useCallback(async () => {
    try {
      setCourses(await getCourses())
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not load courses', message: e.message })
      setCourses((prev) => prev ?? [])
    }
  }, [toast])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch on mount; state is set after the await
    refreshCourses()
  }, [refreshCourses])

  // Poll every active job until it reaches a terminal state; toast on finish.
  useEffect(() => {
    const active = jobs.filter((j) => !TERMINAL.includes(j.status))
    if (active.length === 0) return

    const timer = setTimeout(async () => {
      const updates = await Promise.all(active.map((j) => getJob(j.id).catch(() => null)))
      const byId = new Map(updates.filter(Boolean).map((u) => [u.id, u]))
      if (byId.size === 0) return

      const DONE_TITLE = {
        EXTRACT: 'Extraction complete', RAG: 'Ingestion complete', MCQ: 'MCQ generation complete',
      }
      const FAIL_TITLE = {
        EXTRACT: 'Extraction failed', RAG: 'Ingestion failed', MCQ: 'MCQ generation failed',
      }
      for (const u of byId.values()) {
        if (u.status === 'SUCCESS') {
          toast.push({
            kind: 'success',
            title: DONE_TITLE[u.job_type] || 'Sync complete',
            message: u.message || u.course_id,
          })
        }
        if (u.status === 'FAILURE') {
          toast.push({
            kind: 'error',
            title: FAIL_TITLE[u.job_type] || 'Sync failed',
            message: u.error || u.course_id,
            duration: 8000,
          })
        }
      }
      if ([...byId.values()].some((u) => u.status === 'SUCCESS')) {
        refreshCourses()
        setDataVersion((v) => v + 1)
      }
      setJobs((prev) => prev.map((j) => byId.get(j.id) || j))
    }, 2000)
    return () => clearTimeout(timer)
  }, [jobs, refreshCourses, toast])

  function beginSync(payload) {
    startSync(payload)
      .then((job) => {
        setJobs((prev) => [job, ...prev])
        toast.push({
          kind: 'info',
          title: 'Sync started',
          message: `Fetching ${payload.course_id} in the background — watch Activity for progress.`,
        })
      })
      .catch((e) => toast.push({ kind: 'error', title: 'Could not start sync', message: e.message }))
  }

  function handleSyncExisting(course) {
    // Reuses the course's stored version on the backend, and pins the sync to
    // the course's own environment so it never falls back to the PROD default.
    beginSync({ course_id: course.course_id, environment: course.environment })
  }

  function handleExtractSubmit(course, tokens, unitIds = null) {
    extractContent(course.course_id, tokens, unitIds)
      .then((job) => {
        setJobs((prev) => [job, ...prev])
        toast.push({
          kind: 'info',
          title: unitIds ? 'Syncing learning set' : 'Extraction started',
          message: unitIds
            ? 'Refreshing this learning set’s content — watch Activity for progress.'
            : 'Reading material is being fetched — watch Activity for progress.',
        })
      })
      .catch((e) =>
        toast.push({ kind: 'error', title: 'Could not start extraction', message: e.message }),
      )
  }

  // Per-unit "Sync content": re-extract just one learning set. If its reading
  // materials already have stored resource ids we go token-free via the admin
  // panel; otherwise we open the token modal scoped to that unit.
  function handleSyncUnit(course, unitIds, needsToken) {
    if (needsToken) {
      setExtractFor({ course, unitIds, env: course.environment })
    } else {
      handleExtractSubmit(course, {}, unitIds)
    }
  }

  function handleIngestSubmit(course, unitIds) {
    ingestContent(course.course_id, unitIds)
      .then((job) => {
        // Track the RAG job like sync/extract so it polls and shows in Activity.
        setJobs((prev) => [job, ...prev])
        toast.push({
          kind: 'info',
          title: 'Ingestion started',
          message: `Indexing ${unitIds.length} resource${unitIds.length === 1 ? '' : 's'} — watch Activity for progress.`,
        })
      })
      .catch((e) => toast.push({ kind: 'error', title: 'Could not start ingestion', message: e.message }))
  }

  // course_id → Set of active job types (SYNC, EXTRACT), so each pipeline
  // step animates only for its own kind of job.
  const activeJobsByCourse = useMemo(() => {
    const map = new Map()
    for (const j of jobs) {
      if (TERMINAL.includes(j.status)) continue
      if (!map.has(j.course_id)) map.set(j.course_id, new Set())
      map.get(j.course_id).add(j.job_type || 'SYNC')
    }
    return map
  }, [jobs])
  const activeJobCount = jobs.filter((j) => !TERMINAL.includes(j.status)).length

  function toggleAllCards() {
    const mode = allExpanded ? 'collapse' : 'expand'
    setAllExpanded(!allExpanded)
    setExpandSignal((s) => ({ mode, seq: (s?.seq || 0) + 1 }))
  }

  const filtered = useMemo(() => {
    if (!courses) return null
    const q = query.trim().toLowerCase()
    let list = courses
    if (envFilter) list = list.filter((c) => c.environment === envFilter)
    if (!q) return list
    return list.filter(
      (c) =>
        (c.course_name || '').toLowerCase().includes(q) ||
        c.course_id.toLowerCase().includes(q) ||
        (c.course_category || '').toLowerCase().includes(q),
    )
  }, [courses, query, envFilter])

  const stats = useMemo(() => {
    if (!courses) return null
    return {
      total: courses.length,
      extracted: courses.filter((c) => c.content_extracted_at).length,
      ingested: courses.filter((c) => c.is_ingested).length,
      beta: courses.filter((c) => c.environment === 'BETA').length,
      prod: courses.filter((c) => c.environment === 'PROD').length,
    }
  }, [courses])

  return (
    <div className="app-shell">
      <Sidebar
        page={page}
        onNavigate={setPage}
        activeJobCount={activeJobCount}
        onOpenActivity={() => setDrawerOpen(true)}
        theme={theme}
        onToggleTheme={toggleTheme}
        open={navOpen}
        onClose={() => setNavOpen(false)}
        collapsed={navCollapsed}
        onToggleCollapse={toggleNavCollapsed}
        user={user}
        onOpenAccount={() => setAccountOpen(true)}
        onOpenFeedback={() => setFeedbackOpen(true)}
      />
      <div
        className={`nav-scrim ${navOpen ? 'open' : ''}`}
        onClick={() => setNavOpen(false)}
        aria-hidden="true"
      />

      <div className="workspace-col">
        <MobileBar
          onOpenNav={() => setNavOpen(true)}
          activeJobCount={activeJobCount}
          onOpenActivity={() => setDrawerOpen(true)}
        />
        <main className="main">
        {page === 'chat' && <ChatPage courses={courses} />}
        {page === 'generation' && <GenerationStudio onNavigate={setPage} />}
        {page === 'pipeline' && <PipelinePage />}
        {page === 'llm-providers' && <LLMProvidersPage />}
        {page === 'mcq' && (
          <McqGenerationPage
            courses={courses}
            onBack={() => setPage('generation')}
            onTrackJob={(job) => setJobs((prev) => [job, ...prev])}
          />
        )}
        {page === 'runs' && <McqRunsPage courses={courses} />}
        {page === 'review' && <ReviewQueuePage courses={courses} />}
        {page === 'admin' && <AdminDashboard />}
        {page === 'courses' && (
        <>
        <header className="topbar">
          <div>
            <h1>Courses</h1>
            <p className="topbar-sub">
              Fetch course hierarchies, link prerequisites, extract content and build the RAG
              knowledge base.
            </p>
          </div>
          <div className="topbar-actions">
            {activeJobCount > 0 && (
              <button className="btn btn-soft btn-sm running-pill" onClick={() => setDrawerOpen(true)}>
                <Spinner size={13} />
                {activeJobCount} job{activeJobCount === 1 ? '' : 's'} running
              </button>
            )}
            <button
              className="btn btn-primary"
              onClick={() => setWizard({ prereqFor: null, defaultEnv: 'PROD' })}
            >
              <Plus size={15} /> Add Course
            </button>
          </div>
        </header>

        {stats && courses.length > 0 && (
          <div className="stats-row">
            <StatCard label="Courses" value={stats.total} accent="var(--brand)" />
            <StatCard
              label="PROD"
              value={stats.prod}
              accent="var(--green)"
              active={envFilter === 'PROD'}
              onClick={() => setEnvFilter((f) => (f === 'PROD' ? null : 'PROD'))}
            />
            <StatCard
              label="BETA"
              value={stats.beta}
              accent="var(--amber)"
              active={envFilter === 'BETA'}
              onClick={() => setEnvFilter((f) => (f === 'BETA' ? null : 'BETA'))}
            />
            <StatCard label="Content extracted" value={stats.extracted} accent="var(--cyan)" />
            <StatCard label="Ingested" value={stats.ingested} accent="var(--violet)" />
          </div>
        )}

        {courses && courses.length > 0 && (
          <div className="toolbar">
            <div className="search-bar">
              <Search size={15} className="search-icon" />
              <input
                className="input search-input"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Filter by name, ID or category…"
                spellCheck={false}
              />
            </div>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={toggleAllCards}
              data-tip={allExpanded ? 'Collapse every course' : 'Expand every course'}
            >
              {allExpanded ? <ChevronsDownUp size={14} /> : <ChevronsUpDown size={14} />}
              {allExpanded ? 'Collapse all' : 'Expand all'}
            </button>
          </div>
        )}

        <div className="course-list">
          {filtered === null && (
            <>
              <Skeleton height={92} />
              <Skeleton height={92} />
              <Skeleton height={92} width="92%" />
            </>
          )}

          {filtered !== null && courses.length === 0 && (
            <EmptyState
              icon={LayoutGrid}
              title="No courses yet"
              hint="Add a course to fetch its topics and units from the portal. From there you can link prerequisites, extract content and build the RAG index."
              action={
                <button
                  className="btn btn-primary"
                  onClick={() => setWizard({ prereqFor: null, defaultEnv: 'PROD' })}
                >
                  <Plus size={15} /> Add your first course
                </button>
              }
            />
          )}

          {filtered !== null && courses.length > 0 && filtered.length === 0 && (
            <EmptyState
              icon={Search}
              title="No matches"
              hint={
                envFilter
                  ? `No ${envFilter} courses${query.trim() ? ` match “${query.trim()}”` : ''}. Click the ${envFilter} card again to clear the filter.`
                  : `Nothing matches “${query.trim()}”.`
              }
            />
          )}

          {filtered?.map((course, i) => (
            <CourseCard
              key={course.course_id}
              index={i}
              course={course}
              activeJobsByCourse={activeJobsByCourse}
              expandSignal={expandSignal}
              dataVersion={dataVersion}
              onSync={handleSyncExisting}
              onAddPrerequisite={(c) =>
                setWizard({ prereqFor: c.course_id, defaultEnv: c.environment || 'PROD' })
              }
              onExtract={(c) => setExtractFor({ course: c, unitIds: null })}
              onIngest={setIngestFor}
              onSyncUnit={handleSyncUnit}
            />
          ))}
        </div>
        </>
        )}
        </main>
      </div>

      <JobsDrawer
        open={drawerOpen}
        jobs={jobs}
        onClose={() => setDrawerOpen(false)}
        onDismiss={(id) => setJobs((prev) => prev.filter((j) => j.id !== id))}
        onClearFinished={() => setJobs((prev) => prev.filter((j) => !TERMINAL.includes(j.status)))}
      />

      {wizard && (
        <AddCourseWizard
          prerequisiteFor={wizard.prereqFor}
          defaultEnv={wizard.defaultEnv}
          onClose={() => setWizard(null)}
          onStartSync={beginSync}
        />
      )}

      {extractFor && (
        <TokenModal
          course={extractFor.course}
          unitIds={extractFor.unitIds}
          requireEnv={extractFor.env}
          onClose={() => setExtractFor(null)}
          onSubmit={handleExtractSubmit}
        />
      )}

      {ingestFor && (
        <IngestModal
          course={ingestFor}
          onClose={() => setIngestFor(null)}
          onSubmit={handleIngestSubmit}
        />
      )}

      {accountOpen && <AccountModal onClose={() => setAccountOpen(false)} />}
      {feedbackOpen && <FeedbackForm onClose={() => setFeedbackOpen(false)} />}
    </div>
  )
}

function App() {
  return (
    <ToastProvider>
      <AuthProvider>
        <AuthGate>
          <Workspace />
        </AuthGate>
      </AuthProvider>
    </ToastProvider>
  )
}

export default App
