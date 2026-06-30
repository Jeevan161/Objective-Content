import { ShieldCheck } from 'lucide-react'
import Modal from './Modal'

// Extraction confirmation. Content is always fetched token-free through the
// content-loading admin panel (learning_resource ids come from its CSV), so no
// Bearer/access token is collected anymore — this is just a confirm step.
function TokenModal({ course, onClose, onSubmit, unitIds = null }) {
  function handleConfirm() {
    onSubmit(course, {}, unitIds)
    onClose()
  }

  return (
    <Modal
      title={unitIds ? 'Sync learning set content' : 'Extract learning resource content'}
      subtitle={
        unitIds ? (
          <>Re-fetch this learning set’s latest reading material via the admin panel.</>
        ) : (
          <>
            Reading material for <code>{course.course_name || course.course_id}</code> and all of its
            prerequisites will be fetched via the admin panel and stored.
          </>
        )
      }
      onClose={onClose}
    >
      <div className="form-stack">
        <div className="security-note">
          <ShieldCheck size={14} />
          Content is pulled through the content-loading admin panel — no access token required.
        </div>

        <div className="form-actions">
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" onClick={handleConfirm}>
            Extract via admin panel
          </button>
        </div>
      </div>
    </Modal>
  )
}

export default TokenModal
