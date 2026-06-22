import { CheckCircle2, AlertTriangle, Circle } from 'lucide-react'
import { Spinner } from './ui'

// The fixed pipeline stages, shown immediately (all pending) so the board is
// never blank while the first progress update is in flight.
const DEFAULT_STAGES = [
  // LO-creation stage — the deterministic 10-node pipeline.
  { key: 'parse_structure', label: 'Parse structure', state: 'running' },
  { key: 'extract_concepts', label: 'Extract concepts (self-consistency)', state: 'pending' },
  { key: 'canonicalize_concepts', label: 'Canonicalize concepts', state: 'pending' },
  { key: 'build_dependency_graph', label: 'Build dependency graph', state: 'pending' },
  { key: 'plan_allocation', label: 'Plan allocation', state: 'pending' },
  { key: 'author_outcomes', label: 'Author outcomes', state: 'pending' },
  { key: 'resolve_prerequisites', label: 'Resolve prerequisites', state: 'pending' },
  { key: 'validate', label: 'Validate (V1–V11)', state: 'pending' },
  { key: 'repair', label: 'Repair (if needed)', state: 'pending' },
  { key: 'finalize', label: 'Finalize & freeze', state: 'pending' },
  { key: 'lo_to_legacy', label: 'Bridge to questions', state: 'pending' },
  // Question stage.
  { key: 'recommend_question_types', label: 'Pick question types', state: 'pending' },
  { key: 'generate_questions', label: 'Generate questions', state: 'pending' },
  { key: 'review_questions', label: 'Review & fix', state: 'pending' },
]

// Live stage board for an MCQ run, driven by the job's structured `progress`.
// Stages sharing a `parallel_group` render side by side to mirror the
// concurrently-running branches in the backend.

function StageIcon({ state }) {
  if (state === 'done') return <CheckCircle2 size={15} className="mcq-ic-done" />
  if (state === 'running') return <Spinner size={14} />
  if (state === 'error') return <AlertTriangle size={15} className="mcq-ic-err" />
  return <Circle size={14} className="mcq-ic-pending" />
}

function StageCard({ stage }) {
  const hasCount = typeof stage.total === 'number' && stage.total > 0
  const pct = hasCount ? Math.round(((stage.done || 0) / stage.total) * 100) : 0
  return (
    <div className={`mcq-stage state-${stage.state}`}>
      <div className="mcq-stage-head">
        <StageIcon state={stage.state} />
        <span className="mcq-stage-label">{stage.label}</span>
      </div>
      {stage.detail && <div className="mcq-stage-detail">{stage.detail}</div>}
      {hasCount && (
        <div className="mcq-stage-progress">
          <div className="mcq-stage-bar">
            <div className="mcq-stage-bar-fill" style={{ width: `${pct}%` }} />
          </div>
          <span className="mcq-stage-count">
            {stage.done || 0}/{stage.total}
            {stage.needs_human ? ` · ${stage.needs_human} need review` : ''}
          </span>
        </div>
      )}
    </div>
  )
}

function McqProgress({ progress }) {
  const stages = progress?.stages?.length ? progress.stages : DEFAULT_STAGES
  const total = stages.length
  const completed = stages.filter((s) => s.state === 'done').length

  // Progressive reveal: show every stage that has started (done/running) plus the
  // single next pending stage as "up next" — the rest stay hidden until the flow
  // reaches them, so steps appear one after another instead of all at once.
  let lastShown = -1
  stages.forEach((s, i) => {
    if (s.state === 'done' || s.state === 'running' || s.state === 'error') lastShown = i
  })
  const revealIdx = Math.min(lastShown + 1, total - 1) // include the next pending stage
  const visible = stages.slice(0, revealIdx + 1)
  const hidden = total - visible.length

  // Group consecutive stages sharing a parallel_group into one side-by-side row.
  const rows = []
  let i = 0
  while (i < visible.length) {
    const group = visible[i].parallel_group
    if (group) {
      const grp = []
      while (i < visible.length && visible[i].parallel_group === group) grp.push(visible[i++])
      rows.push(grp)
    } else {
      rows.push([visible[i++]])
    }
  }

  return (
    <div className="mcq-progress">
      <div className="mcq-progress-head">
        <Spinner size={14} />
        <span>Generating — step {Math.min(completed + 1, total)} of {total}</span>
      </div>
      {rows.map((row, ri) => (
        <div key={ri} className={`mcq-progress-row ${row.length > 1 ? 'parallel' : ''}`}>
          {row.map((s) => (
            <StageCard key={s.key} stage={s} />
          ))}
        </div>
      ))}
      {hidden > 0 && (
        <div className="mcq-progress-more">+{hidden} more step{hidden === 1 ? '' : 's'}</div>
      )}
    </div>
  )
}

export default McqProgress
