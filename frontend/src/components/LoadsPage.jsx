import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Upload, FileDown, ExternalLink, ArrowLeft, PackageOpen } from 'lucide-react'
import { listLoads, getLoad } from '../api'
import { useToast } from './Toast'
import { Spinner, EmptyState } from './ui'
import McqResults from './McqResults'

function when(iso) {
  return iso ? new Date(iso).toLocaleString() : '—'
}

// Portal loads + ZIP exports, with click-through to the exact content that was loaded
// (a snapshot stored on the load row, so it stays accurate even if the run changes later).
export default function LoadsPage({ openJobId = null, courses = [] }) {
  const toast = useToast()
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null) // full load detail (with content)
  const [detailLoading, setDetailLoading] = useState(false)

  const courseName = useCallback(
    (id) => courses.find((c) => c.course_id === id)?.course_name || id || '—',
    [courses],
  )

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await listLoads())
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not load history', message: e.message })
    } finally {
      setLoading(false)
    }
  }, [toast])

  useEffect(() => { load() }, [load])

  const openDetail = useCallback(async (id) => {
    setDetailLoading(true)
    try {
      setSelected(await getLoad(id))
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not open load', message: e.message })
    } finally {
      setDetailLoading(false)
    }
  }, [toast])

  // Deep-link from the Activity drawer: open the load produced by a given job.
  useEffect(() => {
    if (!openJobId || rows.length === 0) return
    const match = rows.find((r) => r.job_id === openJobId)
    if (match) openDetail(match.id)
  }, [openJobId, rows, openDetail])

  if (selected) {
    const isLoad = selected.action === 'load'
    return (
      <>
        <header className="topbar">
          <div>
            <button className="btn btn-ghost btn-sm" onClick={() => setSelected(null)}>
              <ArrowLeft size={14} /> Back to loads
            </button>
            <h1 style={{ marginTop: 'var(--sp-2)' }}>
              {isLoad ? 'Loaded content' : 'Exported content'}
            </h1>
            <p className="topbar-sub">
              {courseName(selected.course_id)} · {selected.count} question(s) · {when(selected.created_at)}
              {selected.resource_id ? ` · resource ${selected.resource_id}` : ''}
            </p>
          </div>
          <div className="topbar-actions">
            {selected.sheet_url && (
              <a className="btn btn-soft btn-sm" href={selected.sheet_url} target="_blank" rel="noreferrer">
                <ExternalLink size={14} /> Exam sheet
              </a>
            )}
            {selected.s3_url && (
              <a className="btn btn-soft btn-sm" href={selected.s3_url} target="_blank" rel="noreferrer">
                <FileDown size={14} /> Download ZIP
              </a>
            )}
          </div>
        </header>

        {detailLoading ? (
          <div className="admin-loading"><Spinner size={20} /></div>
        ) : selected.has_content ? (
          <McqResults
            key={selected.id}
            run={{ id: selected.run_id, result: selected.content, review_status: 'approved' }}
            mode="view"
            canLoad={false}
          />
        ) : (
          <EmptyState icon={PackageOpen} title="Snapshot unavailable"
            hint="This load predates content snapshots, so its exact questions weren't recorded." />
        )}
      </>
    )
  }

  return (
    <>
      <header className="topbar">
        <div>
          <h1>Loads</h1>
          <p className="topbar-sub">
            Every portal load and ZIP export — open one to see exactly what was loaded.
          </p>
        </div>
        <div className="topbar-actions">
          <button className="btn btn-soft btn-sm" onClick={load} disabled={loading}>
            <RefreshCw size={14} /> Refresh
          </button>
        </div>
      </header>

      {loading && rows.length === 0 ? (
        <div className="admin-loading"><Spinner size={20} /></div>
      ) : rows.length === 0 ? (
        <EmptyState icon={Upload} title="No loads yet"
          hint="Prepare & Load or Export a reviewed run — it shows up here and in Activity." />
      ) : (
        <div className="admin-section">
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr><th>Action</th><th>Course</th><th>By</th><th>Status</th><th>Questions</th><th>When</th><th></th></tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="loads-row" onClick={() => openDetail(r.id)}>
                    <td>
                      <span className="mcq-status-chip">
                        {r.action === 'load' ? <Upload size={12} /> : <FileDown size={12} />} {r.action}
                      </span>
                    </td>
                    <td>{courseName(r.course_id)}</td>
                    <td>{r.user_name || '—'}</td>
                    <td>
                      <span className={`mcq-status-chip ${r.status === 'SUCCESS' ? 'ok' : r.status === 'FAILURE' ? 'err' : 'warn'}`}>
                        {r.status || '—'}
                      </span>
                    </td>
                    <td className="admin-num">{r.count}</td>
                    <td className="admin-log-time">{when(r.created_at)}</td>
                    <td>
                      <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); openDetail(r.id) }}>
                        View
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  )
}
