import { useEffect, useMemo, useState } from 'react'
import {
  CheckCircle2, AlertTriangle, ListChecks, Activity, ChevronRight,
  FileQuestion, Code2, ToggleLeft, ArrowDownUp, Type, RotateCcw, Check, X, ShieldCheck,
} from 'lucide-react'
import { Spinner } from './ui'
import { useToast } from './Toast'
import NodeSnapshot from './NodeSnapshot'
import { regenerateMcqQuestion, submitMcqFeedback, approveMcqRun, getMcqTrace } from '../api'
import ReactMarkdown from 'react-markdown'

const REVIEW_TAGS = ['grounding', 'ambiguous', 'weak distractor', 'wrong answer', 'LO drift', 'too easy', 'too hard']

const LETTERS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']

// Markdown previewer — the portal renders the question text + explanation as Markdown, so we show
// the same rendered preview here. Options / code / exact answers stay literal (rendered elsewhere)
// so code and program output are never mangled by Markdown.
function Md({ children }) {
  const text = typeof children === 'string' ? children : (children ?? '')
  if (!text.trim()) return null
  return <div className="md"><ReactMarkdown>{text}</ReactMarkdown></div>
}

// Inline Markdown — for option text, list items, and other content that sits INSIDE a flex row.
// Paragraphs render as <span> (not block <p>) so inline code / bold / emphasis work without
// breaking the layout. Reuses the `.md code` / `.md strong` styles.
function MdInline({ children }) {
  const text = typeof children === 'string' ? children : (children ?? '')
  if (!text.trim()) return null
  return (
    <span className="md md-inline">
      <ReactMarkdown components={{ p: ({ node, ...props }) => <span {...props} /> }}>
        {text}
      </ReactMarkdown>
    </span>
  )
}

const TYPE_ICON = {
  MULTIPLE_CHOICE: FileQuestion,
  MORE_THAN_ONE_MULTIPLE_CHOICE: ListChecks,
  TRUE_OR_FALSE: ToggleLeft,
  TEXTUAL: Type,
  CODE_ANALYSIS_MULTIPLE_CHOICE: Code2,
  CODE_ANALYSIS_MORE_THAN_ONE_MULTIPLE_CHOICE: Code2,
  CODE_ANALYSIS_TEXTUAL: Code2,
  FIB_CODING: Code2,
  REARRANGE: ArrowDownUp,
}

function Field({ label, children }) {
  return (
    <section className="qc-field">
      <div className="qc-field-label">{label}</div>
      {children}
    </section>
  )
}

// Compact, human-readable summary of one recorded RAG call.
function ragArg(c) {
  const a = c.args || {}
  const base = a.topic || a.query || a.concept || ''
  return a.syntax ? `${base} · ${a.syntax}` : base
}
function ragResult(c) {
  const r = c.result || {}
  if (c.tool === 'check_concept' || c.tool === 'code_coverage') {
    return (r.verdict || '').split('\n')[0] || (r.covered ? 'covered' : 'not covered')
  }
  if (c.tool === 'search_reading_material') return `${r.hits ?? 0} hits`
  if (c.tool === 'find_prerequisites') return `${(r.prerequisites || []).length} prerequisite(s)`
  return ''
}
function ragVerdictClass(c) {
  const v = ((c.result || {}).verdict || '').toUpperCase()
  if (v.includes('NOT EXPLAINED')) return 'bad'
  if (v.includes('PARTIALLY')) return 'warn'
  if (v.includes('EXPLAINED')) return 'ok'
  return ''
}

// Every RAG call made while generating + reviewing one question — so the user can
// see exactly what was checked against the course material (and what wasn't taught).
function RagCalls({ calls }) {
  if (!calls.length) return null
  return (
    <details className="qc-rag">
      <summary>RAG calls <span className="qc-rag-n">{calls.length}</span></summary>
      <ul className="qc-rag-list">
        {calls.map((c, i) => (
          <li key={i} className="qc-rag-call">
            <span className="qc-rag-tool">{c.tool}</span>
            <span className="qc-rag-arg">{ragArg(c)}</span>
            <span className={`qc-rag-res ${ragVerdictClass(c)}`}>{ragResult(c)}</span>
          </li>
        ))}
      </ul>
    </details>
  )
}

