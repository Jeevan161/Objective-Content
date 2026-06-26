import { useState } from 'react'
import { CheckCircle2, AlertTriangle, RotateCcw, ThumbsUp } from 'lucide-react'

// The human-in-the-loop review gate (the LO-division Gate 1 was removed). It renders the
// paused pipeline's authored outcomes. The reviewer unchecks any outcome and gives a per-LO
// reason ("why"); regenerating sends those reasons so each LO is regenerated with its own
// feedback. After a round, the payload's `regenerated_ids` split the view into two columns:
// LEFT = just regenerated (review these), RIGHT = previously approved/kept. The loop repeats.

function OutcomeRow({ o, rv, unchecked, feedback, onToggle, onFeedback, busy }) {
  const failed = rv?.covered === false
  return (
    <li className={`mcq-lo-item ${unchecked ? 'rejecting' : ''}`}>
      <div className="mcq-lo-main">
        <input
          type="checkbox"
          checked={!unchecked}
          disabled={busy}
          onChange={() => onToggle(o.id)}
          data-tip="Keep this outcome (uncheck to regenerate)"
        />
        <span className={`mcq-lo-bloom b-${o.bloom_level}`}>{o.bloom_level}</span>
        <span className="mcq-lo-desc">{o.title || o.description}</span>
        {failed ? (
          <span className="mcq-badge warn"><AlertTriangle size={11} /> rubric</span>
        ) : (
          <span className="mcq-badge ok"><CheckCircle2 size={11} /> ok</span>
        )}
      </div>
      <div className="mcq-lo-meta">
        <span className="mcq-lo-tag">concept: {(o.concept_id || '').replace(/^C_/, '')}</span>
        {o.learner_action && <span className="mcq-lo-tag">verb: {o.learner_action}</span>}
        {failed && rv?.fail_reason && (
          <span className="mcq-lo-tag mcq-review-fail">{rv.fail_reason}</span>
        )}
      </div>
      {unchecked && (
        <textarea
          className="input mcq-lo-feedback"
          rows={2}
          autoFocus
          disabled={busy}
          value={feedback}
          onChange={(e) => onFeedback(o.id, e.target.value)}
          placeholder="Why is this being regenerated? (required)"
        />
      )}
    </li>
  )
}

function OutcomeList({ outcomes, reviews, rejected, onToggle, onFeedback, busy }) {
  return (
    <ul className="mcq-lo-list detailed">
      {outcomes.map((o) => (
        <OutcomeRow
          key={o.id}
          o={o}
          rv={(reviews || {})[o.id]}
          unchecked={rejected.has(o.id)}
          feedback={rejected.get(o.id) || ''}
          onToggle={onToggle}
          onFeedback={onFeedback}
          busy={busy}
        />
      ))}
    </ul>
  )
}

function McqReviewGate({ review, busy, onDecide }) {
  // rejected: Map<outcomeId, feedbackText> — unchecked outcomes awaiting regeneration.
  const [rejected, setRejected] = useState(() => new Map())

  const onToggle = (id) =>
    setRejected((prev) => {
      const next = new Map(prev)
      if (next.has(id)) next.delete(id)
      else next.set(id, '')
      return next
    })
  const onFeedback = (id, val) =>
    setRejected((prev) => new Map(prev).set(id, val))

  if (review?.gate !== 'outcomes') return null

  const outcomes = review.outcomes || []
  const reviews = review.reviews || {}
  const regenIds = new Set(review.regenerated_ids || [])
  const hasRegen = regenIds.size > 0
  const regenerated = outcomes.filter((o) => regenIds.has(o.id))
  const kept = outcomes.filter((o) => !regenIds.has(o.id))

  // Regeneration is allowed only when every unchecked outcome has a non-empty reason.
  const canRegen = rejected.size > 0 && [...rejected.values()].every((v) => v.trim())

  function approve() { onDecide({ action: 'approve' }) }
  function regenerate() {
    onDecide({
      action: 'reject',
      rejected: [...rejected.entries()].map(([id, feedback]) => ({ id, feedback: feedback.trim() })),
    })
  }

  const listProps = { reviews, rejected, onToggle, onFeedback, busy }

  return (
    <div className="mcq-review">
      <div className="mcq-review-head">
        <div>
          <h3>Review the learning outcomes</h3>
          <p>
            {hasRegen
              ? 'Review the regenerated outcomes on the left. Uncheck any (with a reason) to regenerate again, or approve to continue.'
              : 'Uncheck any outcome and say why — it will be regenerated with your feedback. Approve to generate questions.'}
          </p>
        </div>
        <span className="mcq-badge warn">awaiting review</span>
      </div>

      {hasRegen ? (
        <div className="mcq-review-cols">
          <div className="mcq-review-col">
            <span className="mcq-spec-k">Regenerated ({regenerated.length}) — review these</span>
            <OutcomeList outcomes={regenerated} {...listProps} />
          </div>
          <div className="mcq-review-col">
            <span className="mcq-spec-k">Previously approved ({kept.length})</span>
            <OutcomeList outcomes={kept} {...listProps} />
          </div>
        </div>
      ) : (
        <div className="mcq-review-section">
          <span className="mcq-spec-k">
            Learning outcomes ({outcomes.length}) — uncheck any to regenerate with feedback
          </span>
          <OutcomeList outcomes={outcomes} {...listProps} />
        </div>
      )}

      <div className="mcq-review-actions">
        <button
          type="button"
          className="btn btn-ghost"
          disabled={busy || !canRegen}
          onClick={regenerate}
          title={rejected.size === 0
            ? 'Uncheck an outcome to regenerate it'
            : canRegen ? '' : 'Add a reason for every unchecked outcome'}
        >
          <RotateCcw size={14} /> {`Regenerate ${rejected.size || ''}`.trim()}
        </button>
        <button type="button" className="btn btn-primary" disabled={busy} onClick={approve}>
          <ThumbsUp size={14} /> Approve &amp; continue
        </button>
      </div>
    </div>
  )
}

export default McqReviewGate
