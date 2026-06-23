import { useState } from 'react'
import { ArrowLeft, ArrowRight, GitBranch, Link2, Star, CheckCircle2, XCircle } from 'lucide-react'
import Modal from './Modal'
import { EnvBadge, Spinner } from './ui'
import { lookupCourse } from '../api'
import { useToast } from './Toast'

const ENVIRONMENTS = ['PROD', 'BETA']

// Guided flow for adding a course (or a prerequisite course):
//   1. Paste the course ID
//   2. We look it up in BOTH environments (PROD + BETA) at once and show what's available where —
//      PROD versions, BETA (usually unversioned), or "Not found" per environment. The same course
//      can exist in both with differing content, so each environment is offered independently.
function AddCourseWizard({ prerequisiteFor, onClose, onStartSync }) {
  const toast = useToast()
  const [step, setStep] = useState(1)
  const [courseId, setCourseId] = useState('')
  const [loading, setLoading] = useState(false)
  const [lookup, setLookup] = useState(null) // { PROD: {...}, BETA: {...} }

  const isPrereq = Boolean(prerequisiteFor)
  const anyPresent = lookup && ENVIRONMENTS.some((e) => lookup[e]?.present)

  async function handleLookup(e) {
    e.preventDefault()
    const id = courseId.trim()
    if (!id || loading) return
    setLoading(true)
    try {
      const data = await lookupCourse(id)
      setLookup(data.environments || {})
      setStep(2)
    } catch (err) {
      toast.push({ kind: 'error', title: 'Could not look up course', message: err.message })
    } finally {
      setLoading(false)
    }
  }

  function startSync(environment, extra = {}) {
    onStartSync({
      course_id: courseId.trim(),
      environment,
      prerequisite_for: prerequisiteFor || '',
      ...extra,
    })
    onClose()
  }

  return (
    <Modal
      title={isPrereq ? 'Add prerequisite course' : 'Add course'}
      subtitle={
        isPrereq ? (
          <>
            Will be linked as a prerequisite of <code>{prerequisiteFor}</code>
          </>
        ) : (
          'Fetch a course hierarchy (topics & units) from the portal'
        )
      }
      onClose={onClose}
    >
      {/* step indicator */}
      <div className="wizard-steps">
        <div className={`wizard-step ${step === 1 ? 'current' : 'done'}`}>
          <span className="wizard-step-num">1</span> Course ID
        </div>
        <div className="wizard-step-line" />
        <div className={`wizard-step ${step === 2 ? 'current' : ''}`}>
          <span className="wizard-step-num">2</span> Choose environment
        </div>
      </div>

      {step === 1 && (
        <form onSubmit={handleLookup} className="form-stack">
          <div className="field">
            <label className="field-label" htmlFor="course-id-input">
              Course ID
            </label>
            <input
              id="course-id-input"
              className="input mono"
              autoFocus
              value={courseId}
              onChange={(e) => setCourseId(e.target.value)}
              placeholder="e.g. c6008f8d-cd91-4843-bb3f-b75d4beca046"
              spellCheck={false}
            />
            <p className="field-hint">
              We&apos;ll look it up in both PROD and BETA and show what&apos;s available in each.
            </p>
          </div>

          <div className="form-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Cancel
            </button>
            <button className="btn btn-primary" type="submit" disabled={!courseId.trim() || loading}>
              {loading ? (
                <>
                  <Spinner /> Looking up…
                </>
              ) : (
                <>
                  Look up <ArrowRight size={14} />
                </>
              )}
            </button>
          </div>
        </form>
      )}

      {step === 2 && lookup && (
        <div className="form-stack">
          <p className="muted">
            <code>{courseId.trim()}</code>
          </p>

          {!anyPresent && (
            <div className="empty-versions">
              <p>
                This course was <strong>not found</strong> in PROD or BETA.
              </p>
              <p className="field-hint">
                Double-check the course ID, or confirm the portal credentials for each environment.
              </p>
            </div>
          )}

          {ENVIRONMENTS.map((env) => {
            const info = lookup[env] || { present: false, versions: [], error: null }
            const versions = info.versions || []
            return (
              <div key={env} className={`env-lookup-card ${info.present ? 'present' : 'absent'}`}>
                <div className="env-lookup-head">
                  <EnvBadge env={env} />
                  {info.present ? (
                    <span className="env-lookup-status ok">
                      <CheckCircle2 size={13} /> {info.course_name || 'Available'}
                    </span>
                  ) : (
                    <span className="env-lookup-status no">
                      <XCircle size={13} /> Not found in {env}
                      {info.error ? ` — ${info.error}` : ''}
                    </span>
                  )}
                </div>

                {info.present && versions.length > 0 && (
                  <ul className="version-list">
                    {versions.map((v) => (
                      <li key={v.row_id} className="version-item">
                        <div className="version-item-info">
                          <div className="version-item-title">
                            <GitBranch size={14} />
                            <strong>Version {v.version_id || '—'}</strong>
                            {v.is_latest_version && (
                              <span className="badge badge-latest">
                                <Star size={10} /> latest
                              </span>
                            )}
                          </div>
                          <code className="version-row-id">{v.row_id}</code>
                        </div>
                        <button
                          className="btn btn-primary btn-sm"
                          onClick={() =>
                            startSync(env, {
                              courseversion_id: v.row_id,
                              version_id: v.version_id,
                              is_latest_version: v.is_latest_version,
                            })
                          }
                        >
                          Use this version
                        </button>
                      </li>
                    ))}
                  </ul>
                )}

                {info.present && versions.length === 0 && (
                  <div className="env-lookup-noversion">
                    <p className="field-hint">
                      No versioning — fetch the hierarchy directly from its resource links.
                    </p>
                    <button className="btn btn-primary btn-sm" onClick={() => startSync(env)}>
                      <Link2 size={14} /> Fetch from {env}
                    </button>
                  </div>
                )}
              </div>
            )
          })}

          <div className="form-actions">
            <button type="button" className="btn btn-ghost" onClick={() => setStep(1)}>
              <ArrowLeft size={14} /> Back
            </button>
          </div>
        </div>
      )}
    </Modal>
  )
}

export default AddCourseWizard
