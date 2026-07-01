import { useState } from 'react'
import { CheckCircle2, AlertTriangle, RotateCcw, ThumbsUp, Plus } from 'lucide-react'

// The human-in-the-loop review gate (the LO-division Gate 1 was removed). It renders the paused
// pipeline's authored outcomes. The reviewer rates EACH outcome — Good / Needs work / Regenerate —
// and may add a comment on any of them. EVERY rating + comment is stored (stage='lo' feedback) on
// submit, regardless of approve/reject, building the LO-feedback dataset used to revamp prompts.
// Outcomes marked "Regenerate" (reason required) are rewritten with their comment as feedback;
// if none are, the run continues. After a regen round, regenerated_ids split the view into two
// columns: LEFT = just regenerated (review these), RIGHT = previously kept.

const VERDICTS = [
  { key: 'good', label: 'Good' },
  { key: 'needs_work', label: 'Needs work' },
  { key: 'regenerate', label: 'Regenerate' },
]

function VerdictPicker({ value, disabled, onChange }) {
  return (
    <div className="mcq-verdict-picker" role="group">
      {VERDICTS.map((v) => (
        <button
          key={v.key}
          type="button"
          disabled={disabled}
          className={`btn btn-ghost mcq-verdict v-${v.key} ${value === v.key ? 'active' : ''}`}
          aria-pressed={value === v.key}
          onClick={() => onChange(v.key)}
        >
          {v.label}
        </button>
      ))}
    </div>
  )
}

function OutcomeRow({ o, rv, verdict, comment, onVerdict, onComment, busy,
                     removable = false, isRemoved = false, onToggleRemove }) {
  const failed = rv?.covered === false
  const regen = verdict === 'regenerate'
  return (
    <li className={`mcq-lo-item ${regen ? 'rejecting' : ''} ${isRemoved ? 'removed' : ''}`}>
      <div className="mcq-lo-main">
        {removable && (
          <label className="mcq-lo-keep"
            title={isRemoved ? 'Dropped — tick to keep it' : 'Untick to drop this outcome and free a slot'}>
            <input type="checkbox" checked={!isRemoved} disabled={busy} onChange={() => onToggleRemove(o.id)} />
          </label>
        )}
        <span className={`mcq-lo-bloom b-${o.bloom_level}`}>{o.bloom_level}</span>
        <span className="mcq-lo-desc">{o.title || o.description}</span>
        {failed ? (
          <span className="mcq-badge warn"><AlertTriangle size={11} /> rubric</span>
        ) : (
          <span className="mcq-badge ok"><CheckCircle2 size={11} /> ok</span>
        )}
      </div>
      {!isRemoved && (
        <>
          <div className="mcq-lo-meta">
            <span className="mcq-lo-tag">concept: {(o.concept_id || '').replace(/^C_/, '')}</span>
            {o.learner_action && <span className="mcq-lo-tag">verb: {o.learner_action}</span>}
            {o.question_type && (
              <span className="mcq-lo-tag mcq-lo-qtype" title={o.question_type_rationale || ''}>
                {o.question_type.replaceAll('_', ' ').toLowerCase()}
              </span>
            )}
            {failed && rv?.fail_reason && (
              <span className="mcq-lo-tag mcq-review-fail">{rv.fail_reason}</span>
            )}
          </div>
          <VerdictPicker value={verdict} disabled={busy} onChange={(v) => onVerdict(o.id, v)} />
          <textarea
            className="input mcq-lo-feedback"
            rows={2}
            disabled={busy}
            value={comment}
            onChange={(e) => onComment(o.id, e.target.value)}
            placeholder={regen
              ? 'Why regenerate? (required) — this feedback drives the rewrite'
              : 'Optional feedback on this outcome (stored for prompt tuning)'}
          />
        </>
      )}
    </li>
  )
}

function OutcomeList({ outcomes, reviews, stateMap, onVerdict, onComment, busy,
                      removable = false, removed, onToggleRemove }) {
  return (
    <ul className="mcq-lo-list detailed">
      {outcomes.map((o) => {
        const st = stateMap.get(o.id) || { verdict: 'good', comment: '' }
        return (
          <OutcomeRow
            key={o.id}
            o={o}
            rv={(reviews || {})[o.id]}
            verdict={st.verdict}
            comment={st.comment}
            onVerdict={onVerdict}
            onComment={onComment}
            busy={busy}
            removable={removable}
            isRemoved={removable && !!removed?.has(o.id)}
            onToggleRemove={onToggleRemove}
          />
        )
      })}
    </ul>
  )
}

