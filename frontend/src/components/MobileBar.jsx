import { Menu, Activity, BookOpenCheck } from 'lucide-react'

// Compact top bar shown only on small screens (hidden on desktop via CSS).
// Gives every page a way to open the nav drawer and the activity feed —
// the desktop sidebar is off-canvas on mobile.
function MobileBar({ onOpenNav, activeJobCount, onOpenActivity }) {
  return (
    <header className="mobile-bar">
      <button className="mobile-bar-btn" onClick={onOpenNav} aria-label="Open menu">
        <Menu size={20} />
      </button>

      <div className="mobile-bar-brand">
        <div className="brand-mark sm">
          <BookOpenCheck size={15} />
        </div>
        <span>Objective Content</span>
      </div>

      <button
        className="mobile-bar-btn"
        onClick={onOpenActivity}
        aria-label="Activity"
      >
        <Activity size={19} />
        {activeJobCount > 0 && (
          <span className="mobile-bar-badge">{activeJobCount}</span>
        )}
      </button>
    </header>
  )
}

export default MobileBar
