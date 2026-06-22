import { createContext, useCallback, useContext, useRef, useState } from 'react'
import { CheckCircle2, Info, AlertTriangle, XCircle, X } from 'lucide-react'

// Lightweight toast system: wrap the app in <ToastProvider>, then call
// useToast().push({ kind, title, message }) anywhere below it.
const ToastContext = createContext(null)

const ICONS = {
  success: CheckCircle2,
  info: Info,
  warning: AlertTriangle,
  error: XCircle,
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])
  const nextId = useRef(1)

  const dismiss = useCallback((id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const push = useCallback(
    ({ kind = 'info', title, message, duration = 5000 }) => {
      const id = nextId.current++
      setToasts((prev) => [...prev, { id, kind, title, message }])
      if (duration) setTimeout(() => dismiss(id), duration)
      return id
    },
    [dismiss],
  )

  return (
    <ToastContext.Provider value={{ push, dismiss }}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((t) => {
          const Icon = ICONS[t.kind] || Info
          return (
            <div key={t.id} className={`toast toast-${t.kind}`}>
              <Icon size={17} className="toast-icon" />
              <div className="toast-body">
                {t.title && <div className="toast-title">{t.title}</div>}
                {t.message && <div className="toast-msg">{t.message}</div>}
              </div>
              <button className="icon-btn" onClick={() => dismiss(t.id)} aria-label="Dismiss">
                <X size={14} />
              </button>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}

// eslint-disable-next-line react-refresh/only-export-components -- companion hook for the provider
export function useToast() {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>')
  return ctx
}
