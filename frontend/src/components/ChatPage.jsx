import { useEffect, useRef, useState } from 'react'
import { MessagesSquare, SendHorizonal, BookOpen, Sparkles } from 'lucide-react'
import { ragAnswer } from '../api'
import { EmptyState, Spinner } from './ui'
import { useToast } from './Toast'

// One source citation pulled from the answer's retrieved sections.
function SourceChip({ src }) {
  const where = [src.course_name, src.unit_label || src.part_name, src.section]
    .filter(Boolean)
    .join(' › ')
  return <span className="chat-source" title={where}>{where || src.course_id}</span>
}

// Conversational RAG over a course's ingested reading material. The selected
// course's prerequisites are searched too (handled server-side).
function ChatPage({ courses }) {
  const toast = useToast()
  const [courseId, setCourseId] = useState('')
  const [query, setQuery] = useState('')
  const [sending, setSending] = useState(false)
  // Turn list: { role: 'user' | 'assistant', text, sources? }.
  const [turns, setTurns] = useState([])
  const threadRef = useRef(null)

  // Default to the first course once they load.
  useEffect(() => {
    if (!courseId && courses && courses.length > 0) {
      setCourseId(courses[0].course_id)
    }
  }, [courses, courseId])

  // Keep the latest turn in view.
  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: 'smooth' })
  }, [turns, sending])

  const selected = courses?.find((c) => c.course_id === courseId) || null

  async function send(e) {
    e?.preventDefault()
    const q = query.trim()
    if (!q || !courseId || sending) return
    setQuery('')
    setTurns((prev) => [...prev, { role: 'user', text: q }])
    setSending(true)
    try {
      const res = await ragAnswer([courseId], q)
      setTurns((prev) => [
        ...prev,
        { role: 'assistant', text: res.answer, sources: res.sources || [] },
      ])
    } catch (err) {
      toast.push({ kind: 'error', title: 'Chat failed', message: err.message })
      setTurns((prev) => [
        ...prev,
        { role: 'assistant', text: `Sorry — that request failed: ${err.message}`, sources: [] },
      ])
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="chat-page">
      <header className="topbar">
        <div>
          <h1>Chat</h1>
          <p className="topbar-sub">
            Ask questions answered from a course's ingested reading material. Its prerequisites
            are searched too, and answers cite the sections they came from.
          </p>
        </div>
        <div className="topbar-actions">
          <label className="chat-course-select">
            <BookOpen size={14} />
            <select
              className="input"
              value={courseId}
              onChange={(e) => setCourseId(e.target.value)}
            >
              {(courses || []).map((c) => (
                <option key={c.course_id} value={c.course_id}>
                  {c.course_name || c.course_id}
                </option>
              ))}
            </select>
          </label>
        </div>
      </header>

      {!courses || courses.length === 0 ? (
        <EmptyState
          icon={MessagesSquare}
          title="No courses to chat with"
          hint="Add a course, extract its content and ingest it into the RAG index first. Then come back here to ask questions about it."
        />
      ) : (
        <div className="chat-shell">
          <div className="chat-thread" ref={threadRef}>
            {turns.length === 0 && (
              <div className="chat-intro">
                <div className="chat-intro-icon">
                  <Sparkles size={20} />
                </div>
                <p>
                  Ask anything about{' '}
                  <strong>{selected?.course_name || courseId}</strong>. For example,
                  &ldquo;How do list comprehensions work?&rdquo; or &ldquo;Show the syntax for
                  a for loop.&rdquo;
                </p>
                {selected && selected.prerequisite_count > 0 && (
                  <span className="muted">
                    {selected.prerequisite_count} prerequisite
                    {selected.prerequisite_count === 1 ? '' : 's'} included in the search.
                  </span>
                )}
              </div>
            )}

            {turns.map((t, i) => (
              <div key={i} className={`chat-msg chat-msg-${t.role}`}>
                <div className="chat-bubble">{t.text}</div>
                {t.role === 'assistant' && t.sources && t.sources.length > 0 && (
                  <div className="chat-sources">
                    <span className="chat-sources-label">Sources</span>
                    {t.sources.map((s, j) => (
                      <SourceChip key={j} src={s} />
                    ))}
                  </div>
                )}
              </div>
            ))}

            {sending && (
              <div className="chat-msg chat-msg-assistant">
                <div className="chat-bubble chat-bubble-loading">
                  <Spinner size={14} /> Searching the course materials…
                </div>
              </div>
            )}
          </div>

          <form className="chat-composer" onSubmit={send}>
            <input
              className="input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={`Ask about ${selected?.course_name || 'this course'}…`}
              disabled={sending}
              spellCheck={false}
            />
            <button
              type="submit"
              className="btn btn-primary"
              disabled={sending || !query.trim()}
            >
              {sending ? <Spinner size={14} /> : <SendHorizonal size={15} />}
              Send
            </button>
          </form>
        </div>
      )}
    </div>
  )
}

export default ChatPage
