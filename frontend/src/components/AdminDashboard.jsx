import { useCallback, useEffect, useState } from 'react'
import { ShieldCheck, UserCheck, UserX, RefreshCw, KeyRound } from 'lucide-react'
import {
  adminStats, adminApproveUser, adminDeactivateUser, adminSetRole, adminLogs,
} from '../api'
import { useAuth } from '../auth/AuthContext'
import { useToast } from './Toast'
import { Spinner, EmptyState } from './ui'

export default function AdminDashboard() {
  const { user } = useAuth()
  const toast = useToast()
  const [stats, setStats] = useState(null)
  const [logs, setLogs] = useState([])
  const [level, setLevel] = useState('')
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [s, l] = await Promise.all([adminStats(), adminLogs(level)])
      setStats(s)
      setLogs(l)
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not load dashboard', message: e.message })
    } finally {
      setLoading(false)
    }
  }, [level, toast])

  useEffect(() => { load() }, [load])

  async function act(id, fn, label) {
    setBusyId(id)
    try {
      await fn(id)
      await load()
    } catch (e) {
      toast.push({ kind: 'error', title: `${label} failed`, message: e.message })
    } finally {
      setBusyId(null)
    }
  }

  if (user?.role !== 'admin') {
    return <main className="main"><EmptyState title="Admins only" message="You don't have access to this page." /></main>
  }

  return (
    <>
      <header className="topbar">
        <div>
          <h1>Admin</h1>
          <p className="topbar-sub">Approve users, manage roles & API keys, and review per-user activity + task logs.</p>
        </div>
        <div className="topbar-actions">
          <button className="btn btn-soft btn-sm" onClick={load} disabled={loading}>
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
      </header>

      {loading && !stats ? (
        <div className="admin-loading"><Spinner size={20} /></div>
      ) : (
        <>
          <div className="stats-row">
            <div className="stat-card"><div className="stat-value">{stats?.total_generations ?? 0}</div><div className="stat-label">Generations</div></div>
            <div className="stat-card"><div className="stat-value">{stats?.total_loads ?? 0}</div><div className="stat-label">Loads</div></div>
            <div className="stat-card"><div className="stat-value">{stats?.pending_approval ?? 0}</div><div className="stat-label">Pending approval</div></div>
            <div className="stat-card"><div className="stat-value">{stats?.needs_api_key ?? 0}</div><div className="stat-label">Need API key</div></div>
          </div>

          <div className="admin-section">
            <h2 className="admin-h2">Users</h2>
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr><th>User</th><th>Role</th><th>Status</th><th>API key</th><th>Gen</th><th>Loads</th><th>Actions</th></tr>
                </thead>
                <tbody>
                  {(stats?.users || []).map((u) => (
                    <tr key={u.id}>
                      <td>
                        <div className="admin-user-name">{u.name || '—'}</div>
                        <div className="admin-user-email">{u.email}</div>
                      </td>
                      <td>
                        <span className={`mcq-status-chip ${u.role === 'admin' ? 'ok' : ''}`}>
                          {u.role === 'admin' && <ShieldCheck size={12} />} {u.role}
                        </span>
                      </td>
                      <td>
                        <span className={`mcq-status-chip ${u.is_active ? 'ok' : 'warn'}`}>
                          {u.is_active ? 'active' : 'pending'}
                        </span>
                      </td>
                      <td>{u.has_active_key
                        ? <span className="admin-key-ok"><KeyRound size={12} /> set</span>
                        : <span className="admin-key-missing">missing</span>}</td>
                      <td className="admin-num">{u.generations}</td>
                      <td className="admin-num">{u.loads}</td>
                      <td>
                        <div className="admin-actions">
                          {u.is_active
                            ? <button className="btn btn-soft btn-sm" disabled={busyId === u.id}
                                onClick={() => act(u.id, adminDeactivateUser, 'Deactivate')}>
                                <UserX size={13} /> Deactivate</button>
                            : <button className="btn btn-primary btn-sm" disabled={busyId === u.id}
                                onClick={() => act(u.id, adminApproveUser, 'Approve')}>
                                <UserCheck size={13} /> Approve</button>}
                          <button className="btn btn-ghost btn-sm" disabled={busyId === u.id}
                            onClick={() => act(u.id, (id) => adminSetRole(id, u.role === 'admin' ? 'user' : 'admin'), 'Role change')}>
                            {u.role === 'admin' ? 'Make user' : 'Make admin'}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="admin-section">
            <div className="admin-logs-head">
              <h2 className="admin-h2">Task logs</h2>
              <div className="admin-log-filters">
                {['', 'INFO', 'WARNING', 'ERROR'].map((lv) => (
                  <button key={lv || 'all'} className={`mcq-chip ${level === lv ? 'active' : ''}`}
                    onClick={() => setLevel(lv)}>{lv || 'all'}</button>
                ))}
              </div>
            </div>
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr><th>When</th><th>Type</th><th>Level</th><th>Event</th><th>Message</th></tr>
                </thead>
                <tbody>
                  {logs.map((l) => (
                    <tr key={l.id}>
                      <td className="admin-log-time">{new Date(l.created_at).toLocaleString()}</td>
                      <td>{l.task_type}</td>
                      <td><span className={`log-level log-${l.level.toLowerCase()}`}>{l.level}</span></td>
                      <td>{l.event}</td>
                      <td className="admin-log-msg" title={l.message}>{l.message}</td>
                    </tr>
                  ))}
                  {logs.length === 0 && <tr><td colSpan={5} className="admin-empty">No logs.</td></tr>}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </>
  )
}
