import { useEffect, useMemo, useRef, useState } from 'react'
import {
  CheckCircle2, AlertTriangle, ListChecks, Activity, ChevronRight,
  FileQuestion, Code2, ToggleLeft, ArrowDownUp, Type, RotateCcw, Check, X, ShieldCheck, Download,
  FileSpreadsheet, ExternalLink, Ban, Undo2, Play,
} from 'lucide-react'
import { Spinner } from './ui'
import { useToast } from './Toast'
import { useAuth } from '../auth/AuthContext'
import Modal from './Modal'
import NodeSnapshot from './NodeSnapshot'
import { regenerateMcqQuestion, approveMcqRun, exportMcqRunZip, prepareAndLoadMcqRun, getMcqTrace, setMcqQuestionApproval, setMcqQuestionExclusion, getJob, getMcqRun, executeCode } from '../api'
import ReactMarkdown from 'react-markdown'
import ReadingMaterialPane from './ReadingMaterialPane'

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

// Poll a background job until it settles (or times out). Used to follow a regeneration
// job started by the Review Queue — the question is re-fetched from the run on success.
const JOB_TERMINAL = ['SUCCESS', 'FAILURE', 'CANCELLED']
async function pollJobDone(jobId, { intervalMs = 1500, timeoutMs = 180000 } = {}) {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    await new Promise((r) => setTimeout(r, intervalMs))
    let job = null
    try { job = await getJob(jobId) } catch { /* transient — keep polling */ }
    if (job && JOB_TERMINAL.includes(job.status)) return job
  }
  return null
}

// Per-question reviewer actions (Review Queue only): Approve (persisted, drives the
// load gate), or Reject → feedback → regenerate (which clears the approval server-side).
function QuestionReview({ q, busy, onApprove, onRegenerate, onExclude }) {
  const [open, setOpen] = useState(false)
  const [fb, setFb] = useState('')
  const [tags, setTags] = useState([])
  const toggleTag = (t) => setTags((s) => (s.includes(t) ? s.filter((x) => x !== t) : [...s, t]))
  const approved = q.approval === 'approved'

  // Excluded questions stay visible but only offer "include" (redo) — they're not loaded.
  if (q.excluded) {
    return (
      <div className="qc-review-bar">
        <span className="qc-rev-excluded"><Ban size={13} /> excluded — won't be loaded</span>
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onExclude(false)}>
          <Undo2 size={13} /> Include again
        </button>
      </div>
    )
  }

  if (open) {
    return (
      <div className="qc-review-bar">
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
      </div>
    )
  }
  return (
    <div className="qc-review-bar">
      {approved ? (
        <>
          <span className="qc-rev-accepted"><Check size={13} /> approved</span>
          <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onApprove('pending')}>
            Undo
          </button>
        </>
      ) : (
        <button className="btn btn-soft btn-sm" disabled={busy} onClick={() => onApprove('approved')}>
          <Check size={13} /> Approve
        </button>
      )}
      <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => setOpen(true)}>
        <RotateCcw size={13} /> Reject &amp; regenerate
      </button>
      <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onExclude(true)}
        title="Drop this question from the load — it stays in the list and can be re-included">
        <Ban size={13} /> Exclude
      </button>
    </div>
  )
}

// Shown before an export/load when some questions are excluded: requires the user to
// type "confirm" to proceed with only the remaining questions.
function ConfirmExcludeModal({ excludedCount, proceedingCount, onCancel, onConfirm }) {
  const [text, setText] = useState('')
  const ok = text.trim().toLowerCase() === 'confirm'
  return (
    <Modal title="Proceed without excluded questions?" size="sm" onClose={onCancel}
      footer={(
        <>
          <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button className="btn btn-primary" disabled={!ok} onClick={onConfirm}>
            <Check size={14} /> Continue
          </button>
        </>
      )}>
      <p className="confirm-exclude-msg">
        <strong>{excludedCount}</strong> of <strong>{excludedCount + proceedingCount}</strong>{' '}
        questions are excluded and will <strong>not</strong> be loaded. Proceed with the remaining{' '}
        <strong>{proceedingCount}</strong> question{proceedingCount === 1 ? '' : 's'}?
      </p>
      <p className="confirm-exclude-hint">Type <code>confirm</code> to continue:</p>
      <input className="input" autoFocus value={text} spellCheck={false} placeholder="confirm"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && ok) onConfirm() }} />
    </Modal>
  )
}

