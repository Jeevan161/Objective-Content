import { useState } from 'react'
import { CheckCircle2, AlertTriangle, Circle, ChevronRight } from 'lucide-react'
import { Spinner } from './ui'
import NodeSnapshot from './NodeSnapshot'

// The fixed pipeline stages, shown immediately (all pending) so the board is
// never blank while the first progress update is in flight. Mirrors the backend
// LO-first flow (progress.STAGE_DEFS).
const DEFAULT_STAGES = [
  // LO-creation stage — the LO-first pipeline.
  { key: 'parse_structure', label: 'Parse structure', state: 'running' },
  { key: 'generate_outcomes', label: 'Generate all outcomes', state: 'pending' },
  { key: 'map_concepts', label: 'Map concepts (consistent across outcomes)', state: 'pending' },
  { key: 'build_outcome_graph', label: 'Build outcome graph (weights)', state: 'pending' },
  { key: 'profile_depth', label: 'Profile depth (feasibility)', state: 'pending' },
  { key: 'plan_outcomes', label: 'Plan outcomes (budget + identify apply)', state: 'pending' },
  { key: 'review_division', label: 'Review division (human gate 1)', state: 'pending' },
  { key: 'resolve_prerequisites', label: 'Resolve prerequisites (apply)', state: 'pending' },
  { key: 'review_outcomes_quality', label: 'Dedup & judge (R1–R8 rubric)', state: 'pending' },
  { key: 'validate', label: 'Validate (structural + rubric gate)', state: 'pending' },
  { key: 'repair', label: 'Repair (if needed)', state: 'pending' },
  { key: 'review_outcomes', label: 'Review outcomes (human gate 2)', state: 'pending' },
  { key: 'finalize', label: 'Finalize & freeze', state: 'pending' },
  { key: 'lo_to_legacy', label: 'Bridge to questions', state: 'pending' },
  { key: 'sequence_outcomes', label: 'Sequence outcomes (deep-dive order)', state: 'pending' },
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
  const [open, setOpen] = useState(false)
  const hasCount = typeof stage.total === 'number' && stage.total > 0
  const pct = hasCount ? Math.round(((stage.done || 0) / stage.total) * 100) : 0
  // A node's state details are available once it has produced a snapshot (i.e. it has run).
  const hasDetails = stage.snapshot && Object.keys(stage.snapshot).length > 0
  return (
    <div className={`mcq-stage state-${stage.state} ${open ? 'open' : ''}`}>
      <div
        className={`mcq-stage-head ${hasDetails ? 'clickable' : ''}`}
        onClick={hasDetails ? () => setOpen((o) => !o) : undefined}
        role={hasDetails ? 'button' : undefined}
      >
        <StageIcon state={stage.state} />
        <span className="mcq-stage-label">{stage.label}</span>
        {hasDetails && <ChevronRight size={13} className="mcq-stage-chevron" />}
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
      {open && hasDetails && (
        <div className="mcq-stage-snapshot">
          <NodeSnapshot snapshot={stage.snapshot} />
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
