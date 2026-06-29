import { useCallback, useEffect, useState } from 'react'
import { Presentation, Sparkles, Play, RefreshCw, FileText, AlertTriangle, ChevronRight, Layers } from 'lucide-react'
import {
  classroomQuizIngest,
  classroomQuizListDecks,
  classroomQuizGetDeck,
  classroomQuizGenerate,
  classroomQuizGenerateScope,
  classroomQuizGenerateVariants,
  classroomQuizResume,
  getMcqRun,
  mcqJobWsUrl,
} from '../api'
import { EmptyState, Spinner } from './ui'
import { useToast } from './Toast'
import McqProgress from './McqProgress'
import McqResults from './McqResults'
import McqReviewGate from './McqReviewGate'

const TERMINAL = ['SUCCESS', 'FAILURE', 'CANCELLED']
const COVERAGE = {
  OK: { label: 'OK', cls: 'cov-ok' },
  THIN: { label: 'Thin · 3 LOs', cls: 'cov-thin' },
  INSUFFICIENT: { label: 'Insufficient', cls: 'cov-bad' },
  FAILED: { label: 'Failed', cls: 'cov-bad' },
}

function CoverageBadge({ coverage }) {
  const c = COVERAGE[coverage] || COVERAGE.OK
  return <span className={`cq-cov ${c.cls}`}>{c.label}</span>
}

const STATUS_LABEL = {
  SCOPED: 'Ready to generate',
  GENERATING: 'Generating…',
  READY_FOR_REVIEW: 'Ready for review',
  APPROVED: 'Approved',
  FAILED: 'Failed',
}