// Result of running a candidate program: FIB shows a match/no-match verdict against the
// expected output; code-analysis just shows stdout. Mirrors how the platform grades FIBs.
function ExecResult({ res, isFib }) {
  if (res.error) return <p className="qc-exec-err">{res.error}</p>
  if (res.supported === false)
    return <p className="qc-exec-err">{res.stderr || 'This language is not executable on the server.'}</p>
  const out = (res.actual ?? res.stdout) || ''
  return (
    <div className="qc-exec-out">
      {isFib && (
        <span className={`qc-exec-badge ${res.matched ? 'ok' : 'bad'}`}>
          {res.matched ? <Check size={12} /> : <X size={12} />}
          {res.matched ? 'Output matches expected' : 'Output does NOT match expected'}
        </span>
      )}
      {res.timed_out && <p className="qc-exec-err">Execution timed out.</p>}
      <div className="qc-exec-row"><span className="qc-exec-k">stdout</span><pre className="qc-code">{out || '(no output)'}</pre></div>
      {isFib && !res.matched && res.expected != null && (
        <div className="qc-exec-row"><span className="qc-exec-k">expected</span><pre className="qc-code">{res.expected}</pre></div>
      )}
      {res.stderr && <div className="qc-exec-row"><span className="qc-exec-k">stderr</span><pre className="qc-code qc-exec-stderr">{res.stderr}</pre></div>}
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

  // Reviewer code execution (FIB 'Run & check', code-analysis 'Run code') via the server's
  // sandboxed runner — the same execution that grades FIBs.
  const isFib = q.question_type === 'FIB_CODING'
  const isCodeAnalysis = (q.question_type || '').startsWith('CODE_ANALYSIS')
  const canRun = generated && (isFib || (isCodeAnalysis && !!code))
  const [execBusy, setExecBusy] = useState(false)
  const [execResult, setExecResult] = useState(null)

  const handleRun = async () => {
    setExecBusy(true)
    setExecResult(null)
    try {
      if (isFib) {
        const filled = (lean.code_lines ? lean.code_lines.join('\n') : (lean.code || ''))
          .replaceAll('{{BLANK}}', lean.blank_answer || '')
        setExecResult(await executeCode({
          language: lean.code_language || 'PYTHON',
          code: filled,
          stdin: lean.test_input || '',
          expected_output: lean.test_output || '',
        }))
      } else {
        setExecResult(await executeCode({
          language: lean.code_language || 'PYTHON',
          code: lean.code || code || '',
          stdin: '',
        }))
      }
    } catch (e) {
      setExecResult({ error: e.message })
    } finally {
      setExecBusy(false)
    }
  }

  // Answer chips that apply to this type.
  const answers = []
  if (typeof lean.is_true === 'boolean') answers.push(['Answer', lean.is_true ? 'True' : 'False'])
  if (lean.answer) answers.push(['Answer', lean.answer, true])
  if (lean.blank_answer) answers.push(['Blank fills with', lean.blank_answer, true])
  if (lean.expected_output) answers.push(['Expected output', lean.expected_output, true])
  if (lean.test_output) answers.push(['Test output', lean.test_output, true])

  // Code-analysis MCQs carry their CHOICES as correct_output(s) + wrong_answers (not `options`).
  // Build a unified option list so they render with all choices like a normal MCQ.
  const hasOptions = Array.isArray(lean.options) && lean.options.length > 0
  const codeOptions = []
  if (!hasOptions && Array.isArray(lean.wrong_answers) && lean.wrong_answers.length) {
    if (lean.correct_output) codeOptions.push({ content: lean.correct_output, is_correct: true })
    ;(lean.correct_outputs || []).forEach((c) => codeOptions.push({ content: c, is_correct: true }))
    lean.wrong_answers.forEach((w) => codeOptions.push({ content: w, is_correct: false }))
  }
  const optionList = hasOptions ? lean.options : codeOptions
  if (!codeOptions.length) {          // no synthesized options → show output as an answer chip
    if (lean.correct_output) answers.push(['Output', lean.correct_output, true])
    ;(lean.correct_outputs || []).forEach((c) => answers.push(['Correct', c]))
  }

  return (
    <article className={`qc ${q.needs_human ? 'flagged' : ''} ${q.excluded ? 'excluded' : ''}`}>
      <header className="qc-top">
        <span className="qc-num">{index + 1}</span>
        <span className="qc-type"><Icon size={12} /> {q.question_type.replaceAll('_', ' ')}</span>
        {q.difficulty && <span className={`qc-diff d-${q.difficulty.toLowerCase()}`}>{q.difficulty}</span>}
        <span className="qc-spacer" />
        {q.excluded && generated && (
          <span className="qc-badge skip"><Ban size={12} /> excluded</span>
        )}
        {!generated ? (
          <span className="qc-badge skip">{q.status}</span>
        ) : q.needs_human ? (
          <span className="qc-badge warn"><AlertTriangle size={12} /> needs review</span>
        ) : (
          <span className="qc-badge ok"><CheckCircle2 size={12} /> passed</span>
        )}
        {generated && !q.excluded && q.approval === 'approved' && (
          <span className="qc-badge ok"><Check size={12} /> approved</span>
        )}
      </header>

      <div className="qc-tests">
        <span className="qc-tests-k">Tests outcome</span>
        <span className="qc-tests-v">{lo?.description || lo?.outcome || q.outcome}</span>
      </div>

      <div className="qc-meta-tags">
        {lo?.topic && <span className="qc-tag"><b>Topic</b> {lo.topic}</span>}
        {lo?.concept && <span className="qc-tag"><b>Concept</b> {lo.concept}</span>}
        {lo?.sub_concept && lo.sub_concept !== lo.concept && (
          <span className="qc-tag"><b>Sub-concept</b> {lo.sub_concept}</span>
        )}
        <span className="qc-tag"><b>LO</b> <code>{lo?.outcome || q.outcome}</code></span>
      </div>

      {q.fallback && (
        <div className="qc-note">
          <AlertTriangle size={12} /> Re-routed from {q.fallback.from} — {q.fallback.reason}.
        </div>
      )}

      {q.lo_alignment_note && (
        <div className="qc-note">
          <AlertTriangle size={12} /> {q.lo_alignment_note}
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

          {canRun && (
            <Field label={isFib ? 'Run & check output' : 'Run code'}>
              <div className="qc-exec">
                <button type="button" className="qc-exec-btn" onClick={handleRun} disabled={execBusy}>
                  {execBusy ? <Spinner size={12} /> : <Play size={12} />}
                  {isFib ? 'Fill blank, run & match expected' : 'Run snippet'}
                </button>
                {execResult && <ExecResult res={execResult} isFib={isFib} />}
              </div>
            </Field>
          )}

          {optionList.length > 0 && (
            <Field label="Options">
              <ul className="qc-opts">
                {optionList.map((o, i) => (
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

// `mode`: 'view' (default) shows generation details read-only — used by the generation
// page and the Runs list. 'review' (Review Queue) adds per-question Approve/Reject and the
// approval-gated load controls.
function McqResults({ run, mode = "view", courseId, unitId, onTrackJob }) {
  const review = mode === 'review'
  const toast = useToast()
  const { user } = useAuth()
  const r = run?.result || {}
  const [questions, setQuestions] = useState(r.questions || [])
  const [busyOutcome, setBusyOutcome] = useState(null)
  const [approved, setApproved] = useState(run?.review_status === 'approved')
  const [confirm, setConfirm] = useState(null) // { proceed, proceedingCount } when excludes exist
  const [zipBusy, setZipBusy] = useState(false)
  const [zipResult, setZipResult] = useState(null) // { url, filename } of the last generated export
  const [prepOpen, setPrepOpen] = useState(false)
  const [prepBusy, setPrepBusy] = useState(false)
  const [prepResult, setPrepResult] = useState(null) // { status, message, sheet_url, ... }
  const [prepForm, setPrepForm] = useState({
    child_order: '', duration_min: 30, pass_percentage: 80,
    show_answer_scoring_mode: 'INCORRECT', should_send_solutions: 'yes',
  })
  // State is seeded from `run` on mount; the parent passes key={run.id} so a
  // different run remounts this component (no setState-in-effect needed).

  async function handleRegenerate(outcome, feedback, tags) {
    setBusyOutcome(outcome)
    try {
      // Regeneration now runs as a tracked background job (shows in Activity). Follow it,
      // then pull the freshly persisted question back into the list.
      const job = await regenerateMcqQuestion(run.id, outcome, feedback, tags)
      onTrackJob?.(job)
      const done = await pollJobDone(job.id)
      if (done?.status === 'SUCCESS') {
        const fresh = await getMcqRun(run.id)
        const nq = (fresh?.result?.questions || []).find((q) => q.outcome === outcome)
        if (nq) setQuestions((qs) => qs.map((q) => (q.outcome === outcome ? nq : q)))
        toast.push({ kind: 'success', title: 'Question regenerated', message: `${outcome} updated from your feedback` })
      } else {
        toast.push({
          kind: 'error', title: 'Regenerate failed',
          message: done?.error || 'The regeneration job did not complete.',
        })
      }
    } catch (e) {
      toast.push({ kind: 'error', title: 'Regenerate failed', message: e.message })
    } finally {
      setBusyOutcome(null)
    }
  }
  async function handleSetApproval(outcome, approval) {
    setBusyOutcome(outcome)
    try {
      const res = await setMcqQuestionApproval(run.id, outcome, approval)
      setQuestions((qs) => qs.map((q) => (q.outcome === outcome ? { ...q, approval: res.approval } : q)))
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not save approval', message: e.message })
    } finally {
      setBusyOutcome(null)
    }
  }
  async function handleSetExclusion(outcome, excluded) {
    setBusyOutcome(outcome)
    try {
      const res = await setMcqQuestionExclusion(run.id, outcome, excluded)
      setQuestions((qs) => qs.map((q) => (q.outcome === outcome ? { ...q, excluded: res.excluded } : q)))
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not update exclusion', message: e.message })
    } finally {
      setBusyOutcome(null)
    }
  }
  async function handleApprove() {
    try {
      await approveMcqRun(run.id)
      setApproved(true)
      toast.push({ kind: 'success', title: 'Run approved', message: 'Questions are ready to export.' })
    } catch (e) {
      toast.push({ kind: 'error', title: 'Approve failed', message: e.message })
    }
  }
  async function handleGenerateZip(approvedOnly = false) {
    setZipBusy(true)
    try {
      const { url, filename, total } = await exportMcqRunZip(run.id, approvedOnly)
      setZipResult({ url, filename })
      window.open(url, '_blank', 'noopener,noreferrer')
      toast.push({ kind: 'success', title: 'ZIP generated', message: `${total} question(s) exported to the portal bucket.` })
    } catch (e) {
      toast.push({ kind: 'error', title: 'Generate ZIP failed', message: e.message })
    } finally {
      setZipBusy(false)
    }
  }
  async function handlePrepareLoad(approvedOnly = false) {
    if (prepForm.child_order === '' || Number.isNaN(Number(prepForm.child_order))) {
      toast.push({ kind: 'error', title: 'Child order required', message: 'Enter the position under the topic.' })
      return
    }
    setPrepBusy(true)
    setPrepResult(null)
    try {
      const res = await prepareAndLoadMcqRun(run.id, {
        child_order: Number(prepForm.child_order),
        duration_min: Number(prepForm.duration_min),
        pass_percentage: Number(prepForm.pass_percentage),
        show_answer_scoring_mode: prepForm.show_answer_scoring_mode,
        should_send_solutions: prepForm.should_send_solutions,
        reviewer_email: user?.email || '',
        approved_only: approvedOnly,
      })
      setPrepResult(res)
      const ok = res.status === 'SUCCESS'
      toast.push({
        kind: ok ? 'success' : 'error',
        title: ok ? 'Loaded to beta' : `Load ${String(res.status || '').toLowerCase()}`,
        message: ok ? `${res.total} question(s) loaded; resource unlocked.` : (res.message || 'See the prepared sheet.'),
      })
    } catch (e) {
      toast.push({ kind: 'error', title: 'Prepare & load failed', message: e.message })
    } finally {
      setPrepBusy(false)
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
  // Approval state drives the load gate: every generated question must be approved for the
  // full load; "load approved only" needs at least one. Mirrors the backend's _result_for_load.
  // "Remaining" = generated questions the reviewer hasn't excluded; excluded ones stay
  // in the list (shaded) but drop out of the approval gate and are never loaded.
  const eligibleQs = questions.filter((q) => q.status === 'generated' && !q.excluded)
  const excludedCount = questions.filter((q) => q.status === 'generated' && q.excluded).length
  const approvedCount = eligibleQs.filter((q) => q.approval === 'approved').length
  const allApproved = eligibleQs.length > 0 && approvedCount === eligibleQs.length
  // "Mark run reviewed" is allowed only when EVERY generated question is resolved —
  // approved or excluded (no pending). Export/load stay FROZEN until the run is reviewed.
  const generatedCount = questions.filter((q) => q.status === 'generated').length
  const pendingCount = eligibleQs.filter((q) => q.approval !== 'approved').length
  const canMarkReviewed = generatedCount > 0 && pendingCount === 0
  const canExport = approved   // review_status === 'approved' (set by Mark run reviewed)

  // When questions are excluded, make the user confirm (type "confirm") before an
  // export/load proceeds with only the remaining questions.
  function guardExcluded(proceed, proceedingCount) {
    if (excludedCount > 0) setConfirm({ proceed, proceedingCount })
    else proceed()
  }
  const ragCallCount = useMemo(
    () => (r.questions || []).reduce(
      (n, q) => n + (q.rag_calls?.length || 0) + (q.review_rag_calls?.length || 0), 0),
    [r.questions],
  )

  const [tab, setTab] = useState('questions')
  const [filter, setFilter] = useState('all') // all | review

  const shown = filter === 'review' ? questions.filter((q) => q.needs_human) : questions

  // Reading material sits to the RIGHT of the questions, behind a draggable adjustment bar.
  // The session is whatever was passed in (generation page) or, in the Runs/Review views,
  // the run's own course + session ids.
  const cId = courseId || run?.course_id
  const uId = unitId || run?.unit_id
  const showReading = Boolean(cId && uId)

  const splitRef = useRef(null)
  const [leftPct, setLeftPct] = useState(() => {
    const saved = Number(localStorage.getItem('mcq-split-left'))
    return saved >= 30 && saved <= 80 ? saved : 58
  })
  const [dragging, setDragging] = useState(false)
  useEffect(() => {
    if (!dragging) return
    const onMove = (e) => {
      const el = splitRef.current
      if (!el) return
      const rect = el.getBoundingClientRect()
      const pct = ((e.clientX - rect.left) / rect.width) * 100
      setLeftPct(Math.min(80, Math.max(30, pct)))
    }
    const onUp = () => setDragging(false)
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [dragging])
  useEffect(() => { localStorage.setItem('mcq-split-left', String(Math.round(leftPct))) }, [leftPct])
  const onSplitKey = (e) => {
    if (e.key === 'ArrowLeft') { setLeftPct((p) => Math.max(30, p - 2)); e.preventDefault() }
    else if (e.key === 'ArrowRight') { setLeftPct((p) => Math.min(80, p + 2)); e.preventDefault() }
  }

  return (
    <div className={`mcq-results-split${showReading ? '' : ' no-reading'}${dragging ? ' dragging' : ''}`} ref={splitRef}>
      <div className="mcq-results" style={showReading ? { flexBasis: `${leftPct}%`, minWidth: 0 } : undefined}>
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
          {run?.version != null && (
            <span className="mcq-status-chip" title="Generation version for this session">v{run.version}</span>
          )}
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
            {review && (
              <div className="mcq-review-tools">
                <span className="mcq-reviewer-as" title="Review actions are attributed to your account">
                  Reviewing as <strong>{user?.name || user?.email || 'you'}</strong>
                </span>
                <span className={`mcq-status-chip ${allApproved ? 'ok' : ''}`} title="Questions you've approved">
                  {approvedCount} / {eligibleQs.length} approved
                </span>
                {excludedCount > 0 && (
                  <span className="mcq-status-chip" title="Excluded — will not be loaded">
                    <Ban size={12} /> {excludedCount} excluded
                  </span>
                )}
                {approved ? (
                  <span className="mcq-status-chip ok"><ShieldCheck size={12} /> run reviewed</span>
                ) : (
                  <button className="btn btn-soft btn-sm" onClick={handleApprove}
                    disabled={!canMarkReviewed}
                    title={canMarkReviewed ? ''
                      : `Approve or exclude every question first (${pendingCount} still pending)`}>
                    <ShieldCheck size={13} /> Mark run reviewed
                  </button>
                )}
                <button className="btn btn-soft btn-sm"
                  onClick={() => guardExcluded(() => handleGenerateZip(false), eligibleQs.length)}
                  disabled={zipBusy || !canExport}
                  title={canExport ? '' : 'Mark the run reviewed first'}>
                  <Download size={13} /> {zipBusy ? 'Generating…' : 'Generate ZIP'}
                </button>
                {zipResult && (
                  <a className="mcq-zip-link" href={zipResult.url} target="_blank" rel="noopener noreferrer"
                    title={zipResult.url}>
                    <Download size={12} /> <span>{zipResult.filename}</span>
                  </a>
                )}
                <button className={`btn btn-sm ${prepOpen ? 'btn-soft' : 'btn-primary'}`}
                  onClick={() => setPrepOpen((v) => !v)} disabled={prepBusy || !canExport}
                  title={canExport ? '' : 'Mark the run reviewed first'}>
                  <FileSpreadsheet size={13} /> Prepare &amp; Load
                </button>
              </div>
            )}
            {!review && (
              <span className="mcq-view-hint">Read-only — approve &amp; load from the Review Queue.</span>
            )}
          </div>

          {review && prepOpen && (
            <div className="mcq-prep-panel">
              <div className="mcq-prep-head">
                <FileSpreadsheet size={14} />
                <span>Prepare exam-config sheet &amp; load to beta</span>
                <span className="mcq-prep-sub">
                  parent = this session's topic · {approvedCount} of {eligibleQs.length} approved
                </span>
              </div>
              <div className="mcq-prep-fields">
                <label>Child order under parent
                  <input className="input" type="number" min="1" value={prepForm.child_order}
                    onChange={(e) => setPrepForm((f) => ({ ...f, child_order: e.target.value }))}
                    placeholder="e.g. 5" />
                </label>
                <label>Duration (minutes)
                  <input className="input" type="number" min="1" value={prepForm.duration_min}
                    onChange={(e) => setPrepForm((f) => ({ ...f, duration_min: e.target.value }))} />
                </label>
                <label>Pass percentage
                  <input className="input" type="number" min="0" max="100" value={prepForm.pass_percentage}
                    onChange={(e) => setPrepForm((f) => ({ ...f, pass_percentage: e.target.value }))} />
                </label>
                <label>Show answer scoring mode
                  <select className="input" value={prepForm.show_answer_scoring_mode}
                    onChange={(e) => setPrepForm((f) => ({ ...f, show_answer_scoring_mode: e.target.value }))}>
                    <option value="INCORRECT">INCORRECT</option>
                    <option value="CORRECT">CORRECT</option>
                    <option value="DEFAULT">DEFAULT</option>
                  </select>
                </label>
                <label>Should send solutions
                  <select className="input" value={prepForm.should_send_solutions}
                    onChange={(e) => setPrepForm((f) => ({ ...f, should_send_solutions: e.target.value }))}>
                    <option value="yes">yes</option>
                    <option value="no">no</option>
                  </select>
                </label>
              </div>
              <div className="mcq-prep-actions">
                <button className="btn btn-primary btn-sm"
                  onClick={() => guardExcluded(() => handlePrepareLoad(false), eligibleQs.length)}
                  disabled={prepBusy || !allApproved}
                  title={allApproved ? '' : `Approve all ${eligibleQs.length} remaining questions to enable`}>
                  {prepBusy ? <Spinner size={13} /> : <FileSpreadsheet size={13} />}
                  {prepBusy ? 'Loading… (up to ~2 min)' : 'Prepare & load all'}
                </button>
                <button className="btn btn-soft btn-sm"
                  onClick={() => guardExcluded(() => handlePrepareLoad(true), approvedCount)}
                  disabled={prepBusy || approvedCount < 1}>
                  <FileSpreadsheet size={13} /> Load approved only ({approvedCount})
                </button>
                {prepResult && (
                  <span className={`mcq-status-chip ${prepResult.status === 'SUCCESS' ? 'ok' : 'warn'}`}>
                    {prepResult.status}
                  </span>
                )}
                {prepResult?.sheet_url && (
                  <a className="mcq-zip-link" href={prepResult.sheet_url} target="_blank" rel="noopener noreferrer"
                    title={prepResult.sheet_url}>
                    <ExternalLink size={12} /> <span>Open config sheet</span>
                  </a>
                )}
              </div>
              {prepResult && prepResult.status !== 'SUCCESS' && prepResult.message && (
                <div className="mcq-prep-msg">{prepResult.message}</div>
              )}
            </div>
          )}

          <div className="qc-list">
            {shown.map((q) => (
              <QuestionCard key={q.outcome} q={q} lo={loByOutcome[q.outcome]}
                index={questions.indexOf(q)}
                review={review && q.status === 'generated' ? (
                  <QuestionReview
                    q={q} busy={busyOutcome === q.outcome}
                    onApprove={(approval) => handleSetApproval(q.outcome, approval)}
                    onRegenerate={(fb, tags) => handleRegenerate(q.outcome, fb, tags)}
                    onExclude={(excluded) => handleSetExclusion(q.outcome, excluded)}
                  />
                ) : null} />
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
      {showReading && (
        <>
          <div
            className="mcq-splitter"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize reading material panel"
            aria-valuenow={Math.round(leftPct)}
            aria-valuemin={30}
            aria-valuemax={80}
            tabIndex={0}
            title="Drag to resize"
            onMouseDown={(e) => { e.preventDefault(); setDragging(true) }}
            onKeyDown={onSplitKey}
          />
          <ReadingMaterialPane courseId={cId} unitId={uId} />
        </>
      )}
      {confirm && (
        <ConfirmExcludeModal
          excludedCount={excludedCount}
          proceedingCount={confirm.proceedingCount}
          onCancel={() => setConfirm(null)}
          onConfirm={() => { const p = confirm.proceed; setConfirm(null); p() }}
        />
      )}
    </div>
  )
}

export default McqResults
