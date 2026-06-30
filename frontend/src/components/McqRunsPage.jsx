import { useEffect, useMemo, useState } from 'react'
import { History, RefreshCw, ListChecks, CheckCircle2 } from 'lucide-react'
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

// Runs: every saved MCQ generation run, newest first. Selecting one loads its full result and
// renders it (outcomes / questions / spec / node-by-node trace) via the shared McqResults viewer.
function McqRunsPage({ courses }) {
  const toast = useToast()
  const [runs, setRuns] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [run, setRun] = useState(null)
  const [loadingRun, setLoadingRun] = useState(false)

  const nameOf = useMemo(
    () => Object.fromEntries((courses || []).map((c) => [c.course_id, c.course_name || c.course_id])),
    [courses],
  )

  function load() {
    setRuns(null)
    listAllMcqRuns()
      .then((rows) => setRuns(rows || []))
      .catch((e) => {
        setRuns([])
        toast.push({ kind: 'error', title: 'Could not load runs', message: e.message })
      })
  }
  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function open(r) {
    setSelectedId(r.id)
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
          <h1>Runs</h1>
          <p className="topbar-sub">
            Every MCQ generation run is saved here — open one to see its outcomes, questions, frozen
            spec and node-by-node trace.
          </p>
        </div>
        <div className="topbar-actions">
          <button className="btn btn-ghost btn-sm" onClick={load} data-tip="Reload runs">
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
      </header>

      {runs === null && (
        <div className="mcq-loading">
          <Spinner size={14} /> Loading runs…
        </div>
      )}

      {runs && runs.length === 0 && (
        <EmptyState
          icon={History}
          title="No runs yet"
          hint="Generate MCQs from a session in the Generation Studio — every run is saved here with its trace."
        />
      )}

      {runs && runs.length > 0 && (
        <div className="runs-layout">
          <ul className="runs-list">
            {runs.map((r) => (
              <li key={r.id}>
                <button
                  type="button"
                  className={`runs-item ${selectedId === r.id ? 'active' : ''}`}
                  onClick={() => open(r)}
                >
                  <div className="runs-item-head">
                    <span className="runs-item-course" title={r.unit_name || r.unit_id}>{r.unit_name || r.unit_id || 'Untitled set'}</span>
                    {r.version != null && <span className="runs-item-ver">v{r.version}</span>}
                    <span className="runs-item-date">{fmtDate(r.created_at)}</span>
                  </div>
                  <div className="runs-item-sub">
                    <span className="course-badge" title="Course">{nameOf[r.course_id] || r.course_id}</span>
                  </div>
                  <div className="runs-item-stats">
                    <span>{r.lo_count} LOs</span>
                    <span>{r.question_count} Q</span>
                    {r.estimated_cost_usd > 0 && (
                      <span title="Estimated token cost for this run (list-price estimate)">
                        ${r.estimated_cost_usd < 0.01 ? r.estimated_cost_usd.toFixed(5) : r.estimated_cost_usd.toFixed(r.estimated_cost_usd < 1 ? 4 : 2)}
                      </span>
                    )}
                    {r.needs_human_count > 0 && (
                      <span className="runs-item-review">{r.needs_human_count} need review</span>
                    )}
                    {r.review_status === 'approved' && (
                      <span className="runs-item-approved">
                        <CheckCircle2 size={11} /> approved
                      </span>
                    )}
                  </div>
                </button>
              </li>
            ))}
          </ul>

          <div className="runs-detail">
            {loadingRun && (
              <div className="mcq-loading">
                <Spinner size={14} /> Loading run…
              </div>
            )}
            {!loadingRun && run && <McqResults key={run.id} run={run} courseId={run.course_id} unitId={run.unit_id} />}
            {!loadingRun && !run && (
              <EmptyState
                icon={ListChecks}
                title="Select a run"
                hint="Pick a run on the left to see its outcomes, questions, spec and trace."
              />
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export default McqRunsPage
