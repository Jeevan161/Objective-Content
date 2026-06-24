import { useEffect } from 'react'
import { X, CheckCircle2, XCircle, RefreshCw, FileText, Database, ListChecks, Trash2, Activity } from 'lucide-react'
import { EnvBadge, Spinner, EmptyState } from './ui'

const TERMINAL = ['SUCCESS', 'FAILURE']

const JOB_TYPE_META = {
  SYNC: { label: 'Course sync', icon: RefreshCw },
  EXTRACT: { label: 'Content extraction', icon: FileText },
  RAG: { label: 'RAG ingestion', icon: Database },
  MCQ: { label: 'MCQ generation', icon: ListChecks },
}

function timeAgo(iso) {
  if (!iso) return ''
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (sec < 60) return 'just now'
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`
  return `${Math.floor(sec / 86400)}d ago`
}

function JobRow({ job, onDismiss }) {
  const active = !TERMINAL.includes(job.status)
  const meta = JOB_TYPE_META[job.job_type] || JOB_TYPE_META.SYNC
  const TypeIcon = meta.icon

  return (
    <div className={`job-row job-${job.status.toLowerCase()}`}>
      <div className="job-row-status">
        {active && <Spinner size={16} />}
        {job.status === 'SUCCESS' && <CheckCircle2 size={16} className="text-green" />}
        {job.status === 'FAILURE' && <XCircle size={16} className="text-red" />}
      </div>
      <div className="job-row-main">
        <div className="job-row-title">
          <TypeIcon size={13} className="job-type-icon" />
          <span>{meta.label}</span>
          <EnvBadge env={job.environment} />
        </div>
        <code className="job-row-course">{job.course_id}</code>
        <div className="job-row-msg">
          {job.error || job.message || (active ? 'Starting…' : '')}
        </div>
        <div className="job-row-time">{timeAgo(job.created_at)}</div>
      </div>
      {!active && (
        <button className="icon-btn" onClick={() => onDismiss(job.id)} aria-label="Dismiss job">
          <X size={14} />
        </button>
      )}
    </div>
  )
}

// Slide-over panel listing every background job (running + finished) so the
// user can always see exactly what the system is doing.
function JobsDrawer({ open, jobs, onClose, onDismiss, onClearFinished }) {
  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Escape') onClose()
    }
    if (open) document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const finished = jobs.filter((j) => TERMINAL.includes(j.status))

  return (
    <div className="drawer-backdrop" onMouseDown={onClose}>
      <aside className="drawer" onMouseDown={(e) => e.stopPropagation()}>
        <div className="drawer-header">
          <h2>Activity</h2>
          <div className="drawer-header-actions">
            {finished.length > 0 && (
              <button className="btn btn-ghost btn-sm" onClick={onClearFinished}>
                <Trash2 size={13} /> Clear finished
              </button>
            )}
            <button className="icon-btn" onClick={onClose} aria-label="Close">
              <X size={16} />
            </button>
          </div>
        </div>
        <div className="drawer-body">
          {jobs.length === 0 ? (
            <EmptyState
              icon={Activity}
              title="No activity yet"
              hint="Background jobs show up here with live progress."
            />
          ) : (
            jobs.map((job) => <JobRow key={job.id} job={job} onDismiss={onDismiss} />)
          )}
        </div>
      </aside>
    </div>
  )
}

export default JobsDrawer