// One scope: owns its own progress WebSocket; renders the live stage board while generating
// and the generated run (reading material + base questions + variants) via McqResults when done.
function ScopeCard({ scope, job, onJobUpdate, onSettled }) {
  const toast = useToast()
  const [run, setRun] = useState(null)
  const [open, setOpen] = useState(false)
  const [loadingRun, setLoadingRun] = useState(false)
  const [varJob, setVarJob] = useState(null)          // phase-2 variant generation job
  const [genningVar, setGenningVar] = useState(false)
  const [resuming, setResuming] = useState(false)     // posting the LO-gate decision
  const [genning, setGenning] = useState(false)       // starting this scope's generation
  // Paused at the LO-finalization gate (Gate 1): stop streaming, show the review UI.
  const paused = !!(job && job.status === 'AWAITING_REVIEW' && job.progress?.gate === 'outcomes')
  const streamable = !!(job && job.id && !TERMINAL.includes(job.status) && !paused)
  const varStreamable = !!(varJob && varJob.id && !TERMINAL.includes(varJob.status))

  const loadRun = useCallback(() => {
    if (!scope.run_id) return
    setLoadingRun(true)
    getMcqRun(scope.run_id).then(setRun).catch(() => {}).finally(() => setLoadingRun(false))
  }, [scope.run_id])

  useEffect(() => { loadRun() }, [loadRun])

  // Phase-1 (base generation) progress stream.
  useEffect(() => {
    if (!streamable) return
    const url = mcqJobWsUrl(job.id)
    let ws = null, stopped = false, retry = null
    const connect = () => {
      ws = new WebSocket(url)
      ws.onmessage = (e) => {
        let msg
        try { msg = JSON.parse(e.data) } catch { return }
        if (msg.type !== 'job') return
        const updated = msg.data
        onJobUpdate(scope.id, updated)
        if (TERMINAL.includes(updated.status)) {
          stopped = true; ws.close()
          if (updated.status === 'SUCCESS') onSettled?.()
        }
      }
      ws.onclose = () => { if (!stopped) retry = setTimeout(connect, 1500) }
    }
    connect()
    return () => { stopped = true; if (retry) clearTimeout(retry); if (ws) ws.close() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.id, streamable])

  // Phase-2 (variant generation) progress stream — refetch the run on success.
  useEffect(() => {
    if (!varStreamable) return
    const url = mcqJobWsUrl(varJob.id)
    let ws = null, stopped = false, retry = null
    const connect = () => {
      ws = new WebSocket(url)
      ws.onmessage = (e) => {
        let msg
        try { msg = JSON.parse(e.data) } catch { return }
        if (msg.type !== 'job') return
        const updated = msg.data
        setVarJob(updated)
        if (TERMINAL.includes(updated.status)) {
          stopped = true; ws.close()
          if (updated.status === 'SUCCESS') { loadRun(); setOpen(true) }
        }
      }
      ws.onclose = () => { if (!stopped) retry = setTimeout(connect, 1500) }
    }
    connect()
    return () => { stopped = true; if (retry) clearTimeout(retry); if (ws) ws.close() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [varJob?.id, varStreamable])

  const handleGenerateScope = async () => {
    setGenning(true)
    try {
      const j = await classroomQuizGenerateScope(scope.id)
      onJobUpdate(scope.id, j)         // status -> RUNNING → phase-1 WS attaches
      toast.push({ kind: 'info', title: `Generating Quiz ${scope.scope_no}`,
                   message: 'Reading material → learning objectives; it will pause for your review.' })
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not start generation', message: e.message })
    } finally { setGenning(false) }
  }

  const handleResume = async (decision) => {
    setResuming(true)
    try {
      const j = await classroomQuizResume(job.id, decision)
      onJobUpdate(scope.id, j)        // status flips back to RUNNING → phase-1 WS reattaches
      toast.push({ kind: 'info', title: 'Review submitted',
                   message: decision.action === 'reject'
                     ? 'Regenerating the flagged learning objectives…'
                     : 'Generating base questions…' })
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not submit review', message: e.message })
    } finally { setResuming(false) }
  }

  const handleVariants = async () => {
    if (!run) return
    setGenningVar(true)
    try {
      const j = await classroomQuizGenerateVariants(run.id)
      setVarJob(j)
      toast.push({ kind: 'info', title: 'Generating variants',
                   message: 'For the approved base questions — watch the progress below.' })
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not generate variants', message: e.message })
    } finally { setGenningVar(false) }
  }

  const running = !!(job && !TERMINAL.includes(job.status))
  const failed = (job && job.status === 'FAILURE') || scope.coverage === 'FAILED'
  const counts = run ? `${run.lo_count} LOs · ${run.question_count} questions` : null
  const approved = run?.approved_count || 0
  const hasVariants = (run?.result?.questions || []).some((q) => q.is_variant)
  const varRunning = !!(varJob && !TERMINAL.includes(varJob.status))

  return (
    <div className="cq-scope">
      <div className="cq-scope-head">
        <div className="cq-scope-title">
          <span className="cq-scope-no">Quiz {scope.scope_no}</span>
          <span className="cq-scope-range">slides {scope.slide_start}–{scope.slide_end}</span>
          <span className="cq-scope-kind">closes at “{scope.kind}”</span>
        </div>
        <div className="cq-scope-meta">
          {counts && <span className="cq-scope-counts">{counts}</span>}
          {scope.run_id && <CoverageBadge coverage={scope.coverage} />}
        </div>
      </div>

      {!running && !paused && (
        <div className="cq-scope-actions">
          <button className="cq-btn cq-btn-sm" onClick={handleGenerateScope} disabled={genning}>
            {genning ? <Spinner size={14} /> : <Play size={14} />}
            {scope.run_id ? 'Regenerate this quiz' : 'Generate this quiz'}
          </button>
        </div>
      )}
      {running && !paused && job.progress && <McqProgress progress={job.progress} />}
      {paused && job.progress?.review && (
        <div className="cq-scope-gate">
          <div className="cq-scope-gate-head">
            <Sparkles size={14} /> Finalize the learning objectives for this quiz before questions are generated.
          </div>
          <McqReviewGate review={job.progress.review} busy={resuming} onDecide={handleResume} />
        </div>
      )}
      {failed && (
        <div className="cq-scope-err">
          <AlertTriangle size={14} /> Generation failed{job?.error ? `: ${job.error}` : ''}
        </div>
      )}

      {scope.run_id && (
        <div className="cq-scope-body">
          <div className="cq-scope-actions">
            {approved > 0 ? (
              <button className="cq-btn cq-btn-primary cq-btn-sm"
                onClick={handleVariants} disabled={genningVar || varRunning}>
                {(genningVar || varRunning) ? <Spinner size={14} /> : <Layers size={15} />}
                {hasVariants ? 'Regenerate variants'
                  : `Generate variants (${approved} approved base${approved === 1 ? '' : 's'})`}
              </button>
            ) : (
              <span className="cq-scope-hint">
                Review &amp; approve base questions in the Review Queue to unlock variant generation.
              </span>
            )}
          </div>

          {varRunning && varJob.progress && <McqProgress progress={varJob.progress} />}
          {varJob && varJob.status === 'FAILURE' && (
            <div className="cq-scope-err">
              <AlertTriangle size={14} /> Variant generation failed{varJob.error ? `: ${varJob.error}` : ''}
            </div>
          )}

          <button className="cq-link" onClick={() => setOpen((o) => !o)}>
            <ChevronRight size={14} className={`cq-chev ${open ? 'open' : ''}`} />
            {open ? 'Hide' : 'View'} reading material, base questions{hasVariants ? ' & variants' : ''}
          </button>
          {open && (loadingRun
            ? <div className="cq-run-loading"><Spinner /></div>
            : run && (
              <McqResults
                run={run}
                mode="view"
                canLoad={false}
                readingMaterial={run.result?.reading_material || run.reading_material || ''}
              />
            ))}
        </div>
      )}
    </div>
  )
}

export default function ClassroomQuizPage() {
  const toast = useToast()
  const [decks, setDecks] = useState([])
  const [loading, setLoading] = useState(true)
  const [deck, setDeck] = useState(null)         // active deck (with scopes)
  const [jobs, setJobs] = useState({})           // scope_id -> live job
  const [url, setUrl] = useState('')
  const [title, setTitle] = useState('')
  const [domain, setDomain] = useState('')
  const [ingesting, setIngesting] = useState(false)
  const [generating, setGenerating] = useState(false)

  const loadDecks = useCallback(async () => {
    setLoading(true)
    try { setDecks((await classroomQuizListDecks()) || []) }
    catch (e) { toast.push({ kind: 'error', title: 'Could not load decks', message: e.message }) }
    finally { setLoading(false) }
  }, [toast])

  useEffect(() => { loadDecks() }, [loadDecks])

  const openDeck = async (id) => {
    try { setDeck(await classroomQuizGetDeck(id)); setJobs({}) }
    catch (e) { toast.push({ kind: 'error', title: 'Could not open deck', message: e.message }) }
  }

  const refreshDeck = useCallback(async () => {
    setDeck((cur) => cur)   // keep ref; fetch below
    if (!deck) return
    try { setDeck(await classroomQuizGetDeck(deck.id)) } catch { /* transient */ }
    loadDecks()
  }, [deck, loadDecks])

  const handleIngest = async (e) => {
    e.preventDefault()
    if (!url.trim()) return
    setIngesting(true)
    try {
      const d = await classroomQuizIngest(url.trim(), title.trim(), domain.trim())
      toast.push({ kind: 'success', title: 'Deck ingested',
                   message: `${d.scope_count} quiz scope(s) found.` })
      setUrl(''); setTitle('')
      setDeck(d); setJobs({})
      loadDecks()
    } catch (e) {
      toast.push({ kind: 'error', title: 'Ingest failed', message: e.message })
    } finally { setIngesting(false) }
  }

  const handleGenerate = async () => {
    if (!deck) return
    setGenerating(true)
    try {
      const res = await classroomQuizGenerate(deck.id)
      const map = {}
      for (const j of (res.jobs || [])) {
        const sid = j.progress?.ctx?.scope_id
        if (sid) map[sid] = j
      }
      setJobs(map)
      toast.push({ kind: 'info', title: 'Generation started',
                   message: `${(res.jobs || []).length} scope(s) generating — watch the live progress.` })
      refreshDeck()
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not start generation', message: e.message })
    } finally { setGenerating(false) }
  }

  const onJobUpdate = useCallback((scopeId, job) => {
    setJobs((prev) => ({ ...prev, [scopeId]: job }))
  }, [])

  return (
    <div className="cq-page">
      <header className="cq-header">
        <div className="cq-header-icon"><Presentation size={20} /></div>
        <div>
          <h1>Classroom Quiz</h1>
          <p>Turn a published slides deck into per-quiz reading material, base questions, and variants.</p>
        </div>
      </header>

      <form className="cq-ingest" onSubmit={handleIngest}>
        <div className="cq-ingest-row">
          <input
            className="cq-input cq-input-grow"
            type="url"
            placeholder="Published Google Slides URL (…/pub or …/embed)"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            required
          />
          <input
            className="cq-input"
            type="text"
            placeholder="Title (optional)"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <input
            className="cq-input cq-input-sm"
            type="text"
            placeholder="Domain (e.g. SQL)"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
          />
          <button className="cq-btn cq-btn-primary" type="submit" disabled={ingesting || !url.trim()}>
            {ingesting ? <Spinner size={14} /> : <Sparkles size={15} />}
            {ingesting ? 'Scoping…' : 'Ingest deck'}
          </button>
        </div>
        <div className="cq-ingest-hint">
          Scopes the deck into one quiz per “Quiz Time!” checkpoint (Agenda → … → Key Takeaways).
        </div>
      </form>

      <div className="cq-body">
        <aside className="cq-decklist">
          <div className="cq-decklist-head">
            <span>Recent decks</span>
            <button className="cq-icon-btn" onClick={loadDecks} title="Refresh"><RefreshCw size={14} /></button>
          </div>
          {loading ? (
            <div className="cq-decklist-loading"><Spinner /></div>
          ) : decks.length === 0 ? (
            <div className="cq-decklist-empty">No decks yet.</div>
          ) : (
            decks.map((d) => (
              <button
                key={d.id}
                className={`cq-deck-chip ${deck?.id === d.id ? 'active' : ''}`}
                onClick={() => openDeck(d.id)}
              >
                <FileText size={14} />
                <span className="cq-deck-chip-title">{d.title || 'Untitled deck'}</span>
                <span className="cq-deck-chip-meta">{d.scope_count} · {STATUS_LABEL[d.status] || d.status}</span>
              </button>
            ))
          )}
        </aside>

        <section className="cq-detail">
          {!deck ? (
            <EmptyState
              icon={Presentation}
              title="Ingest a slides deck to begin"
              hint="Paste a published Google Slides URL above, or pick a recent deck."
            />
          ) : (
            <>
              <div className="cq-detail-head">
                <div>
                  <h2>{deck.title || 'Untitled deck'}</h2>
                  <a className="cq-detail-url" href={deck.slides_url} target="_blank" rel="noreferrer">
                    {deck.slides_url}
                  </a>
                  <div className="cq-detail-sub">
                    {deck.scope_count} quiz{deck.scope_count === 1 ? '' : 'zes'} · {STATUS_LABEL[deck.status] || deck.status}
                    {deck.question_domain ? ` · ${deck.question_domain}` : ''}
                  </div>
                </div>
                <div className="cq-detail-actions">
                  <button className="cq-icon-btn" onClick={refreshDeck} title="Refresh deck"><RefreshCw size={15} /></button>
                  <button
                    className="cq-btn cq-btn-primary"
                    onClick={handleGenerate}
                    disabled={generating || deck.status === 'GENERATING'}
                  >
                    {generating ? <Spinner size={14} /> : <Play size={15} />}
                    {deck.status === 'GENERATING' ? 'Generating…'
                      : (deck.scopes || []).some((s) => s.run_id) ? 'Regenerate all' : 'Generate all scopes'}
                  </button>
                </div>
              </div>

              <div className="cq-scopes">
                {(deck.scopes || []).map((sc) => (
                  <ScopeCard
                    key={sc.id}
                    scope={sc}
                    job={jobs[sc.id]}
                    onJobUpdate={onJobUpdate}
                    onSettled={refreshDeck}
                  />
                ))}
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
