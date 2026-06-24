import { LogOut, ShieldCheck } from 'lucide-react'
import Modal from './Modal'
import ApiKeyForm from './ApiKeyForm'
import { useAuth } from '../auth/AuthContext'

// Account dialog: shows the signed-in user, manages their personal API key, logout.
export default function AccountModal({ onClose }) {
  const { user, logout } = useAuth()
  return (
    <Modal title="Account" subtitle={user?.email} size="sm" onClose={onClose}
      footer={(
        <button className="btn btn-soft" onClick={() => { logout(); onClose?.() }}>
          <LogOut size={14} /> Sign out
        </button>
      )}>
      <div className="account-row">
        <span className="account-name">{user?.name || user?.email}</span>
        <span className={`mcq-status-chip ${user?.role === 'admin' ? 'ok' : ''}`}>
          {user?.role === 'admin' && <ShieldCheck size={12} />} {user?.role}
        </span>
      </div>
      <ApiKeyForm />
    </Modal>
  )
}
