import { useEffect } from 'react'
import { X, CheckCircle2, XCircle, RefreshCw, FileText, Database, ListChecks, Trash2, Activity, RotateCcw, Ban, ChevronRight, Upload, FileDown } from 'lucide-react'
import { EnvBadge, Spinner, EmptyState } from './ui'

const TERMINAL = ['SUCCESS', 'FAILURE', 'CANCELLED']

// Job types whose finished rows can be reopened from the drawer (to their target view).
const REOPENABLE = ['MCQ', 'LOAD', 'EXPORT']

const JOB_TYPE_META = {
  SYNC: { label: 'Course sync', icon: RefreshCw },
  EXTRACT: { label: 'Content extraction', icon: FileText },
  RAG: { label: 'RAG ingestion', icon: Database },
  MCQ: { label: 'MCQ generation', icon: ListChecks },
  REGEN: { label: 'Question regeneration', icon: RotateCcw },
  LOAD: { label: 'Portal load', icon: Upload },
  EXPORT: { label: 'ZIP export', icon: FileDown },
}

function timeAgo(iso) {
  if (!iso) return ''
  const sec = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (sec < 60) return 'just now'
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`
  return `${Math.floor(sec / 86400)}d ago`
}

function JobRow({ job, onDismiss, onCancel, onOpen }) {
  const active = !TERMINAL.includes(job.status)
  const meta = JOB_TYPE_META[job.job_type] || JOB_TYPE_META.SYNC
  const TypeIcon = meta.icon
  // MCQ jobs reopen to their exact page/stage; LOAD/EXPORT open the Loads page. Only MCQ
  // can be cancelled while live.
  const canOpen = onOpen && REOPENABLE.includes(job.job_type)
  const canCancel = onCancel && active && job.job_type === 'MCQ'

  return (
    <div className={`job-row job-${job.status.toLowerCase()} ${canOpen ? 'job-row-clickable' : ''}`}
      onClick={canOpen ? () => onOpen(job) : undefined}
      role={canOpen ? 'button' : undefined}
      tabIndex={canOpen ? 0 : undefined}
      onKeyDown={canOpen ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(job) } } : undefined}
    >
      <div className="job-row-status">
        {active && <Spinner size={16} />}
        {job.status === 'SUCCESS' && <CheckCircle2 size={16} className="text-green" />}
        {job.status === 'FAILURE' && <XCircle size={16} className="text-red" />}
        {job.status === 'CANCELLED' && <Ban size={16} className="text-muted" />}
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
      <div className="job-row-actions" onClick={(e) => e.stopPropagation()}>
        {canCancel && (
          <button className="btn btn-ghost btn-sm" onClick={() => onCancel(job)} aria-label="Cancel job">
            <Ban size={13} /> Cancel
          </button>
        )}
        {canOpen && <ChevronRight size={15} className="job-row-open" aria-hidden="true" />}
        {!active && (
          <button className="icon-btn" onClick={() => onDismiss(job.id)} aria-label="Dismiss job">
            <X size={14} />
          </button>
        )}
      </div>
    </div>
  )
}

// Slide-over panel listing every background job (running + finished) so the
// user can always see exactly what the system is doing.
function JobsDrawer({ open, jobs, onClose, onDismiss, onClearFinished, onCancel, onOpenJob }) {
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
            jobs.map((job) => (
              <JobRow key={job.id} job={job} onDismiss={onDismiss}
                onCancel={onCancel} onOpen={onOpenJob} />
            ))
          )}
        </div>
      </aside>
    </div>
  )
}

export default JobsDrawer
