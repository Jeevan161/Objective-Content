import { useCallback, useEffect, useState } from 'react'
import { KeyRound, Check } from 'lucide-react'
import { fetchMyKeys, setConnectorKey } from '../api'
import { useToast } from './Toast'

// Manage the current user's API key for each LLM connector. One key per connector;
// the ACTIVE connector's key is what generation uses. Keys are write-only.
export default function ApiKeyForm() {
  const toast = useToast()
  const [keys, setKeys] = useState(null)
  const [drafts, setDrafts] = useState({})
  const [busy, setBusy] = useState(null)

  const load = useCallback(async () => {
    try {
      setKeys(await fetchMyKeys())
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not load connectors', message: e.message })
    }
  }, [toast])

  useEffect(() => { load() }, [load])

  async function save(p) {
    const v = (drafts[p.provider_id] || '').trim()
    if (!v) {
      toast.push({ kind: 'error', title: 'Key required', message: `Paste your ${p.name} key.` })
      return
    }
    setBusy(p.provider_id)
    try {
      await setConnectorKey(p.provider_id, v)
      setDrafts((d) => ({ ...d, [p.provider_id]: '' }))
      await load()
      toast.push({ kind: 'success', title: 'Key saved', message: `${p.name} key stored.` })
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not save', message: e.message })
    } finally {
      setBusy(null)
    }
  }

  if (keys === null) {
    return <div className="apikey-list"><p className="apikey-hint">Loading connectors…</p></div>
  }

  return (
    <div className="apikey-list">
      <div className="apikey-list-head"><KeyRound size={13} /> Your LLM connector keys</div>
      {keys.map((p) => (
        <div key={p.provider_id} className={`apikey-conn ${p.active ? 'active' : ''}`}>
          <div className="apikey-conn-meta">
            <span className="apikey-conn-name">
              {p.name}{p.active && <span className="apikey-active-tag">active</span>}
            </span>
            <span className="apikey-conn-model">{p.model || p.adapter}</span>
            {p.has_key && <span className="apikey-set"><Check size={12} /> set</span>}
          </div>
          <div className="apikey-row">
            <input
              className="input" type="password" spellCheck={false}
              placeholder={p.has_key ? 'Replace key…' : 'API key…'}
              value={drafts[p.provider_id] || ''}
              onChange={(e) => setDrafts((d) => ({ ...d, [p.provider_id]: e.target.value }))}
              onKeyDown={(e) => e.key === 'Enter' && save(p)}
            />
            <button className="btn btn-primary btn-sm" disabled={busy === p.provider_id}
              onClick={() => save(p)}>
              {busy === p.provider_id ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      ))}
      <p className="apikey-hint">
        Only the key is per-user — model, base URL and proxy settings are shared. Keys are stored
        encrypted and never shown again. The <b>active</b> connector's key is required to generate.
      </p>
    </div>
  )
}
