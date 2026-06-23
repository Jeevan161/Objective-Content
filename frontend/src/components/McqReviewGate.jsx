import { useState } from 'react'
import { CheckCircle2, AlertTriangle, RotateCcw, ThumbsUp } from 'lucide-react'

// The human-in-the-loop review gate. Renders the paused pipeline's review payload
// (job.progress.review): Gate 1 = the proposed LO division; Gate 2 = the authored outcomes.
// Approve continues the run; reject sends it back (re-plan / regenerate the unchecked outcomes).

const TIERS = ['remember', 'understand', 'apply', 'scenario']

function Flags({ flags }) {
  if (!flags || flags.length === 0) return null
  return (
    <div className="mcq-review-flags">
      {flags.map((f) => (
        <span key={f} className="mcq-badge warn">
          <AlertTriangle size={11} /> {f.replaceAll('_', ' ')}
        </span>
      ))}
    </div>
  )
}

function DivisionGate({ proposal }) {
  const tc = proposal.tier_counts || {}
  const inScope = proposal.in_scope || []
  const dropped = proposal.dropped || []
  return (
    <>
      <div className="mcq-spec-grid">
        <div className="mcq-spec-row">
          <span className="mcq-spec-k">Budget</span>
          <span>
            {proposal.final_budget} question{proposal.final_budget === 1 ? '' : 's'}
            {proposal.budget_reduced && (
              <span className="mcq-lo-tag"> · requested {proposal.requested_budget}, capped to material</span>
            )}
          </span>
        </div>
        <div className="mcq-spec-row">
          <span className="mcq-spec-k">Bloom division</span>
          <span className="mcq-review-tiers">
            {TIERS.map((t) => (
              <span key={t} className={`mcq-lo-bloom b-${t}`}>
                {t} {tc[t] ?? 0}
              </span>
            ))}
          </span>
        </div>
      </div>
      <Flags flags={proposal.flags} />
      <div className="mcq-review-section">
        <span className="mcq-spec-k">In scope ({inScope.length})</span>
        <ul className="mcq-lo-list detailed">
          {inScope.map((c) => (
            <li key={c.concept_id} className="mcq-lo-item">
              <div className="mcq-lo-main">
                <span className="mcq-lo-desc">{c.name}</span>
                <span className="mcq-lo-type">
                  {c.depth}
                  {c.procedural ? ' · procedural' : ''}
                </span>
              </div>
              <div className="mcq-lo-meta">
                <span className="mcq-lo-tag">supports: {(c.ceiling || []).join(', ')}</span>
              </div>
            </li>
          ))}
        </ul>
      </div>
      {dropped.length > 0 && (
        <div className="mcq-review-section">
          <span className="mcq-spec-k">Dropped ({dropped.length})</span>
          <ul className="mcq-lo-list">
            {dropped.map((c) => (
              <li key={c.concept_id}>
                <span className="mcq-lo-desc">{c.name}</span>
                <span className="mcq-lo-tag">{c.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  )
}

function OutcomesGate({ outcomes, reviews, rejected, toggle }) {
  return (
    <div className="mcq-review-section">
      <span className="mcq-spec-k">
        Learning outcomes ({outcomes.length}) — uncheck any to regenerate before continuing
      </span>
      <ul className="mcq-lo-list detailed">
        {outcomes.map((o) => {
          const rv = (reviews || {})[o.id] || {}
          const failed = rv.covered === false
          const keep = !rejected.has(o.id)
          return (
            <li key={o.id} className="mcq-lo-item">
              <div className="mcq-lo-main">
                <input
                  type="checkbox"
                  checked={keep}
                  onChange={() => toggle(o.id)}
                  data-tip="Keep this outcome (uncheck to regenerate)"
                />
                <span className={`mcq-lo-bloom b-${o.bloom_level}`}>{o.bloom_level}</span>
                <span className="mcq-lo-desc">{o.title || o.description}</span>
                {failed ? (
                  <span className="mcq-badge warn">
                    <AlertTriangle size={11} /> rubric
                  </span>
                ) : (
                  <span className="mcq-badge ok">
                    <CheckCircle2 size={11} /> ok
                  </span>
                )}
              </div>
              <div className="mcq-lo-meta">
                <span className="mcq-lo-tag">concept: {(o.concept_id || '').replace(/^C_/, '')}</span>
                {o.learner_action && <span className="mcq-lo-tag">verb: {o.learner_action}</span>}
                {failed && rv.fail_reason && (
                  <span className="mcq-lo-tag mcq-review-fail">{rv.fail_reason}</span>
                )}
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function McqReviewGate({ review, busy, onDecide }) {
  const gate = review?.gate
  const isDivision = gate === 'division'
  const [note, setNote] = useState('')
  const [rejected, setRejected] = useState(() => new Set())

  const toggle = (id) =>
    setRejected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const title = isDivision ? 'Review the LO division' : 'Review the learning outcomes'
  const sub = isDivision
    ? 'Approve the proposed budget, Bloom division and in-scope concepts — or reject to re-plan.'
    : 'Approve to generate questions, or uncheck outcomes to regenerate them before continuing.'
  const rejectCount = rejected.size

  function approve() {
    onDecide({ action: 'approve' })
  }
  function reject() {
    if (isDivision) onDecide({ action: 'reject', note })
    else onDecide({ action: 'reject', rejected_ids: [...rejected], note })
  }

  if (!gate) return null

  return (
    <div className="mcq-review">
      <div className="mcq-review-head">
        <div>
          <h3>{title}</h3>
          <p>{sub}</p>
        </div>
        <span className="mcq-badge warn">awaiting review · gate {isDivision ? '1' : '2'}</span>
      </div>

      {isDivision ? (
        <DivisionGate proposal={review.proposal || {}} />
      ) : (
        <OutcomesGate
          outcomes={review.outcomes || []}
          reviews={review.reviews}
          rejected={rejected}
          toggle={toggle}
        />
      )}

      <textarea
        className="input mcq-review-note"
        rows={2}
        value={note}
        disabled={busy}
        onChange={(e) => setNote(e.target.value)}
        placeholder={isDivision ? 'Optional note for the planner on reject…' : 'Optional note for regeneration…'}
      />

      <div className="mcq-review-actions">
        <button
          type="button"
          className="btn btn-ghost"
          disabled={busy || (!isDivision && rejectCount === 0)}
          onClick={reject}
        >
          <RotateCcw size={14} />
          {isDivision ? 'Reject & re-plan' : `Regenerate ${rejectCount || ''}`.trim()}
        </button>
        <button type="button" className="btn btn-primary" disabled={busy} onClick={approve}>
          <ThumbsUp size={14} /> Approve &amp; continue
        </button>
      </div>
    </div>
  )
}

export default McqReviewGate
