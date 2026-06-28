import { useEffect, useMemo, useState } from 'react'
import { ClipboardCheck, RefreshCw, ListChecks, CheckCircle2, ChevronRight } from 'lucide-react'
import { listAllMcqRuns, getMcqRun } from '../api'
import { EmptyState, Spinner } from './ui'
import { useToast } from './Toast'
import McqResults from './McqResults'

function fmtDate(s) {
  if (!s) return ''
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return ''
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${d.toLocaleTimeString(
    undefined,
    { hour: '2-digit', minute: '2-digit' },
  )}`
}

// A run still needs reviewing until it's marked reviewed AND every generated question is approved.
function needsReview(r) {
  return r.review_status !== 'approved' || (r.approved_count ?? 0) < (r.eligible_count ?? 0)
}

// Review Queue: runs awaiting question review. Open one to Approve/Reject each question and load
// it to the portal once approved. Shares the McqResults viewer in its interactive 'review' mode.
function ReviewQueuePage({ courses, onTrackJob }) {
  const toast = useToast()
  const [runs, setRuns] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [run, setRun] = useState(null)
  const [loadingRun, setLoadingRun] = useState(false)
  const [railOpen, setRailOpen] = useState(false)   // collapsed queue opens on click, not hover
  const [tab, setTab] = useState('queue')           // 'queue' (needs review) | 'reviewed'

  const nameOf = useMemo(
    () => Object.fromEntries((courses || []).map((c) => [c.course_id, c.course_name || c.course_id])),
    [courses],
  )
  const queueRuns = useMemo(() => (runs || []).filter(needsReview), [runs])
  const reviewedRuns = useMemo(() => (runs || []).filter((r) => !needsReview(r)), [runs])
  const shownRuns = tab === 'queue' ? queueRuns : reviewedRuns

  function load() {
    setRuns(null)
    listAllMcqRuns()
      .then((rows) => setRuns(rows || []))
      .catch((e) => {
        setRuns([])
        toast.push({ kind: 'error', title: 'Could not load review queue', message: e.message })
      })
  }
  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function open(r) {
    setSelectedId(r.id)
    setRailOpen(false)            // close the queue overlay after picking a run
    setRun(null)
    setLoadingRun(true)
    getMcqRun(r.id)
      .then((full) => setRun(full))
      .catch((e) => toast.push({ kind: 'error', title: 'Could not load run', message: e.message }))
      .finally(() => setLoadingRun(false))
  }

  return (
    <div className="runs-page">
      <header className="topbar">
        <div>
          <h1>Review</h1>
          <p className="topbar-sub">
            Approve or reject each question, then load the approved set to the portal.
          </p>
        </div>
        <div className="topbar-actions">
          <button className="btn btn-ghost btn-sm" onClick={load} data-tip="Reload">
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
      </header>

      {runs !== null && (
        <div className="queue-tabs">
          <button type="button" className={`mcq-chip ${tab === 'queue' ? 'active' : ''}`}
            onClick={() => { setTab('queue'); setSelectedId(null); setRun(null) }}>
            Review Queue {queueRuns.length > 0 && <span className="mcq-chip-n">{queueRuns.length}</span>}
          </button>
          <button type="button" className={`mcq-chip ${tab === 'reviewed' ? 'active' : ''}`}
            onClick={() => { setTab('reviewed'); setSelectedId(null); setRun(null) }}>
            Reviewed {reviewedRuns.length > 0 && <span className="mcq-chip-n">{reviewedRuns.length}</span>}
          </button>
        </div>
      )}

      {runs === null && (
        <div className="mcq-loading">
          <Spinner size={14} /> Loading runs…
        </div>
      )}

      {runs !== null && shownRuns.length === 0 && (
        <EmptyState
          icon={ClipboardCheck}
          title={tab === 'queue' ? 'Nothing to review' : 'No reviewed sets yet'}
          hint={tab === 'queue'
            ? 'When a run finishes generating it shows up here until every question is approved.'
            : 'Sets you mark reviewed (all questions approved) appear here.'}
        />
      )}

      {shownRuns.length > 0 && (
        <div className={`runs-layout queue-layout${selectedId ? ' is-collapsed' : ''}`}>
          <div className={`queue-rail${railOpen ? ' rail-open' : ''}`}>
            {selectedId && (
              <button type="button" className="queue-handle" aria-label={railOpen ? 'Hide run queue' : 'Show run queue'}
                aria-expanded={railOpen} onClick={() => setRailOpen((o) => !o)}>
                <ChevronRight size={16} />
                <span className="queue-handle-label">Queue</span>
              </button>
            )}
            <ul className="runs-list">
            {shownRuns.map((r) => (
              <li key={r.id}>
                <button
                  type="button"
                  className={`runs-item ${selectedId === r.id ? 'active' : ''}`}
                  onClick={() => open(r)}
                >
                  <div className="runs-item-head">
                    <span className="runs-item-course">{nameOf[r.course_id] || r.course_id}</span>
                    {r.version != null && <span className="runs-item-ver">v{r.version}</span>}
                    <span className="runs-item-date">{fmtDate(r.created_at)}</span>
                  </div>
                  <div className="runs-item-stats">
                    <span className={(r.approved_count ?? 0) === (r.eligible_count ?? 0) ? 'runs-item-approved' : ''}>
                      {r.approved_count ?? 0} / {r.eligible_count ?? 0} approved
                    </span>
                    {r.needs_human_count > 0 && (
                      <span className="runs-item-review">{r.needs_human_count} need review</span>
                    )}
                  </div>
                </button>
              </li>
            ))}
            </ul>
          </div>

          <div className="runs-detail">
            {loadingRun && (
              <div className="mcq-loading">
                <Spinner size={14} /> Loading run…
              </div>
            )}
            {!loadingRun && run && <McqResults key={run.id} run={run} mode="review" courseId={run.course_id} unitId={run.unit_id} onTrackJob={onTrackJob} />}
            {!loadingRun && !run && (
              <EmptyState
                icon={ListChecks}
                title="Select a run to review"
                hint="Pick a run on the left to approve its questions and load it to the portal."
              />
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export default ReviewQueuePage
