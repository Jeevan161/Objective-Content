import { useEffect, useRef, useState } from 'react'
import { ChevronDown, Check } from 'lucide-react'

// A themed dropdown that replaces the native <select> so the OPEN option list is
// styled too (native popups are OS-rendered and ignore our CSS). API mirrors a
// controlled select: `value`, `onChange(value)`, `options: [{value, label}]`.
export default function Select({
  value, onChange, options = [], placeholder = 'Select…', disabled = false, dataTip,
}) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)   // keyboard-highlighted index
  const ref = useRef(null)
  const selected = options.find((o) => o.value === value) || null

  // Close on outside click (mousedown so it beats the option's own handler).
  useEffect(() => {
    if (!open) return undefined
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  function openMenu() {
    setActive(options.findIndex((o) => o.value === value))   // highlight current on open
    setOpen(true)
  }
  function toggle() { if (open) setOpen(false); else openMenu() }
  function choose(opt) { onChange(opt.value); setOpen(false) }

  function onKey(e) {
    if (disabled) return
    if (e.key === 'Escape') { setOpen(false); return }
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      if (open && active >= 0 && options[active]) choose(options[active])
      else openMenu()
      return
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      if (!open) { openMenu(); return }
      const n = options.length
      if (!n) return
      setActive((i) => {
        let next = e.key === 'ArrowDown' ? i + 1 : i - 1
        if (next < 0) next = n - 1
        if (next >= n) next = 0
        return next
      })
    }
  }

  return (
    <div className={`select ${open ? 'open' : ''} ${disabled ? 'disabled' : ''}`} ref={ref} data-tip={dataTip}>
      <button type="button" className="select-trigger input" disabled={disabled}
        aria-haspopup="listbox" aria-expanded={open}
        onClick={() => !disabled && toggle()} onKeyDown={onKey}>
        <span className={`select-value ${selected ? '' : 'placeholder'}`}>
          {selected ? selected.label : placeholder}
        </span>
        <ChevronDown size={16} className="select-caret" />
      </button>
      {open && !disabled && (
        <ul className="select-panel" role="listbox">
          {options.length === 0 && <li className="select-empty">No options</li>}
          {options.map((o, i) => (
            <li key={`${o.value}-${i}`} role="option" aria-selected={o.value === value}
              className={`select-option ${o.value === value ? 'selected' : ''} ${i === active ? 'active' : ''}`}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => { e.preventDefault(); choose(o) }}>
              <span className="select-option-label">{o.label}</span>
              {o.value === value && <Check size={14} className="select-check" />}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
