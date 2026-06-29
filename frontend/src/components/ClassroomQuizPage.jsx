import { useCallback, useEffect, useState } from 'react'
import { Presentation, Sparkles, Play, RefreshCw, FileText, AlertTriangle, ChevronRight } from 'lucide-react'
import {
  classroomQuizIngest,
  classroomQuizListDecks,
  classroomQuizGetDeck,
  classroomQuizGenerate,
  getMcqRun,
  mcqJobWsUrl,
} from '../api'
import { EmptyState, Spinner } from './ui'
import { useToast } from './Toast'
import McqProgress from './McqProgress'
import McqResults from './McqResults'

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
  const [run, setRun] = useState(null)
  const [open, setOpen] = useState(false)
  const [loadingRun, setLoadingRun] = useState(false)
  const streamable = !!(job && job.id && !TERMINAL.includes(job.status))

  useEffect(() => {
    if (!streamable) return
    const url = mcqJobWsUrl(job.id)
    let ws = null
    let stopped = false
    let retry = null
    const connect = () => {
      ws = new WebSocket(url)
      ws.onmessage = (e) => {
        let msg
        try { msg = JSON.parse(e.data) } catch { return }
        if (msg.type !== 'job') return
        const updated = msg.data
        onJobUpdate(scope.id, updated)
        if (TERMINAL.includes(updated.status)) {
          stopped = true
          ws.close()
          if (updated.status === 'SUCCESS') onSettled?.()
        }
      }
      ws.onclose = () => { if (!stopped) retry = setTimeout(connect, 1500) }
    }
    connect()
    return () => { stopped = true; if (retry) clearTimeout(retry); if (ws) ws.close() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.id, streamable])

  useEffect(() => {
    if (!scope.run_id) return
    let alive = true
    setLoadingRun(true)
    getMcqRun(scope.run_id)
      .then((r) => { if (alive) setRun(r) })
      .catch(() => {})
      .finally(() => { if (alive) setLoadingRun(false) })
    return () => { alive = false }
  }, [scope.run_id])

  const running = !!(job && !TERMINAL.includes(job.status))
  const failed = (job && job.status === 'FAILURE') || scope.coverage === 'FAILED'
  const counts = run
    ? `${run.lo_count} LOs · ${run.question_count} questions`
    : null

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

      {running && job.progress && <McqProgress progress={job.progress} />}
      {failed && (
        <div className="cq-scope-err">
          <AlertTriangle size={14} /> Generation failed{job?.error ? `: ${job.error}` : ''}
        </div>
      )}

      {scope.run_id && (
        <div className="cq-scope-body">
          <button className="cq-link" onClick={() => setOpen((o) => !o)}>
            <ChevronRight size={14} className={`cq-chev ${open ? 'open' : ''}`} />
            {open ? 'Hide' : 'View'} reading material, base questions & variants
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
