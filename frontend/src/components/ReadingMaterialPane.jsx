import { useEffect, useState } from 'react'
import { FileText, AlertCircle } from 'lucide-react'
import { Spinner } from './ui'
import { getUnitContent } from '../api'
import ReactMarkdown from 'react-markdown'

// Markdown previewer for reading material content
function Md({ children }) {
  const text = typeof children === 'string' ? children : (children ?? '')
  if (!text.trim()) return null
  return <div className="md"><ReactMarkdown>{text}</ReactMarkdown></div>
}

function ReadingMaterialPane({ courseId, unitId }) {
  const [content, setContent] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!courseId || !unitId) {
      setContent(null)
      setError('')
      return
    }
    setLoading(true)
    setError('')
    getUnitContent(courseId, unitId)
      .then((data) => {
        setContent(data)
      })
      .catch((e) => {
        setError(e.message)
      })
      .finally(() => {
        setLoading(false)
      })
  }, [courseId, unitId])

  return (
    <aside className="mcq-split-reading">
      <div className="mcq-reading-header">
        <FileText size={14} />
        <h3>{content?.title || 'Reading material'}</h3>
        {content?.content_chars ? (
          <span className="mcq-reading-meta">{(content.content_chars / 1000).toFixed(1)}k chars</span>
        ) : null}
      </div>
      <div className="mcq-reading-body">
        {loading && (
          <div className="mcq-reading-loading"><Spinner size={14} /> Loading…</div>
        )}
        {error && (
          <div className="mcq-reading-error"><AlertCircle size={13} /> {error}</div>
        )}
        {!loading && !error && content?.content && <Md>{content.content}</Md>}
        {!loading && !error && !content?.content && (
          <div className="mcq-reading-empty">No reading material for this session.</div>
        )}
      </div>
    </aside>
  )
}

export default ReadingMaterialPane
