import { useState } from 'react'
import { ArrowLeft, ArrowRight, GitBranch, Link2, Star } from 'lucide-react'
import Modal from './Modal'
import { EnvBadge, Segmented, Spinner } from './ui'
import { fetchVersions } from '../api'
import { useToast } from './Toast'

const ENVIRONMENTS = ['PROD', 'BETA']

// Guided two-step flow for adding a course (or a prerequisite course):
//   1. Pick environment + paste the course ID
//   2. Choose which course version to fetch
// On confirm, calls onStartSync(payload) and closes.
function AddCourseWizard({ prerequisiteFor, defaultEnv = 'PROD', onClose, onStartSync }) {
  const toast = useToast()
  const [step, setStep] = useState(1)
  const [environment, setEnvironment] = useState(defaultEnv)
  const [courseId, setCourseId] = useState('')
  const [loading, setLoading] = useState(false)
  const [versions, setVersions] = useState([])

  const isPrereq = Boolean(prerequisiteFor)

  async function handleFetchVersions(e) {
    e.preventDefault()
    const id = courseId.trim()
    if (!id || loading) return
    setLoading(true)
    try {
      const data = await fetchVersions(id, environment)
      setVersions(data.versions)
      setStep(2)
    } catch (err) {
      toast.push({ kind: 'error', title: 'Could not fetch versions', message: err.message })
    } finally {
      setLoading(false)
    }
  }

  function startSync(extra = {}) {
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
          <span className="wizard-step-num">1</span> Course details
        </div>
        <div className="wizard-step-line" />
        <div className={`wizard-step ${step === 2 ? 'current' : ''}`}>
          <span className="wizard-step-num">2</span> Select version
        </div>
      </div>

      {step === 1 && (
        <form onSubmit={handleFetchVersions} className="form-stack">
          <div className="field">
            <label className="field-label">Environment</label>
            <Segmented options={ENVIRONMENTS} value={environment} onChange={setEnvironment} />
            <p className="field-hint">
              Which portal to fetch from. {environment === 'PROD' ? 'Production' : 'Beta'} portal
              credentials will be used.
            </p>
          </div>

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
          </div>

          <div className="form-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Cancel
            </button>
            <button className="btn btn-primary" type="submit" disabled={!courseId.trim() || loading}>
              {loading ? (
                <>
                  <Spinner /> Fetching versions…
                </>
              ) : (
                <>
                  Continue <ArrowRight size={14} />
                </>
              )}
            </button>
          </div>
        </form>
      )}

      {step === 2 && (
        <div className="form-stack">
          <p className="muted">
            <EnvBadge env={environment} /> <code>{courseId.trim()}</code>
          </p>

          {versions.length === 0 ? (
            <div className="empty-versions">
              <p>No published versions were found for this course.</p>
              <p className="field-hint">
                You can still fetch the hierarchy directly from its resource links.
              </p>
              <button className="btn btn-primary" onClick={() => startSync()}>
                <Link2 size={14} /> Fetch via resource links
              </button>
            </div>
          ) : (
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
                      startSync({
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
