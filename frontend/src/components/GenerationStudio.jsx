import { ListChecks, Code2, Layers, ChevronRight } from 'lucide-react'

// Tools surfaced in the Generation Studio. Only MCQ Generation is wired up
// today; the rest are placeholders that show where the studio is heading.
const TOOLS = [
  {
    key: 'mcq',
    page: 'mcq',
    icon: ListChecks,
    accent: 'var(--violet)',
    title: 'MCQ Generation',
    desc: 'Generate multiple-choice practice from a course topic and session using its ingested reading material.',
  },
  {
    key: 'coding',
    icon: Code2,
    accent: 'var(--cyan)',
    title: 'Coding Practice',
    desc: 'Generate hands-on coding problems and starter scaffolds from session content.',
    soon: true,
  },
  {
    key: 'flashcards',
    icon: Layers,
    accent: 'var(--amber)',
    title: 'Flashcards',
    desc: 'Turn key concepts in a session into spaced-repetition flashcards.',
    soon: true,
  },
]

// Landing hub for the generation tools. Each live tool card navigates to its
// own page; "soon" cards are disabled.
function GenerationStudio({ onNavigate }) {
  return (
    <div className="studio-page">
      <header className="topbar">
        <div>
          <h1>Generation Studio</h1>
          <p className="topbar-sub">
            Generate practice content from your ingested courses. Pick a tool to get started.
          </p>
        </div>
      </header>

      <div className="studio-grid">
        {TOOLS.map(({ key, page, icon: Icon, accent, title, desc, soon }) => (
          <button
            key={key}
            type="button"
            className={`studio-card ${soon ? 'soon' : ''}`}
            disabled={soon}
            title={soon ? 'Coming soon' : undefined}
            onClick={() => !soon && onNavigate(page)}
          >
            <span className="studio-card-icon" style={{ color: accent, background: `color-mix(in srgb, ${accent} 14%, transparent)` }}>
              <Icon size={20} />
            </span>
            <span className="studio-card-body">
              <span className="studio-card-title">
                {title}
                {soon && <span className="soon-pill">soon</span>}
              </span>
              <span className="studio-card-desc">{desc}</span>
            </span>
            {!soon && <ChevronRight size={16} className="studio-card-arrow" />}
          </button>
        ))}
      </div>
    </div>
  )
}

export default GenerationStudio
