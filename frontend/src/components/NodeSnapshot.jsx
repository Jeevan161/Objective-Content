// Renders one pipeline node's STATE DETAILS — the compact snapshot a node emits (counts + small
// samples) plus, when present, the LLM calls it made (prompt messages + response). Used by both the
// live progress board (McqProgress) and the completed-run trace (McqResults · TracePanel).
import { useState } from 'react'
import { ChevronRight } from 'lucide-react'

const isPlain = (v) => v === null || ['string', 'number', 'boolean'].includes(typeof v)
const labelize = (k) =>
  String(k).replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())

// One snapshot value rendered for human eyes: scalars inline, arrays as rows, objects as pairs.
function Value({ value }) {
  if (isPlain(value)) return <span className="nsv-scalar">{String(value)}</span>
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="nsv-muted">none</span>
    if (value.every(isPlain))
      return (
        <span className="nsv-chips">
          {value.map((v, i) => (
            <span key={i} className="nsv-chip">{String(v)}</span>
          ))}
        </span>
      )
    return (
      <ul className="nsv-list">
        {value.map((v, i) => (
          <li key={i} className="nsv-list-item">
            {isPlain(v) ? (
              String(v)
            ) : (
              <span className="nsv-pairs">
                {Object.entries(v).map(([k, val]) => (
                  <span key={k} className="nsv-pair">
                    <span className="nsv-pk">{k}:</span>{' '}
                    {isPlain(val) ? String(val) : Array.isArray(val) ? (val.join(', ') || '—') : JSON.stringify(val)}
                  </span>
                ))}
              </span>
            )}
          </li>
        ))}
      </ul>
    )
  }
  // nested object
  return (
    <span className="nsv-pairs">
      {Object.entries(value).map(([k, val]) => (
        <span key={k} className="nsv-pair">
          <span className="nsv-pk">{k}:</span> {isPlain(val) ? String(val) : JSON.stringify(val)}
        </span>
      ))}
    </span>
  )
}

// One LLM call: the prompt messages + the model response, each collapsible.
function LlmCall({ call, idx }) {
  const [open, setOpen] = useState(false)
  const msgs = call.messages || []
  return (
    <div className={`nsv-llm-call ${open ? 'open' : ''}`}>
      <button className="nsv-llm-head" onClick={() => setOpen((o) => !o)}>
        <ChevronRight size={12} className="nsv-chevron" />
        <span className="nsv-llm-title">LLM call {idx + 1}</span>
        {typeof call.temperature === 'number' && (
          <span className="nsv-llm-temp">temp {call.temperature}</span>
        )}
        <span className="nsv-llm-peek">{(msgs[msgs.length - 1]?.content || '').slice(0, 60)}</span>
      </button>
      {open && (
        <div className="nsv-llm-body">
          {msgs.map((m, i) => (
            <div key={i} className="nsv-llm-msg">
              <div className={`nsv-llm-role role-${m.role}`}>{m.role}</div>
              <pre className="nsv-pre">{m.content}</pre>
            </div>
          ))}
          <div className="nsv-llm-msg">
            <div className="nsv-llm-role role-response">response</div>
            <pre className="nsv-pre">{call.response}</pre>
          </div>
        </div>
      )}
    </div>
  )
}

function NodeSnapshot({ snapshot }) {
  if (!snapshot || typeof snapshot !== 'object' || !Object.keys(snapshot).length)
    return <p className="nsv-empty">No state details captured for this step.</p>

  const { llm_calls: llmCalls, llm_calls_truncated: llmTrunc, ...rest } = snapshot
  const entries = Object.entries(rest).filter(([, v]) => v !== undefined && v !== null)

  return (
    <div className="node-snapshot">
      {entries.length > 0 && (
        <dl className="nsv-fields">
          {entries.map(([k, v]) => (
            <div key={k} className="nsv-field">
              <dt className="nsv-key">{labelize(k)}</dt>
              <dd className="nsv-val">
                <Value value={v} />
              </dd>
            </div>
          ))}
        </dl>
      )}
      {Array.isArray(llmCalls) && llmCalls.length > 0 && (
        <div className="nsv-llm">
          <div className="nsv-llm-section">
            LLM I/O · {llmCalls.length} call{llmCalls.length === 1 ? '' : 's'}
            {llmTrunc ? ` (+${llmTrunc} more not shown)` : ''}
          </div>
          {llmCalls.map((c, i) => (
            <LlmCall key={i} call={c} idx={i} />
          ))}
        </div>
      )}
    </div>
  )
}

export default NodeSnapshot
