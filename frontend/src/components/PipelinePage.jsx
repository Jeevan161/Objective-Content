import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Workflow, RotateCcw, Save, Pencil, X, ChevronRight, CheckCircle2, Info,
  FileSearch, Brain, Wrench, GitMerge, ListChecks, Sparkles, ShieldCheck,
  Search, Copy, Check, SlidersHorizontal,
} from 'lucide-react'
import { getMcqPipeline, updateMcqPrompt, resetMcqPrompt } from '../api'
import { useToast } from './Toast'
import { EmptyState, Skeleton, Spinner } from './ui'

// Icon + accent + one-line blurb per stage, so the map reads at a glance and the
// panel can introduce the selected stage in plain language.
const STAGE_META = {
  // LO-creation stage (deterministic 10-node pipeline).
  parse_structure: { icon: FileSearch, accent: 'var(--text-3)', blurb: 'Split the reading material into topics from its own headings.' },
  extract_concepts: { icon: Brain, accent: 'var(--violet)', blurb: 'Extract the teachable concepts, stabilized by self-consistency voting.' },
  canonicalize_concepts: { icon: GitMerge, accent: 'var(--text-3)', blurb: 'Normalize concepts into a stable, de-duplicated inventory.' },
  build_dependency_graph: { icon: Workflow, accent: 'var(--violet)', blurb: 'Infer the prerequisite graph over the concepts (vote-stabilized).' },
  plan_allocation: { icon: ListChecks, accent: 'var(--text-3)', blurb: 'Allocate the 20 outcomes across topics and Bloom levels.' },
  author_outcomes: { icon: Sparkles, accent: 'var(--violet)', blurb: 'Write the learning outcomes for each topic to the planned counts.' },
  resolve_prerequisites: { icon: GitMerge, accent: 'var(--text-3)', blurb: 'Attach each outcome’s in-session prerequisite closure.' },
  validate: { icon: ShieldCheck, accent: 'var(--text-3)', blurb: 'Run the validation rules V1–V11 over the outcome set.' },
  repair: { icon: Wrench, accent: 'var(--amber)', blurb: 'Regenerate only the outcomes that failed validation.' },
  finalize: { icon: CheckCircle2, accent: 'var(--text-3)', blurb: 'Freeze, hash and stamp provenance on the outcome artifact.' },
  lo_to_legacy: { icon: GitMerge, accent: 'var(--text-3)', blurb: 'Bridge the frozen outcomes into the question pipeline’s format.' },
  // Question stage.
  recommend_question_types: { icon: ListChecks, accent: 'var(--amber)', blurb: 'Pick the single ideal question type for each outcome.' },
  generate_questions: { icon: Sparkles, accent: 'var(--green)', blurb: 'Write one grounded question per outcome, by type.' },
  review_questions: { icon: ShieldCheck, accent: 'var(--cyan)', blurb: 'Validate each question and fix it until it passes.' },
}
const metaFor = (key) => STAGE_META[key] || { icon: Workflow, accent: 'var(--violet)', blurb: '' }

const LINES = (s) => (s ? s.split('\n').length : 0)

// One editable prompt. Long content is clamped until expanded; Save/Reset bubble
// up to the page so it can hit the API, toast, and update the shared data.
function PromptCard({ prompt, onSave, onReset }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(prompt.content)
  const [showDefault, setShowDefault] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)

  const ro = Boolean(prompt.informational) // deterministic reference doc — read-only
  const dirty = draft !== prompt.content
  const lines = LINES(prompt.content)
  const long = lines > 14

  async function handleSave() {
    setBusy(true)
    const ok = await onSave(prompt.key, draft)
    setBusy(false)
    if (ok) setEditing(false)
  }

  async function handleReset() {
    setBusy(true)
    const reset = await onReset(prompt.key)
    setBusy(false)
    if (reset) {
      setDraft(reset.content)
      setEditing(false)
    }
  }

  function cancel() {
    setDraft(prompt.content)
    setEditing(false)
  }

  function copy() {
    navigator.clipboard?.writeText(prompt.content)
    setCopied(true)
    setTimeout(() => setCopied(false), 1200)
  }

  return (
    <div className={`prompt-card ${prompt.overridden ? 'is-edited' : ''}`}>
      <div className="prompt-card-head">
        <code className="prompt-key">{prompt.key}</code>
        {ro ? (
          <span className="prompt-badge ref" data-tip="Deterministic stage — read-only reference">
            <Info size={11} /> reference
          </span>
        ) : prompt.overridden ? (
          <span className="prompt-badge edited" data-tip={`Active version v${prompt.version}`}>
            <Pencil size={11} /> edited
          </span>
        ) : (
          <span className="prompt-badge default">default</span>
        )}
        <span className="prompt-meta">{lines} {lines === 1 ? 'line' : 'lines'} · {prompt.content.length} chars</span>
        <span className="prompt-head-actions">
          {!editing && (
            <>
              <button className="icon-btn" data-tip={copied ? 'Copied!' : 'Copy'} onClick={copy}>
                {copied ? <Check size={14} /> : <Copy size={14} />}
              </button>
              {!ro && (
                <button className="btn btn-ghost btn-sm prompt-edit-btn" onClick={() => setEditing(true)}>
                  <Pencil size={13} /> Edit
                </button>
              )}
            </>
          )}
        </span>
      </div>

      {prompt.description && <p className="prompt-desc">{prompt.description}</p>}

      {editing ? (
        <>
          <textarea
            className="input mono prompt-textarea"
            value={draft}
            spellCheck={false}
            rows={Math.min(28, Math.max(8, LINES(draft) + 1))}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="prompt-actions">
            <button className="btn btn-primary btn-sm" disabled={!dirty || busy} onClick={handleSave}>
              {busy ? <Spinner size={13} /> : <Save size={13} />} Save version
            </button>
            <button className="btn btn-ghost btn-sm" disabled={busy} onClick={cancel}>
              <X size={13} /> Cancel
            </button>
            {prompt.overridden && (
              <button
                className="btn btn-ghost btn-sm prompt-reset"
                disabled={busy}
                onClick={handleReset}
                data-tip="Restore the built-in default text"
              >
                <RotateCcw size={13} /> Reset to default
              </button>
            )}
          </div>
        </>
      ) : (
        <div className="prompt-body-wrap">
          <pre className={`prompt-body ${long && !expanded ? 'clamped' : ''}`}>{prompt.content}</pre>
          {long && (
            <button className="prompt-expand" onClick={() => setExpanded((e) => !e)}>
              {expanded ? 'Collapse' : `Show full (${lines} lines)`}
            </button>
          )}
        </div>
      )}

      {prompt.overridden && !editing && (
        <div className="prompt-default-wrap">
          <button className="prompt-default-toggle" onClick={() => setShowDefault((s) => !s)}>
            <ChevronRight size={13} className={showDefault ? 'rot90' : ''} />
            {showDefault ? 'Hide' : 'Compare'} code default
          </button>
          {showDefault && <pre className="prompt-body is-default">{prompt.default}</pre>}
        </div>
      )}
    </div>
  )
}

