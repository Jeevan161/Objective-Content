import {
  LayoutGrid,
  MessagesSquare,
  Sparkles,
  ClipboardCheck,
  Activity,
  History,
  Moon,
  Sun,
  BookOpenCheck,
  Workflow,
  Plug,
  X,
  ChevronsLeft,
  ChevronsRight,
} from 'lucide-react'

// Pages that exist today vs. stages of the workflow that are coming soon.
// Keeping them visible (but disabled) shows users where the product is going.
const NAV = [
  { key: 'courses', label: 'Courses', icon: LayoutGrid },
  { key: 'chat', label: 'Chat', icon: MessagesSquare },
  { key: 'generation', label: 'Generation Studio', icon: Sparkles },
  { key: 'runs', label: 'Runs', icon: History },
  { key: 'pipeline', label: 'MCQ Pipeline', icon: Workflow },
  { key: 'llm-providers', label: 'LLM Connectors', icon: Plug },
  { key: 'review', label: 'Review Queue', icon: ClipboardCheck, soon: true },
]

function Sidebar({
  page,
  onNavigate,
  activeJobCount,
  onOpenActivity,
  theme,
  onToggleTheme,
  open = false,
  onClose,
  collapsed = false,
  onToggleCollapse,
}) {
  // On mobile the sidebar is a drawer: navigating should dismiss it.
  const go = (key) => {
    onNavigate(key)
    onClose?.()
  }
  const openActivity = () => {
    onOpenActivity()
    onClose?.()
  }
  // When collapsed to the icon rail, surface the label as a hover tooltip.
  const tip = (label) => (collapsed ? { 'data-tip': label } : {})

  return (
    <aside className={`sidebar ${open ? 'open' : ''} ${collapsed ? 'collapsed' : ''}`}>
      <div className="sidebar-brand">
        <div className="brand-mark">
          <BookOpenCheck size={18} />
        </div>
        <div className="brand-text">
          <div className="brand-name">Objective Content</div>
          <div className="brand-sub">Generator Studio</div>
        </div>
        <button
          className="sidebar-close"
          onClick={onClose}
          aria-label="Close menu"
        >
          <X size={18} />
        </button>
      </div>

      <nav className="sidebar-nav">
        <div className="nav-section-label">Workspace</div>
        {NAV.map(({ key, label, icon: Icon, soon }) => (
          <button
            key={key}
            className={`nav-item ${page === key || (key === 'generation' && page === 'mcq') ? 'active' : ''}`}
            disabled={soon}
            title={soon ? 'Coming soon' : undefined}
            onClick={() => go(key)}
            {...tip(soon ? `${label} (soon)` : label)}
          >
            <Icon size={16} />
            <span>{label}</span>
            {soon && <span className="soon-pill">soon</span>}
          </button>
        ))}

        <div className="nav-section-label">Monitor</div>
        <button className="nav-item" onClick={openActivity} {...tip('Activity')}>
          <Activity size={16} />
          <span>Activity</span>
          {activeJobCount > 0 && (
            <span className="activity-count">{activeJobCount}</span>
          )}
        </button>
      </nav>

      <div className="sidebar-footer">
        <button
          className="nav-item"
          onClick={onToggleTheme}
          {...tip(theme === 'dark' ? 'Light mode' : 'Dark mode')}
        >
          {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
          <span>{theme === 'dark' ? 'Light mode' : 'Dark mode'}</span>
        </button>
        <button
          className="nav-item nav-collapse"
          onClick={onToggleCollapse}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          {...tip(collapsed ? 'Expand sidebar' : 'Collapse')}
        >
          {collapsed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}
          <span>Collapse</span>
        </button>
      </div>
    </aside>
  )
}

export default Sidebar
