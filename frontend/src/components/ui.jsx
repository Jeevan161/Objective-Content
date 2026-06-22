import { Loader2 } from 'lucide-react'

// ---- Small shared UI primitives ----

// Environment badge: PROD / BETA, colour-coded.
export function EnvBadge({ env }) {
  if (!env) return null
  return <span className={`env-badge env-${env.toLowerCase()}`}>{env}</span>
}

// Inline spinner used inside buttons and status rows.
export function Spinner({ size = 15 }) {
  return <Loader2 size={size} className="spin" aria-label="Loading" />
}

// PROD/BETA (or any option list) segmented control.
export function Segmented({ options, value, onChange }) {
  return (
    <div className="segmented" role="radiogroup">
      {options.map((opt) => (
        <button
          type="button"
          key={opt}
          role="radio"
          aria-checked={value === opt}
          className={`segmented-option ${value === opt ? 'active' : ''}`}
          onClick={() => onChange(opt)}
        >
          {opt}
        </button>
      ))}
    </div>
  )
}

// Grey shimmering placeholder block.
export function Skeleton({ width = '100%', height = 14, style }) {
  return <div className="skeleton" style={{ width, height, ...style }} />
}

// Centered empty/placeholder state with an icon and optional action.
export function EmptyState({ icon: Icon, title, hint, action }) {
  return (
    <div className="empty-state">
      {Icon && (
        <div className="empty-state-icon">
          <Icon size={26} strokeWidth={1.6} />
        </div>
      )}
      <h3>{title}</h3>
      {hint && <p className="empty-state-hint">{hint}</p>}
      {action}
    </div>
  )
}
