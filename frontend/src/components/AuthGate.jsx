import { useState } from 'react'
import { BookOpenCheck, LogOut, Clock } from 'lucide-react'
import { useAuth } from '../auth/AuthContext'
import { useToast } from './Toast'
import { Spinner } from './ui'
import ApiKeyForm from './ApiKeyForm'

// Login / register card shown when there's no authenticated user.
function AuthScreen() {
  const { login, register } = useAuth()
  const toast = useToast()
  const [mode, setMode] = useState('login') // login | register
  const [form, setForm] = useState({ email: '', password: '', name: '' })
  const [busy, setBusy] = useState(false)
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    try {
      if (mode === 'login') {
        await login(form.email, form.password)
      } else {
        await register(form.email, form.password, form.name)
        toast.push({ kind: 'success', title: 'Account created',
          message: 'An admin must approve it before you can generate.' })
        setMode('login')
      }
    } catch (err) {
      toast.push({ kind: 'error', title: mode === 'login' ? 'Login failed' : 'Registration failed',
        message: err.message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="brand-mark"><BookOpenCheck size={20} /></div>
          <div>
            <div className="auth-title">Objective Content</div>
            <div className="auth-sub">Generator Studio</div>
          </div>
        </div>

        <div className="auth-tabs">
          <button className={`auth-tab ${mode === 'login' ? 'active' : ''}`}
            onClick={() => setMode('login')}>Sign in</button>
          <button className={`auth-tab ${mode === 'register' ? 'active' : ''}`}
            onClick={() => setMode('register')}>Register</button>
        </div>

        <form className="auth-form" onSubmit={submit}>
          {mode === 'register' && (
            <label>Name
              <input className="input" value={form.name} onChange={set('name')} placeholder="Your name" />
            </label>
          )}
          <label>Email
            <input className="input" type="email" required value={form.email} onChange={set('email')}
              placeholder="you@nxtwave.co.in" autoComplete="username" />
          </label>
          <label>Password
            <input className="input" type="password" required value={form.password} onChange={set('password')}
              placeholder="••••••••" autoComplete={mode === 'login' ? 'current-password' : 'new-password'} />
          </label>
          <button className="btn btn-primary auth-submit" type="submit" disabled={busy}>
            {busy ? <Spinner size={14} /> : (mode === 'login' ? 'Sign in' : 'Create account')}
          </button>
        </form>
        <p className="auth-foot">
          {mode === 'login'
            ? 'New here? Register, then an admin will approve your account.'
            : 'Already have an account? Sign in.'}
        </p>
      </div>
    </div>
  )
}

// Shown to a signed-in user whose account hasn't been approved yet.
function PendingScreen() {
  const { user, logout } = useAuth()
  return (
    <div className="auth-screen">
      <div className="auth-card">
        <div className="auth-pending-icon"><Clock size={26} /></div>
        <div className="auth-title">Awaiting approval</div>
        <p className="auth-sub">
          Hi {user?.name || user?.email} — your account is pending admin approval. You can set your
          API key now so you're ready to generate once approved.
        </p>
        <ApiKeyForm />
        <button className="btn btn-soft btn-sm auth-logout" onClick={logout}>
          <LogOut size={14} /> Sign out
        </button>
      </div>
    </div>
  )
}

// Gates the whole app: spinner → login/register → pending → app.
export default function AuthGate({ children }) {
  const { user, loading } = useAuth()
  if (loading) {
    return <div className="auth-screen"><Spinner size={22} /></div>
  }
  if (!user) return <AuthScreen />
  if (!user.is_active) return <PendingScreen />
  return children
}
