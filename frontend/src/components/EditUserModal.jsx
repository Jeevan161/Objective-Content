import { useState } from 'react'
import { Save, KeyRound } from 'lucide-react'
import Modal from './Modal'
import { adminSetName, adminResetPassword } from '../api'
import { useToast } from './Toast'

// Admin-only "Edit user" dialog: rename the user and/or set a new password.
// Name and password are saved via independent endpoints, so each only fires
// when actually changed. onSaved() lets the parent refresh its list.
export default function EditUserModal({ user, onClose, onSaved }) {
  const toast = useToast()
  const [name, setName] = useState(user.name || '')
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [saving, setSaving] = useState(false)

  const nameChanged = name.trim() && name.trim() !== (user.name || '')
  const wantsPassword = pw.length > 0 || pw2.length > 0

  async function onSave() {
    if (wantsPassword) {
      if (pw.length < 8) {
        toast.push({ kind: 'error', title: 'Password too short', message: 'Use at least 8 characters.' })
        return
      }
      if (pw !== pw2) {
        toast.push({ kind: 'error', title: 'Passwords do not match', message: 'Re-enter the confirmation.' })
        return
      }
    }
    if (!nameChanged && !wantsPassword) {
      onClose?.()
      return
    }
    setSaving(true)
    try {
      if (nameChanged) await adminSetName(user.id, name.trim())
      if (wantsPassword) await adminResetPassword(user.id, pw)
      toast.push({ kind: 'success', title: 'User updated', message: user.email })
      onSaved?.()
      onClose?.()
    } catch (e) {
      toast.push({ kind: 'error', title: 'Update failed', message: e.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal title="Edit user" subtitle={user.email} size="sm" onClose={onClose}
      footer={(
        <>
          <button className="btn btn-soft" onClick={onClose} disabled={saving}>Cancel</button>
          <button className="btn btn-primary" onClick={onSave} disabled={saving}>
            <Save size={14} /> Save
          </button>
        </>
      )}>
      <div className="form-stack">
        <div className="field">
          <label className="field-label">Name</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)}
            placeholder="Full name" autoFocus />
        </div>
        <div className="field">
          <label className="field-label"><KeyRound size={12} /> New password</label>
          <input className="input" type="password" value={pw} onChange={(e) => setPw(e.target.value)}
            placeholder="Leave blank to keep current" autoComplete="new-password" />
          <span className="field-hint">Share the new password with the user directly — it is not emailed.</span>
        </div>
        <div className="field">
          <label className="field-label">Confirm password</label>
          <input className="input" type="password" value={pw2} onChange={(e) => setPw2(e.target.value)}
            placeholder="Re-enter new password" autoComplete="new-password" />
        </div>
      </div>
    </Modal>
  )
}
