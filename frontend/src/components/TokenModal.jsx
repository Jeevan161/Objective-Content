import { useEffect, useState } from 'react'
import { Eye, EyeOff, KeyRound, ShieldCheck } from 'lucide-react'
import Modal from './Modal'
import { EnvBadge, Skeleton } from './ui'
import { getExtractInfo } from '../api'
import { useToast } from './Toast'

// Collects one Bearer token per environment that the course + its
// prerequisites span, then kicks off the extraction job.
function TokenModal({ course, onClose, onSubmit, unitIds = null, requireEnv = null }) {
  const toast = useToast()
  const [envs, setEnvs] = useState(null) // null = loading
  const [tokenRequired, setTokenRequired] = useState([]) // envs that MUST have a token
  const [tokens, setTokens] = useState({})
  const [shown, setShown] = useState({})

  useEffect(() => {
    // Scoped per-unit sync: we already know the one environment that needs a
    // token — no need to query course-wide extract-info.
    if (requireEnv) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- seed the single scoped env
      setEnvs([requireEnv])
      setTokenRequired([requireEnv])
      return
    }
    let cancelled = false
    getExtractInfo(course.course_id)
      .then((info) => {
        if (cancelled) return
        setEnvs(info.environments)
        setTokenRequired(info.token_required || [])
      })
      .catch((err) => {
        toast.push({ kind: 'error', title: 'Could not check environments', message: err.message })
        onClose()
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [course.course_id, requireEnv])

  // Only the required environments must be filled; the rest can extract
  // token-free via the admin panel (or take an optional token for tutorials).
  const ready = envs !== null && tokenRequired.every((env) => (tokens[env] || '').trim())
  const anyToken = Object.values(tokens).some((t) => (t || '').trim())

  function handleSubmit(e) {
    e.preventDefault()
    if (!ready) return
    const clean = {}
    for (const env of envs) {
      const t = (tokens[env] || '').trim()
      if (t) clean[env] = t
    }
    onSubmit(course, clean, unitIds)
    onClose()
  }

  return (
    <Modal
      title="Extract learning resource content"
      subtitle={
        <>
          Reading material for <code>{course.course_name || course.course_id}</code> and all of its
          prerequisites will be fetched and stored.
        </>
      }
      onClose={onClose}
    >
      <form onSubmit={handleSubmit} className="form-stack">
        {envs === null ? (
          <div className="form-stack">
            <Skeleton height={18} width="60%" />
            <Skeleton height={38} />
          </div>
        ) : (
          <>
            <p className="field-hint">
              {tokenRequired.length === 0
                ? 'Stored learning resource ids are available — this can extract token-free via the admin panel. Add a Bearer token for any environment to also pull tutorial content.'
                : 'A Bearer token is required for the environment(s) below that have no stored learning resource ids yet. Others can extract token-free via the admin panel.'}
            </p>

            {envs.map((env) => {
              const required = tokenRequired.includes(env)
              return (
                <div className="field" key={env}>
                  <label className="field-label">
                    <KeyRound size={13} /> <EnvBadge env={env} /> Bearer token
                    <span className={`token-tag ${required ? 'req' : 'opt'}`}>
                      {required ? 'required' : 'optional · admin'}
                    </span>
                  </label>
                  <div className="input-with-action">
                    <input
                      className="input mono"
                      type={shown[env] ? 'text' : 'password'}
                      value={tokens[env] || ''}
                      onChange={(e) => setTokens((p) => ({ ...p, [env]: e.target.value }))}
                      placeholder={
                        required
                          ? `Paste the ${env} Bearer token`
                          : `Optional — leave blank to use the admin panel`
                      }
                      spellCheck={false}
                      autoComplete="off"
                    />
                    <button
                      type="button"
                      className="icon-btn input-action"
                      onClick={() => setShown((p) => ({ ...p, [env]: !p[env] }))}
                      aria-label={shown[env] ? 'Hide token' : 'Show token'}
                    >
                      {shown[env] ? <EyeOff size={14} /> : <Eye size={14} />}
                    </button>
                  </div>
                </div>
              )
            })}

            <div className="security-note">
              <ShieldCheck size={14} />
              Tokens are used server-side for this run only and are never stored.
            </div>

            <div className="form-actions">
              <button type="button" className="btn btn-ghost" onClick={onClose}>
                Cancel
              </button>
              <button className="btn btn-primary" type="submit" disabled={!ready}>
                {anyToken || tokenRequired.length > 0 ? 'Start extraction' : 'Extract via admin panel'}
              </button>
            </div>
          </>
        )}
      </form>
    </Modal>
  )
}

export default TokenModal