// One generated question — all details laid out in clearly-padded sections.
// Per-question reviewer actions: Accept, or Reject + feedback → regenerate.
function QuestionReview({ q, busy, onAccept, onRegenerate }) {
  const [open, setOpen] = useState(false)
  const [fb, setFb] = useState('')
  const [tags, setTags] = useState([])
  const toggleTag = (t) => setTags((s) => (s.includes(t) ? s.filter((x) => x !== t) : [...s, t]))

  if (q._reviewState === 'accepted') {
    return <div className="qc-review-bar"><span className="qc-rev-accepted"><Check size={13} /> accepted</span></div>
  }
  return (
    <div className="qc-review-bar">
      {!open ? (
        <>
          <button className="btn btn-soft btn-sm" disabled={busy} onClick={onAccept}>
            <Check size={13} /> Accept
          </button>
          <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => setOpen(true)}>
            <RotateCcw size={13} /> Reject &amp; regenerate
          </button>
        </>
      ) : (
        <div className="qc-rev-form">
          <textarea
            className="input qc-rev-text" rows={2} value={fb} spellCheck
            placeholder="What's wrong? This feedback is saved and injected when the question is regenerated."
            onChange={(e) => setFb(e.target.value)}
          />
          <div className="qc-rev-tags">
            {REVIEW_TAGS.map((t) => (
              <button key={t} type="button"
                className={`mcq-chip ${tags.includes(t) ? 'active' : ''}`}
                onClick={() => toggleTag(t)}>{t}</button>
            ))}
          </div>
          <div className="qc-rev-actions">
            <button className="btn btn-primary btn-sm" disabled={busy || !fb.trim()}
              onClick={() => onRegenerate(fb, tags)}>
              {busy ? <Spinner size={13} /> : <RotateCcw size={13} />} Regenerate
            </button>
            <button className="btn btn-ghost btn-sm" disabled={busy}
              onClick={() => { setOpen(false); setFb(''); setTags([]) }}>
              <X size={13} /> Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function QuestionCard({ q, lo, index, review }) {
  const generated = q.status === 'generated'
  const lean = q.lean || {}
  const issues = q.review?.issues || []
  const Icon = TYPE_ICON[q.question_type] || FileQuestion
  const code = lean.code || (lean.code_lines ? lean.code_lines.join('\n') : null)
  const ragCalls = [...(q.rag_calls || []), ...(q.review_rag_calls || [])]

  // Answer chips that apply to this type.
  const answers = []
  if (typeof lean.is_true === 'boolean') answers.push(['Answer', lean.is_true ? 'True' : 'False'])
  if (lean.answer) answers.push(['Answer', lean.answer, true])
  if (lean.blank_answer) answers.push(['Blank fills with', lean.blank_answer, true])
  if (lean.correct_output) answers.push(['Output', lean.correct_output, true])
  if (lean.expected_output) answers.push(['Expected output', lean.expected_output, true])
  if (lean.test_output) answers.push(['Test output', lean.test_output, true])
  ;(lean.correct_outputs || []).forEach((c) => answers.push(['Correct', c]))

  return (
    <article className={`qc ${q.needs_human ? 'flagged' : ''}`}>
      <header className="qc-top">
        <span className="qc-num">{index + 1}</span>
        <span className="qc-type"><Icon size={12} /> {q.question_type.replaceAll('_', ' ')}</span>
        {q.difficulty && <span className={`qc-diff d-${q.difficulty.toLowerCase()}`}>{q.difficulty}</span>}
        <span className="qc-spacer" />
        {!generated ? (
          <span className="qc-badge skip">{q.status}</span>
        ) : q.needs_human ? (
          <span className="qc-badge warn"><AlertTriangle size={12} /> needs review</span>
        ) : (
          <span className="qc-badge ok"><CheckCircle2 size={12} /> passed</span>
        )}
      </header>

      <div className="qc-tests">
        <span className="qc-tests-k">Tests outcome</span>
        <span className="qc-tests-v">{lo?.description || lo?.outcome || q.outcome}</span>
      </div>

      {q.fallback && (
        <div className="qc-note">
          <AlertTriangle size={12} /> Re-routed from {q.fallback.from} — {q.fallback.reason}.
        </div>
      )}

      {!generated ? (
        <Field label="Status"><p className="qc-muted">{q.reason || 'Not generated.'}</p></Field>
      ) : (
        <>
          <Field label="Question">
            <div className="qc-stem"><Md>{lean.question || lean.statement}</Md></div>
            {code && <pre className="qc-code">{code}</pre>}
          </Field>

          {Array.isArray(lean.options) && lean.options.length > 0 && (
            <Field label="Options">
              <ul className="qc-opts">
                {lean.options.map((o, i) => (
                  <li key={i} className={`qc-opt ${o.is_correct ? 'correct' : ''}`}>
                    <span className="qc-opt-letter">{LETTERS[i] || '•'}</span>
                    <span className="qc-opt-text"><MdInline>{o.content}</MdInline></span>
                    {o.is_correct && <CheckCircle2 size={14} className="qc-opt-tick" />}
                  </li>
                ))}
              </ul>
            </Field>
          )}

          {Array.isArray(lean.ordered_items) && lean.ordered_items.length > 0 && (
            <Field label="Correct order">
              <ol className="qc-rearrange">
                {lean.ordered_items.map((it, i) => <li key={i}><MdInline>{it}</MdInline></li>)}
              </ol>
            </Field>
          )}

          {answers.length > 0 && (
            <Field label="Answer">
              <div className="qc-answers">
                {answers.map(([k, v, mono], i) => (
                  <span key={i} className="qc-answer">
                    <span className="qc-answer-k">{k}</span>
                    {mono ? <code>{v}</code> : <strong>{v}</strong>}
                  </span>
                ))}
              </div>
            </Field>
          )}

          {lean.explanation && (
            <Field label="Explanation"><div className="qc-expl"><Md>{lean.explanation}</Md></div></Field>
          )}
        </>
      )}

      {(q.attempts > 0 || issues.length > 0 || q.review?.summary) && (
        <Field label={`Review${q.attempts ? ` · ${q.attempts} fix attempt${q.attempts === 1 ? '' : 's'}` : ''}`}>
          {q.review?.summary && <p className="qc-review-summary">{q.review.summary}</p>}
          {issues.map((iss, i) => (
            <div key={i} className="qc-issue">
              <span className={`qc-sev ${iss.severity}`}>{iss.severity}</span>
              <span className="qc-issue-text">
                <strong>{iss.rule}:</strong> {iss.problem}
                {iss.suggested_fix ? ` — fix: ${iss.suggested_fix}` : ''}
              </span>
            </div>
          ))}
        </Field>
      )}

      {ragCalls.length > 0 && (
        <Field label="Grounding"><RagCalls calls={ragCalls} /></Field>
      )}

      {review}
    </article>
  )
}

// Frozen-spec summary: status, hash, effective Bloom split, override log, and the
// per-rule (V1–V11) validation report from the deterministic LO pipeline.
function SpecPanel({ artifact, overrides, escalation }) {
  const rep = artifact.validation_report || {}
  const rules = Object.entries(rep)
  const split = artifact.effective_bloom_split || {}
  const hash = (artifact.spec_hash || '').replace('sha256:', '').slice(0, 16)
  return (
    <div className="mcq-spec">
      <div className="mcq-spec-grid">
        <div className="mcq-spec-row">
          <span className="mcq-spec-k">Status</span>
          <span className={`mcq-spec-status ${artifact.status === 'FROZEN' ? 'ok' : 'warn'}`}>
            {artifact.status === 'FROZEN'
              ? <CheckCircle2 size={13} /> : <AlertTriangle size={13} />} {artifact.status || '—'}
          </span>
        </div>
        {hash && (
          <div className="mcq-spec-row"><span className="mcq-spec-k">Spec hash</span><code>{hash}…</code></div>
        )}
        <div className="mcq-spec-row">
          <span className="mcq-spec-k">Bloom split</span>
          <span>{['remember', 'understand', 'apply', 'scenario'].map(t => `${split[t] ?? 0} ${t}`).join(' · ')}</span>
        </div>
      </div>

      {overrides?.length > 0 && (
        <div className="mcq-overrides">
          {overrides.map((o, i) => (
            <div key={i} className="mcq-override">
              <AlertTriangle size={12} /> <strong>{o.rule}</strong>: {o.from} → {o.to} — {o.reason}
            </div>
          ))}
        </div>
      )}

      {rules.length > 0 && (
        <div className="mcq-rules">
          {rules.map(([rid, v]) => (
            <span key={rid} className={`mcq-rule ${v.pass ? 'ok' : 'fail'}`}
              title={`${v.detail || ''}${(v.failing || []).length ? ' · ' + JSON.stringify(v.failing) : ''}`}>
              {v.pass ? <CheckCircle2 size={11} /> : <AlertTriangle size={11} />} {rid}
            </span>
          ))}
        </div>
      )}

      {escalation && (
        <div className="mcq-escalation">
          <AlertTriangle size={13} /> Escalated after {escalation.retry_count} repair attempt(s);
          unresolved rules: {(escalation.failed_rules || []).join(', ') || '—'}.
        </div>
      )}
    </div>
  )
}

// Full result of one MCQ run: stat tiles + filterable question list + LOs + spec.
function fmtMs(ms) {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`
}

// Node-by-node execution trace (our own tracing, replacing LangSmith). Fetched by the run's job
// id; a node re-run by the repair loop / HITL resume appears more than once, in chronological order.
function TracePanel({ jobId }) {
  const [spans, setSpans] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    let cancelled = false
    getMcqTrace(jobId)
      .then((rows) => {
        if (!cancelled) setSpans(rows || [])
      })
      .catch((e) => {
        if (!cancelled) setErr(e.message)
      })
    return () => {
      cancelled = true
    }
  }, [jobId])

  if (err) return <p className="muted">Could not load trace: {err}</p>
  if (spans === null)
    return (
      <div className="mcq-loading">
        <Spinner size={14} /> Loading trace…
      </div>
    )
  if (spans.length === 0) return <p className="muted">No trace recorded for this run.</p>

  const total = spans.reduce((n, s) => n + (s.duration_ms || 0), 0)
  const max = Math.max(1, ...spans.map((s) => s.duration_ms || 0))
  return (
    <div className="mcq-trace">
      <div className="mcq-trace-head">
        <span className="mcq-spec-k">{spans.length} node steps</span>
        <span className="mcq-lo-tag">total {fmtMs(total)}</span>
        <span className="mcq-lo-tag">click a step to inspect its state &amp; LLM I/O</span>
      </div>
      <ol className="mcq-trace-list">
        {spans.map((s, i) => (
          <TraceRow key={i} span={s} max={max} />
        ))}
      </ol>
    </div>
  )
}

// One trace span: a clickable header (node · duration · detail) that expands to show the node's
// captured STATE DETAILS and the LLM calls (prompt + response) it made.
function TraceRow({ span: s, max }) {
  const [open, setOpen] = useState(false)
  const snap = s.snapshot || {}
  const nCalls = Array.isArray(snap.llm_calls) ? snap.llm_calls.length : 0
  const hasDetails = Object.keys(snap).length > 0
  return (
    <li className={`mcq-trace-item ${s.status === 'error' ? 'err' : ''} ${open ? 'open' : ''}`}>
      <div
        className={`mcq-trace-row ${hasDetails ? 'clickable' : ''}`}
        onClick={hasDetails ? () => setOpen((o) => !o) : undefined}
        role={hasDetails ? 'button' : undefined}
      >
        <span className="mcq-trace-node">
          {hasDetails && <ChevronRight size={12} className="mcq-trace-chevron" />}
          {s.status === 'error' && <AlertTriangle size={11} />} {s.label || s.node}
        </span>
        <span className="mcq-trace-bar">
          <span
            className="mcq-trace-fill"
            style={{ width: `${Math.round(((s.duration_ms || 0) / max) * 100)}%` }}
          />
        </span>
        <span className="mcq-trace-ms">{fmtMs(s.duration_ms || 0)}</span>
        {nCalls > 0 && <span className="mcq-trace-llm">{nCalls} LLM</span>}
        {s.detail && <span className="mcq-trace-detail">{s.detail}</span>}
      </div>
      {open && hasDetails && (
        <div className="mcq-trace-snapshot">
          <NodeSnapshot snapshot={snap} />
        </div>
      )}
    </li>
  )
}

function McqResults({ run }) {
  const toast = useToast()
  const r = run?.result || {}
  const [questions, setQuestions] = useState(r.questions || [])
  const [reviewer, setReviewer] = useState('')
  const [busyOutcome, setBusyOutcome] = useState(null)
  const [approved, setApproved] = useState(run?.review_status === 'approved')
  // State is seeded from `run` on mount; the parent passes key={run.id} so a
  // different run remounts this component (no setState-in-effect needed).

  async function handleRegenerate(outcome, feedback, tags) {
    setBusyOutcome(outcome)
    try {
      const { question } = await regenerateMcqQuestion(run.id, outcome, feedback, tags, reviewer)
      setQuestions((qs) => qs.map((q) => (q.outcome === outcome ? question : q)))
      toast.push({ kind: 'success', title: 'Question regenerated', message: `${outcome} updated from your feedback` })
    } catch (e) {
      toast.push({ kind: 'error', title: 'Regenerate failed', message: e.message })
    } finally {
      setBusyOutcome(null)
    }
  }
  async function handleAccept(outcome) {
    try {
      await submitMcqFeedback(run.id, outcome, { action: 'accept', reviewer })
      setQuestions((qs) => qs.map((q) => (q.outcome === outcome ? { ...q, _reviewState: 'accepted' } : q)))
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not save feedback', message: e.message })
    }
  }
  async function handleApprove() {
    try {
      await approveMcqRun(run.id, reviewer)
      setApproved(true)
      toast.push({ kind: 'success', title: 'Run approved', message: 'Questions are ready to export.' })
    } catch (e) {
      toast.push({ kind: 'error', title: 'Approve failed', message: e.message })
    }
  }

  const los = r.final_los || []
  const notes = r.notes || []
  const jobId = run?.job_id
  const artifact = r.artifact || {}
  const status = artifact.status || r.lo_status || ''
  const overrides = r.overrides || artifact.overrides || []
  const escalation = r.escalation || artifact.escalation || null
  const hasSpec = Boolean(artifact.status || (artifact.validation_report && Object.keys(artifact.validation_report).length))

  const loByOutcome = useMemo(
    () => Object.fromEntries((r.final_los || []).map((lo) => [lo.outcome, lo])), [r.final_los],
  )
  const needsReview = questions.filter((q) => q.needs_human).length
  const ragCallCount = useMemo(
    () => (r.questions || []).reduce(
      (n, q) => n + (q.rag_calls?.length || 0) + (q.review_rag_calls?.length || 0), 0),
    [r.questions],
  )

  const [tab, setTab] = useState('questions')
  const [filter, setFilter] = useState('all') // all | review

  const shown = filter === 'review' ? questions.filter((q) => q.needs_human) : questions

  return (
    <div className="mcq-results">
      {status === 'NEEDS_REVIEW' && (
        <div className="mcq-banner warn">
          <AlertTriangle size={15} />
          <span>
            Learning outcomes <strong>need review</strong> — the spec was frozen as
            <code> NEEDS_REVIEW</code> after the repair loop.
            {escalation?.failed_rules?.length ? ` Unresolved: ${escalation.failed_rules.join(', ')}.` : ''}
          </span>
        </div>
      )}

      <div className="mcq-tiles">
        <div className="mcq-tile"><div className="mcq-tile-num">{run?.lo_count ?? los.length}</div><div className="mcq-tile-lbl">Outcomes</div></div>
        <div className="mcq-tile"><div className="mcq-tile-num">{run?.question_count ?? 0}</div><div className="mcq-tile-lbl">Questions</div></div>
        <div className="mcq-tile warn"><div className="mcq-tile-num">{run?.needs_human_count ?? needsReview}</div><div className="mcq-tile-lbl">Need review</div></div>
        {ragCallCount > 0 && (
          <div className="mcq-tile"><div className="mcq-tile-num">{ragCallCount}</div><div className="mcq-tile-lbl">RAG calls</div></div>
        )}
        <div className="mcq-tile-actions">
          {status && (
            <span className={`mcq-status-chip ${status === 'FROZEN' ? 'ok' : 'warn'}`}>
              {status === 'FROZEN' ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />} {status}
            </span>
          )}
          {!r.ingested && (
            <span className="mcq-mode" data-tip="Course not ingested — grounded on the session reading material only">
              reading-material mode
            </span>
          )}
        </div>
      </div>

      <div className="mcq-tabs">
        <button className={`mcq-tab ${tab === 'questions' ? 'active' : ''}`} onClick={() => setTab('questions')}>
          Questions <span className="mcq-tab-n">{questions.length}</span>
        </button>
        <button className={`mcq-tab ${tab === 'los' ? 'active' : ''}`} onClick={() => setTab('los')}>
          Learning outcomes <span className="mcq-tab-n">{los.length}</span>
        </button>
        {hasSpec && (
          <button className={`mcq-tab ${tab === 'spec' ? 'active' : ''}`} onClick={() => setTab('spec')}>
            Spec {status && <span className={`mcq-tab-n ${status === 'FROZEN' ? '' : 'warn'}`}>{status === 'FROZEN' ? '✓' : '!'}</span>}
          </button>
        )}
        {jobId && (
          <button className={`mcq-tab ${tab === 'trace' ? 'active' : ''}`} onClick={() => setTab('trace')}>
            <Activity size={12} /> Trace
          </button>
        )}
      </div>

      {tab === 'questions' && (
        <>
          <div className="mcq-toolbar">
            <div className="mcq-filters">
              <button className={`mcq-chip ${filter === 'all' ? 'active' : ''}`} onClick={() => setFilter('all')}>
                All
              </button>
              <button className={`mcq-chip ${filter === 'review' ? 'active' : ''}`} onClick={() => setFilter('review')}>
                Needs review {needsReview > 0 && <span className="mcq-chip-n">{needsReview}</span>}
              </button>
            </div>
            <div className="mcq-review-tools">
              <input className="input mcq-reviewer" placeholder="Your name (reviewer)"
                value={reviewer} spellCheck={false} onChange={(e) => setReviewer(e.target.value)} />
              {approved ? (
                <span className="mcq-status-chip ok"><ShieldCheck size={12} /> approved</span>
              ) : (
                <button className="btn btn-primary btn-sm" onClick={handleApprove}>
                  <ShieldCheck size={13} /> Approve run
                </button>
              )}
            </div>
          </div>
          <div className="qc-list">
            {shown.map((q) => (
              <QuestionCard key={q.outcome} q={q} lo={loByOutcome[q.outcome]}
                index={questions.indexOf(q)}
                review={(
                  <QuestionReview
                    q={q} busy={busyOutcome === q.outcome}
                    onAccept={() => handleAccept(q.outcome)}
                    onRegenerate={(fb, tags) => handleRegenerate(q.outcome, fb, tags)}
                  />
                )} />
            ))}
            {shown.length === 0 && <p className="muted">No questions in this filter.</p>}
          </div>
        </>
      )}

      {tab === 'los' && (
        <ul className="mcq-lo-list detailed">
          {los.map((lo, i) => {
            const prereqs = lo.in_session_prerequisites || []
            return (
              <li key={i} className="mcq-lo-item">
                <div className="mcq-lo-main">
                  <span className={`mcq-lo-bloom b-${(lo.bloom_category || '').toLowerCase()}`}>{lo.bloom_category}</span>
                  <span className="mcq-lo-desc">{lo.description || lo.outcome}</span>
                  {lo.question_type && <span className="mcq-lo-type">{lo.question_type.replaceAll('_', ' ')}</span>}
                </div>
                {(lo.concept || lo.syntax || lo.prerequisite_scope || prereqs.length > 0) && (
                  <div className="mcq-lo-meta">
                    {lo.concept && <span className="mcq-lo-tag">concept: {lo.concept}</span>}
                    {lo.syntax && <code className="mcq-lo-syntax">{lo.syntax}</code>}
                    {lo.prerequisite_scope && (
                      <span className={`mcq-lo-scope ${lo.prerequisite_scope}`}>
                        {lo.prerequisite_scope.replaceAll('_', ' ')}
                      </span>
                    )}
                    {prereqs.length > 0 && (
                      <span className="mcq-lo-tag">prereqs: {prereqs.join(', ')}</span>
                    )}
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}

      {tab === 'spec' && hasSpec && (
        <SpecPanel artifact={artifact} overrides={overrides} escalation={escalation} />
      )}

      {tab === 'trace' && jobId && <TracePanel jobId={jobId} />}

      {notes.length > 0 && (
        <div className="mcq-notes">{notes.map((n, i) => <span key={i}>{n}</span>)}</div>
      )}
    </div>
  )
}

export default McqResults
