import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Plug, Plus, Pencil, Trash2, CheckCircle2, Power, FlaskConical,
  KeyRound, X, Save, Search, AlertTriangle,
} from 'lucide-react'
import {
  getLlmProviders, saveLlmProvider, activateLlmProvider, testLlmProvider, deleteLlmProvider,
} from '../api'
import { useToast } from './Toast'
import { EmptyState, Skeleton, Spinner, Segmented } from './ui'
import Modal from './Modal'

const ADAPTERS = ['openai_compatible', 'anthropic']
const ADAPTER_LABEL = { openai_compatible: 'OpenAI-compatible', anthropic: 'Anthropic' }

// A couple of common presets to speed up adding a connector.
const PRESETS = {
  openai_compatible: { base_url: 'https://api.openai.com/v1', model: 'gpt-4o' },
  anthropic: { base_url: '', model: 'claude-opus-4-8' },
}

function pretty(obj) {
  try {
    return JSON.stringify(obj || {}, null, 2)
  } catch {
    return '{}'
  }
}

// Create / edit a connector. `provider` is the existing row (null = new). The API key
// is write-only: leaving it blank on an existing connector keeps the stored secret.
function ProviderModal({ provider, onClose, onSaved }) {
  const toast = useToast()
  const isNew = !provider
  const [name, setName] = useState(provider?.name || '')
  const [adapter, setAdapter] = useState(provider?.adapter || 'openai_compatible')
  const [model, setModel] = useState(provider?.model || '')
  const [baseUrl, setBaseUrl] = useState(provider?.base_url || '')
  const [apiKey, setApiKey] = useState('')
  const [headers, setHeaders] = useState(pretty(provider?.default_headers))
  const [extraBody, setExtraBody] = useState(pretty(provider?.extra_body))
  const [busy, setBusy] = useState(false)

  // Filling a fresh connector: offer the adapter's default base_url/model once.
  function pickAdapter(a) {
    setAdapter(a)
    if (isNew) {
      const p = PRESETS[a] || {}
      if (!baseUrl) setBaseUrl(p.base_url || '')
      if (!model) setModel(p.model || '')
    }
  }

  async function handleSave() {
    if (!name.trim()) {
      toast.push({ kind: 'error', title: 'Name required', message: 'Give the connector a unique name.' })
      return
    }
    let parsedHeaders
    let parsedExtra
    try {
      parsedHeaders = JSON.parse(headers || '{}')
    } catch {
      toast.push({ kind: 'error', title: 'Invalid default headers', message: 'Default headers must be valid JSON.' })
      return
    }
    try {
      parsedExtra = JSON.parse(extraBody || '{}')
    } catch {
      toast.push({ kind: 'error', title: 'Invalid extra body', message: 'Extra body must be valid JSON.' })
      return
    }
    const payload = {
      name: name.trim(),
      adapter,
      model: model.trim(),
      base_url: baseUrl.trim(),
      default_headers: parsedHeaders,
      extra_body: parsedExtra,
    }
    // Only send the key when the user typed a new one — blank preserves the stored secret.
    if (apiKey) payload.api_key = apiKey

    setBusy(true)
    try {
      const saved = await saveLlmProvider(payload)
      toast.push({ kind: 'success', title: isNew ? 'Connector added' : 'Connector saved', message: saved.name })
      onSaved()
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not save connector', message: e.message })
    } finally {
      setBusy(false)
    }
  }

  const keyPlaceholder = isNew
    ? 'Paste the API key'
    : provider?.has_key
      ? `Stored ${provider.key_masked} — leave blank to keep`
      : 'No key stored — paste one to set it'

  return (
    <Modal
      title={isNew ? 'Add connector' : `Edit ${provider.name}`}
      subtitle="Keys are encrypted at rest and never shown again in full."
      size="lg"
      onClose={onClose}
      footer={
        <div className="form-actions">
          <button className="btn btn-ghost" disabled={busy} onClick={onClose}>
            <X size={14} /> Cancel
          </button>
          <button className="btn btn-primary" disabled={busy} onClick={handleSave}>
            {busy ? <Spinner size={14} /> : <Save size={14} />} {isNew ? 'Add connector' : 'Save changes'}
          </button>
        </div>
      }
    >
      <div className="form-stack">
        <div className="field">
          <label className="field-label">Name</label>
          <input
            className="input"
            value={name}
            disabled={!isNew}
            placeholder="e.g. openai, claude, proxy"
            spellCheck={false}
            onChange={(e) => setName(e.target.value)}
          />
          {!isNew && <span className="field-hint">The name identifies a connector and can't be changed.</span>}
        </div>

        <div className="field">
          <label className="field-label">Adapter</label>
          <Segmented
            options={ADAPTERS}
            value={adapter}
            onChange={pickAdapter}
          />
          <span className="field-hint">
            {adapter === 'anthropic'
              ? 'Claude models via the Anthropic API.'
              : 'OpenAI, OpenRouter, or any OpenAI-compatible endpoint (incl. the internal proxy).'}
          </span>
        </div>

        <div className="llm-form-row">
          <div className="field">
            <label className="field-label">Model</label>
            <input
              className="input mono"
              value={model}
              placeholder={adapter === 'anthropic' ? 'claude-opus-4-8' : 'gpt-4o'}
              spellCheck={false}
              onChange={(e) => setModel(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="field-label">Base URL <span className="field-opt">(optional)</span></label>
            <input
              className="input mono"
              value={baseUrl}
              placeholder={adapter === 'anthropic' ? 'default Anthropic endpoint' : 'https://api.openai.com/v1'}
              spellCheck={false}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </div>
        </div>

        <div className="field">
          <label className="field-label"><KeyRound size={12} /> API key</label>
          <input
            className="input mono"
            type="password"
            value={apiKey}
            placeholder={keyPlaceholder}
            autoComplete="new-password"
            spellCheck={false}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </div>

        <div className="field">
          <label className="field-label">Default headers <span className="field-opt">(JSON)</span></label>
          <textarea
            className="input mono llm-json"
            value={headers}
            rows={3}
            spellCheck={false}
            onChange={(e) => setHeaders(e.target.value)}
          />
        </div>

        <div className="field">
          <label className="field-label">Extra body <span className="field-opt">(JSON)</span></label>
          <textarea
            className="input mono llm-json"
            value={extraBody}
            rows={8}
            spellCheck={false}
            onChange={(e) => setExtraBody(e.target.value)}
          />
          <span className="field-hint">
            Merged into each request body. The internal proxy reads its required
            <code> metadata </code> block from here ( <code>unit</code> / <code>step</code> are filled per run ).
          </span>
        </div>
      </div>
    </Modal>
  )
}

// One connector. Shows adapter, model, endpoint, masked key + the active state, and
// the actions (activate / test / edit / delete).
function ProviderCard({ provider, onActivate, onTest, onEdit, onDelete }) {
  const [busy, setBusy] = useState('') // '' | 'activate' | 'test' | 'delete'

  async function run(kind, fn) {
    setBusy(kind)
    try {
      await fn()
    } finally {
      setBusy('')
    }
  }

  return (
    <div className={`llm-card ${provider.active ? 'active' : ''}`}>
      <div className="llm-card-head">
        <span className="llm-name">{provider.name}</span>
        <span className={`adapter-badge ${provider.adapter}`}>{ADAPTER_LABEL[provider.adapter] || provider.adapter}</span>
        {provider.active && (
          <span className="active-badge"><CheckCircle2 size={12} /> Active</span>
        )}
      </div>

      <dl className="llm-meta">
        <div className="llm-meta-row">
          <dt>Model</dt>
          <dd className="mono">{provider.model || <span className="muted">— not set —</span>}</dd>
        </div>
        <div className="llm-meta-row">
          <dt>Endpoint</dt>
          <dd className="mono">{provider.base_url || <span className="muted">provider default</span>}</dd>
        </div>
        <div className="llm-meta-row">
          <dt>API key</dt>
          <dd className="mono">
            {provider.has_key
              ? provider.key_masked
              : <span className="llm-nokey"><AlertTriangle size={12} /> none stored</span>}
          </dd>
        </div>
        {provider.extra_body?.metadata && (
          <div className="llm-meta-row">
            <dt>Metadata</dt>
            <dd className="mono muted">proxy metadata block set</dd>
          </div>
        )}
      </dl>

      <div className="llm-card-actions">
        {provider.active ? (
          <span className="llm-active-note"><Power size={13} /> Drives every generation</span>
        ) : (
          <button
            className="btn btn-soft btn-sm"
            disabled={Boolean(busy)}
            onClick={() => run('activate', () => onActivate(provider))}
            data-tip="Make this the connector used for all generation"
          >
            {busy === 'activate' ? <Spinner size={13} /> : <Power size={13} />} Activate
          </button>
        )}
        <button
          className="btn btn-ghost btn-sm"
          disabled={Boolean(busy)}
          onClick={() => run('test', () => onTest(provider))}
          data-tip="Make a tiny live call to check connectivity"
        >
          {busy === 'test' ? <Spinner size={13} /> : <FlaskConical size={13} />} Test
        </button>
        <span className="llm-actions-spacer" />
        <button className="icon-btn" onClick={() => onEdit(provider)} data-tip="Edit">
          <Pencil size={14} />
        </button>
        <button
          className="icon-btn danger"
          disabled={Boolean(busy)}
          onClick={() => run('delete', () => onDelete(provider))}
          data-tip="Delete connector"
        >
          {busy === 'delete' ? <Spinner size={14} /> : <Trash2 size={14} />}
        </button>
      </div>
    </div>
  )
}

function LLMProvidersPage() {
  const toast = useToast()
  const [data, setData] = useState(null) // null = loading
  const [error, setError] = useState(null)
  const [filter, setFilter] = useState('')
  const [editing, setEditing] = useState(null) // null | 'new' | provider

  const load = useCallback(async () => {
    try {
      setData(await getLlmProviders())
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch on mount
    load()
  }, [load])

  const handleActivate = useCallback(
    async (p) => {
      try {
        await activateLlmProvider(p.name)
        toast.push({ kind: 'success', title: 'Connector activated', message: `${p.name} now drives every generation` })
        await load()
      } catch (e) {
        toast.push({ kind: 'error', title: 'Could not activate', message: e.message })
      }
    },
    [load, toast],
  )

  const handleTest = useCallback(
    async (p) => {
      try {
        const res = await testLlmProvider(p.name)
        if (res.ok) {
          toast.push({ kind: 'success', title: `${p.name} is reachable`, message: `${res.model} replied: “${res.reply}”` })
        } else {
          toast.push({ kind: 'error', title: `${p.name} test failed`, message: res.error || 'No response', duration: 0 })
        }
      } catch (e) {
        toast.push({ kind: 'error', title: 'Test request failed', message: e.message })
      }
    },
    [toast],
  )

  const handleDelete = useCallback(
    async (p) => {
      if (!window.confirm(`Delete the “${p.name}” connector? This cannot be undone.`)) return
      try {
        await deleteLlmProvider(p.name)
        toast.push({ kind: 'info', title: 'Connector deleted', message: p.name })
        await load()
      } catch (e) {
        toast.push({ kind: 'error', title: 'Could not delete', message: e.message })
      }
    },
    [load, toast],
  )

  const onSaved = useCallback(() => {
    setEditing(null)
    load()
  }, [load])

  const shown = useMemo(() => {
    const list = data || []
    const q = filter.trim().toLowerCase()
    if (!q) return list
    return list.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        (p.model || '').toLowerCase().includes(q) ||
        (p.adapter || '').toLowerCase().includes(q),
    )
  }, [data, filter])

  const activeName = (data || []).find((p) => p.active)?.name

  return (
    <div className="llm-page">
      <header className="topbar">
        <div>
          <h1>LLM Connectors</h1>
          <p className="topbar-sub">
            Configure the API keys and endpoints the generator uses. Exactly one connector is
            <strong> active</strong> and drives every LO and question call. Keys are encrypted at rest.
          </p>
        </div>
        <div className="topbar-actions">
          {data && (
            <span className="llm-active-pill">
              {activeName ? <><CheckCircle2 size={13} /> Active: <strong>{activeName}</strong></> : 'No active connector'}
            </span>
          )}
          <button className="btn btn-primary" onClick={() => setEditing('new')}>
            <Plus size={15} /> Add connector
          </button>
        </div>
      </header>

      {error && <EmptyState icon={Plug} title="Could not load connectors" hint={error} />}

      {!error && data === null && (
        <div className="llm-grid">
          <Skeleton height={196} /><Skeleton height={196} /><Skeleton height={196} />
        </div>
      )}

      {!error && data && data.length === 0 && (
        <EmptyState
          icon={Plug}
          title="No connectors yet"
          hint="Add an OpenAI, OpenRouter, Anthropic, or proxy connector to get started."
          action={<button className="btn btn-primary" onClick={() => setEditing('new')}><Plus size={15} /> Add connector</button>}
        />
      )}

      {!error && data && data.length > 0 && (
        <>
          {data.length > 4 && (
            <div className="panel-search llm-search">
              <Search size={14} />
              <input
                className="input"
                placeholder={`Search ${data.length} connectors…`}
                value={filter}
                spellCheck={false}
                onChange={(e) => setFilter(e.target.value)}
              />
              {filter && (
                <button className="icon-btn" onClick={() => setFilter('')} data-tip="Clear"><X size={13} /></button>
              )}
            </div>
          )}

          {shown.length === 0 ? (
            <EmptyState icon={Search} title="No matching connectors" hint={`Nothing matches “${filter.trim()}”.`} />
          ) : (
            <div className="llm-grid">
              {shown.map((p) => (
                <ProviderCard
                  key={p.name}
                  provider={p}
                  onActivate={handleActivate}
                  onTest={handleTest}
                  onEdit={(prov) => setEditing(prov)}
                  onDelete={handleDelete}
                />
              ))}
            </div>
          )}
        </>
      )}

      {editing && (
        <ProviderModal
          provider={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={onSaved}
        />
      )}
    </div>
  )
}

export default LLMProvidersPage
