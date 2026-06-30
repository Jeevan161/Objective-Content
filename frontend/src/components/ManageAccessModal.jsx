import { useEffect, useState } from 'react'
import { Crown, Mail, Trash2, UserPlus, Users } from 'lucide-react'
import Modal from './Modal'
import { Skeleton, Spinner } from './ui'
import { useToast } from './Toast'
import {
  addCourseCollaborator,
  getCourseCollaborators,
  removeCourseCollaborator,
} from '../api'

// Owner/admin surface to grant or revoke who can work on (generate content for) a
// course. Adding a collaborator by email takes effect immediately — no approval.
function ManageAccessModal({ course, onClose }) {
  const toast = useToast()
  const [data, setData] = useState(null) // null = loading
  const [email, setEmail] = useState('')
  const [adding, setAdding] = useState(false)
  const [removingId, setRemovingId] = useState(null)

  async function load() {
    try {
      setData(await getCourseCollaborators(course.course_id))
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not load access', message: e.message })
      onClose()
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch; state set after await
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [course.course_id])

  async function handleAdd(e) {
    e.preventDefault()
    const value = email.trim()
    if (!value || adding) return
    setAdding(true)
    try {
      const added = await addCourseCollaborator(course.course_id, value)
      toast.push({ kind: 'success', title: 'Access granted', message: `${added.email} can now work on this course.` })
      setEmail('')
      await load()
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not grant access', message: e.message })
    } finally {
      setAdding(false)
    }
  }

  async function handleRemove(u) {
    setRemovingId(u.id)
    try {
      await removeCourseCollaborator(course.course_id, u.id)
      toast.push({ kind: 'info', title: 'Access revoked', message: `${u.email} can no longer work on this course.` })
      await load()
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not revoke access', message: e.message })
    } finally {
      setRemovingId(null)
    }
  }

  const canManage = data?.can_manage

  return (
    <Modal
      title="Manage course access"
      subtitle={
        <>
          Choose who can generate content for{' '}
          <code>{course.course_name || course.course_id}</code>. Access is immediate — no
          approval needed.
        </>
      }
      onClose={onClose}
    >
      {data === null ? (
        <div className="form-stack">
          <Skeleton height={42} />
          <Skeleton height={42} width="80%" />
        </div>
      ) : (
        <div className="form-stack">
          <div className="access-list">
            {data.owner && (
              <div className="access-row">
                <span className="access-who">
                  <Crown size={14} className="access-owner-icon" />
                  <span className="access-name">{data.owner.name || data.owner.email}</span>
                  <span className="access-email">{data.owner.email}</span>
                </span>
                <span className="badge badge-latest">owner</span>
              </div>
            )}

            {data.collaborators.length === 0 ? (
              <p className="muted access-empty">
                <Users size={13} /> No collaborators yet — only the owner and admins can
                work on this course.
              </p>
            ) : (
              data.collaborators.map((u) => (
                <div className="access-row" key={u.id}>
                  <span className="access-who">
                    <Mail size={13} className="access-collab-icon" />
                    <span className="access-name">{u.name || u.email}</span>
                    <span className="access-email">{u.email}</span>
                  </span>
                  {canManage && (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      disabled={removingId === u.id}
                      onClick={() => handleRemove(u)}
                      data-tip="Revoke access"
                    >
                      {removingId === u.id ? <Spinner size={13} /> : <Trash2 size={13} />}
                    </button>
                  )}
                </div>
              ))
            )}
          </div>

          {canManage && (
            <form onSubmit={handleAdd} className="access-add">
              <input
                className="input"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="Add a teammate by email…"
                spellCheck={false}
                autoComplete="off"
              />
              <button className="btn btn-primary" type="submit" disabled={!email.trim() || adding}>
                {adding ? <Spinner size={13} /> : <UserPlus size={14} />}
                {adding ? 'Adding…' : 'Add'}
              </button>
            </form>
          )}
        </div>
      )}
    </Modal>
  )
}

export default ManageAccessModal