function McqReviewGate({ review, busy, onDecide }) {
  // state: Map<outcomeId, {verdict, comment}>. Default verdict 'good' (so a silent submit still
  // records every outcome as reviewed-good). New outcomes from a regen round default in too.
  const [state, setState] = useState(() => new Map())
  const get = (id) => state.get(id) || { verdict: 'good', comment: '' }
  const setVerdict = (id, verdict) =>
    setState((p) => new Map(p).set(id, { ...get(id), verdict }))
  const setComment = (id, comment) =>
    setState((p) => new Map(p).set(id, { ...get(id), comment }))

  // Overflow: which distinct-uncovered outcomes the reviewer ticked to add (default none).
  const [addSel, setAddSel] = useState(() => new Set())
  const toggleAdd = (id) =>
    setAddSel((p) => {
      const next = new Set(p)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  // Classroom-Quiz SWAP: finalized outcomes the reviewer dropped to free ceiling slots.
  const [removed, setRemoved] = useState(() => new Set())
  const toggleRemove = (id) =>
    setRemoved((p) => {
      const next = new Set(p)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })

  if (review?.gate !== 'outcomes') return null

  const outcomes = review.outcomes || []
  const reviews = review.reviews || {}
  const regenIds = new Set(review.regenerated_ids || [])
  const hasRegen = regenIds.size > 0
  const regenerated = outcomes.filter((o) => regenIds.has(o.id))
  const kept = outcomes.filter((o) => !regenIds.has(o.id))

  const toRegen = outcomes.filter((o) => get(o.id).verdict === 'regenerate')
  const missingReason = toRegen.some((o) => !get(o.id).comment.trim())

  const target = review.target || 20
  // Classroom Quiz: HARD ceiling — adding an overflow LO must SWAP out a finalized one (never grow).
  // MCQ: adding grows the set past the target (a conscious, costed opt-in).
  const strict = !!review.strict_budget
  const ceiling = review.ceiling || target

  // Overflow = distinct concepts that couldn't fit the budget (NOT Bloom-restacks). The reviewer
  // ticks any to add; they're folded into Submit (no separate step). `overflow` is an array on new
  // runs (may be empty); undefined on older runs (legacy count-based fallback).
  const overflow = Array.isArray(review.overflow) ? review.overflow : null
  const keptCount = outcomes.length - (strict ? removed.size : 0)   // finalized kept after drops
  const addRoom = strict ? Math.max(0, ceiling - keptCount) : Infinity   // strict: add only into freed slots
  const atAddCap = strict && addSel.size >= addRoom
  const overflowInvalid = strict && keptCount + addSel.size > ceiling   // never exceed the CQ ceiling
  const canSubmit = !busy && !missingReason && !overflowInvalid

  // Legacy fallback (older runs without the overflow list): count-based reserve promote.
  const reserveAvailable = review.reserve_available || 0
  const legacyAddCount = Math.min(Math.max(0, target - outcomes.length), reserveAvailable)
  const canLegacyAdd = !busy && overflow === null && legacyAddCount > 0

  function submit() {
    const lo_feedback = outcomes.map((o) => {
      const { verdict, comment } = get(o.id)
      return { id: o.id, verdict, comment: comment.trim() }
    })
    const rejected = toRegen.map((o) => ({ id: o.id, feedback: get(o.id).comment.trim() }))
    // Fold the overflow adds (+ strict-mode drops) INTO submit: one step, no separate button — the
    // backend promotes then continues to generation (no re-pause).
    onDecide({
      action: rejected.length ? 'reject' : 'approve',
      rejected,
      lo_feedback,
      add_ids: [...addSel],
      removed_ids: strict ? [...removed] : [],
    })
  }

  function legacyAddMore() {
    onDecide({ action: 'add_more', count: legacyAddCount })
  }

  const listProps = { reviews, stateMap: state, onVerdict: setVerdict, onComment: setComment, busy,
                      removable: strict, removed, onToggleRemove: toggleRemove }

  return (
    <div className="mcq-review">
      <div className="mcq-review-head">
        <div>
          <h3>Review the learning outcomes</h3>
          <p>
            Rate each outcome — <b>Good</b> / <b>Needs work</b> / <b>Regenerate</b> — and add any
            feedback. Every rating and comment is saved. Outcomes marked “Regenerate” (reason
            required) are rewritten with your feedback; otherwise the run continues.
          </p>
        </div>
        <div className="mcq-gate-meta">
          <span className={`mcq-badge ${outcomes.length < target ? 'warn' : 'ok'}`}
            title={outcomes.length < target ? `Below the target of ${target}` : `At/above the target of ${target}`}>
            {outcomes.length} of {target} outcomes
          </span>
          <span className="mcq-badge warn">awaiting review</span>
        </div>
      </div>

      {hasRegen ? (
        <div className="mcq-review-cols">
          <div className="mcq-review-col">
            <span className="mcq-spec-k">Regenerated ({regenerated.length}) — review these</span>
            <OutcomeList outcomes={regenerated} {...listProps} />
          </div>
          <div className="mcq-review-col">
            <span className="mcq-spec-k">Previously kept ({kept.length})</span>
            <OutcomeList outcomes={kept} {...listProps} />
          </div>
        </div>
      ) : (
        <div className="mcq-review-section">
          <span className="mcq-spec-k">Learning outcomes ({outcomes.length})</span>
          <OutcomeList outcomes={outcomes} {...listProps} />
        </div>
      )}

      {overflow && overflow.length > 0 && (
        <div className="mcq-overflow">
          <div className="mcq-overflow-head">
            <span className="mcq-spec-k">
              {overflow.length} distinct concept{overflow.length === 1 ? '' : 's'} didn’t fit the budget
            </span>
            <span className="mcq-overflow-sub">
              These are taught concepts the {ceiling}-outcome budget couldn’t cover — <b>not</b> Bloom
              variants of ones already listed.{' '}
              {strict
                ? <>Classroom Quiz is capped at {ceiling}: to add one, <b>untick a finalized outcome above</b> to free a slot (a swap). They’re applied when you Submit.</>
                : <>Tick any to add — they’re included when you <b>Submit</b>. Adding raises the question count beyond {ceiling} (more questions = more cost). Leave all unticked to keep {ceiling}.</>}
            </span>
          </div>
          <ul className="mcq-overflow-list">
            {overflow.map((r) => {
              const on = addSel.has(r.id)
              return (
                <li key={r.id} className={`mcq-overflow-item ${on ? 'on' : ''}`}>
                  <label className="mcq-overflow-check"
                    title={!on && atAddCap ? `Drop a finalized outcome first (cap ${ceiling})` : undefined}>
                    <input type="checkbox" checked={on} disabled={busy || (!on && atAddCap)}
                      onChange={() => toggleAdd(r.id)} />
                  </label>
                  <div className="mcq-overflow-main">
                    <div className="mcq-overflow-title-row">
                      <span className={`mcq-lo-bloom b-${r.bloom_level}`}>{r.bloom_level}</span>
                      <span className="mcq-overflow-title">{r.title || r.sub_concept}</span>
                    </div>
                    <div className="mcq-lo-meta">
                      <span className="mcq-lo-tag">concept: {(r.concept || r.concept_id || '').replace(/^C_/, '')}</span>
                      {r.reason && <span className="mcq-lo-tag mcq-overflow-why">{r.reason}</span>}
                    </div>
                  </div>
                </li>
              )
            })}
          </ul>
          <div className="mcq-overflow-foot">
            <span className={`mcq-badge ${overflowInvalid ? 'warn' : (addSel.size ? 'ok' : '')}`}>
              {strict
                ? `${keptCount} kept + ${addSel.size} added = ${keptCount + addSel.size} / ${ceiling}`
                : `${outcomes.length} + ${addSel.size} = ${outcomes.length + addSel.size} outcomes`}
            </span>
            {overflowInvalid ? (
              <span className="mcq-overflow-why">Over the {ceiling} cap — untick a finalized outcome to add this many.</span>
            ) : addSel.size > 0 ? (
              <span className="mcq-overflow-why">Applied when you Submit ↓</span>
            ) : null}
          </div>
        </div>
      )}

      <div className="mcq-review-actions">
        {canLegacyAdd && (
          <button
            type="button"
            className="btn btn-ghost"
            disabled={busy}
            onClick={legacyAddMore}
            title={`Promote ${legacyAddCount} more already-authored outcome${legacyAddCount > 1 ? 's' : ''} toward the target of ${target}`}
          >
            <Plus size={14} /> {`Add ${legacyAddCount} more outcome${legacyAddCount > 1 ? 's' : ''}`}
          </button>
        )}
        <button
          type="button"
          className="btn btn-primary"
          disabled={!canSubmit}
          onClick={submit}
          title={missingReason ? 'Add a reason for every outcome marked “Regenerate”' : ''}
        >
          {toRegen.length ? (
            <><RotateCcw size={14} /> {`Submit — regenerate ${toRegen.length} & continue`}</>
          ) : addSel.size ? (
            <><ThumbsUp size={14} /> {strict
              ? `Submit — swap in ${addSel.size} & continue`
              : `Submit — add ${addSel.size} & generate ${outcomes.length + addSel.size}`}</>
          ) : (
            <><ThumbsUp size={14} /> Submit review & continue</>
          )}
        </button>
      </div>
    </div>
  )
}

export default McqReviewGate
