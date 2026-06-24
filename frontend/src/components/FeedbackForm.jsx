import { useState } from 'react'
import { Send, ThumbsUp, ThumbsDown } from 'lucide-react'
import Modal from './Modal'
import { useToast } from './Toast'
import { submitAppFeedback } from '../api'

// Emoji rating scale, 1 (worst) → 5 (best). Index 0 is rating 1.
const EMOJIS = [
  { value: 1, glyph: '😞', label: 'Very unhappy' },
  { value: 2, glyph: '😕', label: 'Unhappy' },
  { value: 3, glyph: '😐', label: 'Neutral' },
  { value: 4, glyph: '🙂', label: 'Happy' },
  { value: 5, glyph: '😄', label: 'Very happy' },
]

const CATEGORIES = ['Generation Issue', 'Review', 'UI Related', 'Enhancement']

// Application-level feedback dialog: emoji rating + category + helpful yes/no + free text.
export default function FeedbackForm({ onClose }) {
  const toast = useToast()
  const [rating, setRating] = useState(0)
  const [category, setCategory] = useState('')
  const [helpful, setHelpful] = useState(null) // true | false | null
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit() {
    if (!rating && !message.trim()) {
      toast.push({ kind: 'error', title: 'Nothing to send', message: 'Pick a rating or write a note.' })
      return
    }
    setBusy(true)
    try {
      await submitAppFeedback({ rating, category, helpful, message: message.trim() })
      toast.push({ kind: 'success', title: 'Thanks for the feedback!', message: 'It’s been sent to the team.' })
      onClose?.()
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not send feedback', message: e.message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Share feedback"
      subtitle="Tell us how the studio is working for you."
      size="sm"
      onClose={onClose}
      footer={(
        <>
          <button className="btn btn-ghost" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn btn-primary" onClick={submit} disabled={busy}>
            <Send size={14} /> {busy ? 'Sending…' : 'Send feedback'}
          </button>
        </>
      )}
    >
      <div className="feedback-form">
        <div className="feedback-field">
          <label className="feedback-label">How was your experience?</label>
          <div className="emoji-rating" role="radiogroup" aria-label="Rating">
            {EMOJIS.map((e) => (
              <button
                key={e.value}
                type="button"
                role="radio"
                aria-checked={rating === e.value}
                aria-label={e.label}
                title={e.label}
                className={`emoji-btn ${rating === e.value ? 'selected' : ''} ${rating && rating !== e.value ? 'dimmed' : ''}`}
                onClick={() => setRating((v) => (v === e.value ? 0 : e.value))}
              >
                {e.glyph}
              </button>
            ))}
          </div>
        </div>

        <div className="feedback-field">
          <label className="feedback-label">Category</label>
          <div className="feedback-chips" role="radiogroup" aria-label="Category">
            {CATEGORIES.map((c) => (
              <button
                key={c}
                type="button"
                role="radio"
                aria-checked={category === c}
                className={`mcq-chip ${category === c ? 'active' : ''}`}
                onClick={() => setCategory((v) => (v === c ? '' : c))}
              >
                {c}
              </button>
            ))}
          </div>
        </div>

        <div className="feedback-field">
          <label className="feedback-label">Was this product helpful?</label>
          <div className="feedback-helpful" role="radiogroup" aria-label="Helpful">
            <button
              type="button"
              role="radio"
              aria-checked={helpful === true}
              className={`btn btn-sm ${helpful === true ? 'btn-primary' : 'btn-soft'}`}
              onClick={() => setHelpful((v) => (v === true ? null : true))}
            >
              <ThumbsUp size={13} /> Yes
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={helpful === false}
              className={`btn btn-sm ${helpful === false ? 'btn-primary' : 'btn-soft'}`}
              onClick={() => setHelpful((v) => (v === false ? null : false))}
            >
              <ThumbsDown size={13} /> No
            </button>
          </div>
        </div>

        <div className="feedback-field">
          <label className="feedback-label" htmlFor="feedback-message">Your feedback</label>
          <textarea
            id="feedback-message"
            className="input feedback-textarea"
            rows={4}
            placeholder="What went well, what didn’t, or what you’d like to see…"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
          />
        </div>
      </div>
    </Modal>
  )
}