// Stage map (left) + the selected stage's prompts (right). Parallel stages are
// bracketed to mirror the concurrent branches in the backend.
function PipelinePage() {
  const toast = useToast()
  const [data, setData] = useState(null) // null = loading
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)
  const [filter, setFilter] = useState('')
  const [editedOnly, setEditedOnly] = useState(false)

  const load = useCallback(async () => {
    try {
      const res = await getMcqPipeline()
      setData(res)
      setSelected((cur) => cur || res.stages.find((s) => s.prompts.length)?.key || res.stages[0]?.key)
    } catch (e) {
      setError(e.message)
    }
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch on mount
    load()
  }, [load])

  const replacePrompt = useCallback((updated) => {
    setData((d) => {
      if (!d) return d
      const swap = (p) => (p.key === updated.key ? updated : p)
      const recount = (stages, unassigned) => {
        const all = [...stages.flatMap((s) => s.prompts), ...unassigned]
        return { ...d.counts, overridden: all.filter((p) => p.overridden).length }
      }
      const stages = d.stages.map((s) => ({ ...s, prompts: s.prompts.map(swap) }))
      const unassigned = d.unassigned.map(swap)
      return { ...d, stages, unassigned, counts: recount(stages, unassigned) }
    })
  }, [])

  const handleSave = useCallback(
    async (key, content) => {
      try {
        const updated = await updateMcqPrompt(key, content)
        replacePrompt(updated)
        toast.push({ kind: 'success', title: 'Prompt saved', message: `${key} → v${updated.version} (live now)` })
        return true
      } catch (e) {
        toast.push({ kind: 'error', title: 'Could not save prompt', message: e.message })
        return false
      }
    },
    [replacePrompt, toast],
  )

  const handleReset = useCallback(
    async (key) => {
      try {
        const updated = await resetMcqPrompt(key)
        replacePrompt(updated)
        toast.push({ kind: 'info', title: 'Prompt reset', message: `${key} restored to default` })
        return updated
      } catch (e) {
        toast.push({ kind: 'error', title: 'Could not reset prompt', message: e.message })
        return null
      }
    },
    [replacePrompt, toast],
  )

  // Group consecutive parallel stages into one bracketed row in the map.
  const rows = useMemo(() => {
    const stages = data?.stages || []
    const out = []
    let i = 0
    while (i < stages.length) {
      const group = stages[i].parallel_group
      if (group) {
        const grp = []
        while (i < stages.length && stages[i].parallel_group === group) grp.push(stages[i++])
        out.push(grp)
      } else {
        out.push([stages[i++]])
      }
    }
    return out
  }, [data])

  const activeStage = data?.stages.find((s) => s.key === selected) || null
  const meta = activeStage ? metaFor(activeStage.key) : null

  const shownPrompts = useMemo(() => {
    if (!activeStage) return []
    const q = filter.trim().toLowerCase()
    return activeStage.prompts.filter(
      (p) =>
        (!editedOnly || p.overridden) &&
        (!q ||
          p.key.toLowerCase().includes(q) ||
          (p.description || '').toLowerCase().includes(q) ||
          (p.content || '').toLowerCase().includes(q)),
    )
  }, [activeStage, filter, editedOnly])

  return (
    <div className="pipeline-page">
      <header className="topbar">
        <div>
          <h1>MCQ Pipeline</h1>
          <p className="topbar-sub">
            The stages the generator runs, and the editable prompts that drive each one.
            Saving a prompt applies it to the next run immediately.
          </p>
        </div>
        {data && (
          <div className="pipeline-stats">
            <span className="pstat"><strong>{data.counts.stages}</strong> stages</span>
            <span className="pstat"><strong>{data.counts.prompts}</strong> prompts</span>
            <span className={`pstat ${data.counts.overridden ? 'accent' : ''}`}>
              <strong>{data.counts.overridden}</strong> edited
            </span>
          </div>
        )}
      </header>

      {error && <EmptyState icon={Workflow} title="Could not load the pipeline" hint={error} />}

      {!error && data === null && (
        <div className="pipeline-layout">
          <div className="pipeline-stages">
            <Skeleton height={62} /><Skeleton height={62} /><Skeleton height={62} /><Skeleton height={62} />
          </div>
          <div className="pipeline-panel"><Skeleton height={260} /></div>
        </div>
      )}

      {!error && data && (
        <div className="pipeline-layout">
          {/* Stage map */}
          <div className="pipeline-stages">
            {rows.map((row, ri) => (
              <div key={ri} className="pipeline-row-wrap">
                <div className={`pipeline-row ${row.length > 1 ? 'parallel' : ''}`}>
                  {row.length > 1 && <span className="parallel-tag">runs in parallel</span>}
                  <div className="pipeline-row-cards">
                    {row.map((stage) => {
                      const num = data.stages.indexOf(stage) + 1
                      const m = metaFor(stage.key)
                      const Icon = m.icon
                      const edited = stage.prompts.filter((p) => p.overridden).length
                      const allRef = stage.prompts.length > 0 && stage.prompts.every((p) => p.informational)
                      return (
                        <button
                          key={stage.key}
                          className={`pipeline-stage-btn ${selected === stage.key ? 'active' : ''}`}
                          style={{ '--accent': m.accent }}
                          onClick={() => setSelected(stage.key)}
                        >
                          <span className="pstage-icon"><Icon size={16} /></span>
                          <span className="pstage-body">
                            <span className="pstage-label">
                              <span className="pstage-num">{num}</span>
                              {stage.label}
                            </span>
                            <span className="pstage-count">
                              {allRef
                                ? 'reference'
                                : stage.prompts.length
                                  ? `${stage.prompts.length} prompt${stage.prompts.length === 1 ? '' : 's'}`
                                  : 'no prompt'}
                              {edited > 0 && <span className="pstage-edited">{edited} edited</span>}
                            </span>
                          </span>
                          <ChevronRight size={15} className="pstage-arrow" />
                        </button>
                      )
                    })}
                  </div>
                </div>
                {ri < rows.length - 1 && <span className="pipeline-flow-arrow" aria-hidden>↓</span>}
              </div>
            ))}
          </div>

          {/* Selected stage prompts */}
          <div className="pipeline-panel">
            {activeStage && (
              <div key={activeStage.key} className="panel-fade">
                <div className="panel-head">
                  <span className="panel-icon" style={{ '--accent': meta.accent }}>
                    <meta.icon size={20} />
                  </span>
                  <div className="panel-head-text">
                    <h2>{activeStage.label}</h2>
                    <span className="panel-sub">{meta.blurb}</span>
                  </div>
                </div>

                {activeStage.note && (
                  <div className="panel-note"><Info size={14} /> {activeStage.note}</div>
                )}

                {activeStage.prompts.length === 0 ? (
                  <EmptyState
                    icon={CheckCircle2}
                    title="No prompts here"
                    hint="This stage is deterministic code — there's nothing to tune."
                  />
                ) : (
                  <>
                    <div className="panel-tools">
                      <div className="panel-search">
                        <Search size={14} />
                        <input
                          className="input"
                          placeholder={`Search ${activeStage.prompts.length} prompts…`}
                          value={filter}
                          spellCheck={false}
                          onChange={(e) => setFilter(e.target.value)}
                        />
                        {filter && (
                          <button className="icon-btn" onClick={() => setFilter('')} data-tip="Clear">
                            <X size={13} />
                          </button>
                        )}
                      </div>
                      <button
                        className={`btn btn-sm ${editedOnly ? 'btn-primary' : 'btn-soft'}`}
                        onClick={() => setEditedOnly((v) => !v)}
                        data-tip="Show only prompts changed from their code default"
                      >
                        <SlidersHorizontal size={13} /> Edited only
                      </button>
                    </div>

                    {shownPrompts.length === 0 ? (
                      <EmptyState
                        icon={Search}
                        title="No matching prompts"
                        hint={editedOnly ? 'No edited prompts in this stage.' : `Nothing matches “${filter.trim()}”.`}
                      />
                    ) : (
                      <div className="prompt-list">
                        {shownPrompts.map((p) => (
                          <PromptCard key={p.key} prompt={p} onSave={handleSave} onReset={handleReset} />
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export default PipelinePage
