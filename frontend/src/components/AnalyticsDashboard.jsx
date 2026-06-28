import { useCallback, useEffect, useMemo, useState } from 'react'
import { RefreshCw, Download, BarChart3 } from 'lucide-react'
import { adminAnalytics, adminStats } from '../api'
import { useAuth } from '../auth/AuthContext'
import { useToast } from './Toast'
import { Spinner, EmptyState, Segmented } from './ui'

const ELEVATED = ['admin', 'manager', 'lead']
const PRESETS = ['Daily', 'Weekly', 'Monthly', 'Custom']
const fmt = (n) => (n ?? 0).toLocaleString()

// yyyy-mm-dd in local time, for <input type="date"> defaults.
function isoDate(d) {
  const z = new Date(d.getTime() - d.getTimezoneOffset() * 60000)
  return z.toISOString().slice(0, 10)
}

// Resolve a preset (or explicit custom dates) into an absolute [from, to] window + a
// sensible bucket granularity.
function resolveRange(preset, customFrom, customTo) {
  const now = new Date()
  let from
  let to = now
  if (preset === 'Daily') {
    from = new Date(now); from.setHours(0, 0, 0, 0)
  } else if (preset === 'Weekly') {
    from = new Date(now.getTime() - 7 * 86400000)
  } else if (preset === 'Monthly') {
    from = new Date(now.getTime() - 30 * 86400000)
  } else {
    from = customFrom ? new Date(`${customFrom}T00:00:00`) : new Date(now.getTime() - 7 * 86400000)
    to = customTo ? new Date(`${customTo}T23:59:59`) : now
  }
  const spanDays = Math.max(1, (to - from) / 86400000)
  const bucket = spanDays > 180 ? 'month' : spanDays > 60 ? 'week' : 'day'
  return { from: from.toISOString(), to: to.toISOString(), bucket }
}

// Grouped bar chart (Generated / Reviewed / Approved per time bucket), inline SVG so it
// works inside the app and inside the self-contained downloaded report.
function ThroughputChart({ series }) {
  if (!series || series.length === 0) {
    return <div className="an-chart-empty">No activity in this range.</div>
  }
  const W = 920
  const H = 240
  const padL = 40
  const padB = 28
  const padT = 12
  const max = Math.max(1, ...series.flatMap((d) => [d.generated, d.reviewed, d.approved]))
  const innerW = W - padL - 12
  const innerH = H - padB - padT
  const groupW = innerW / series.length
  const barW = Math.max(2, Math.min(16, (groupW - 6) / 3))
  const colors = { generated: 'var(--brand)', reviewed: 'var(--cyan)', approved: 'var(--green)' }
  const yOf = (v) => padT + innerH - (v / max) * innerH
  const ticks = [0, 0.5, 1].map((t) => Math.round(max * t))

  return (
    <div className="an-chart">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" role="img"
        aria-label="Throughput over time">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={padL} x2={W - 12} y1={yOf(t)} y2={yOf(t)} className="an-grid" />
            <text x={padL - 6} y={yOf(t) + 3} textAnchor="end" className="an-axis">{t}</text>
          </g>
        ))}
        {series.map((d, i) => {
          const gx = padL + i * groupW + (groupW - barW * 3 - 4) / 2
          const keys = ['generated', 'reviewed', 'approved']
          const showLabel = series.length <= 16 || i % Math.ceil(series.length / 16) === 0
          return (
            <g key={d.bucket}>
              {keys.map((k, j) => (
                <rect key={k} x={gx + j * (barW + 2)} y={yOf(d[k])} width={barW}
                  height={Math.max(0, padT + innerH - yOf(d[k]))} rx="2" fill={colors[k]}>
                  <title>{`${d.bucket} — ${k}: ${d[k]}`}</title>
                </rect>
              ))}
              {showLabel && (
                <text x={gx + (barW * 3) / 2} y={H - 10} textAnchor="middle" className="an-axis">
                  {d.bucket.slice(5)}
                </text>
              )}
            </g>
          )
        })}
      </svg>
      <div className="an-legend">
        <span><i style={{ background: 'var(--brand)' }} /> Generated</span>
        <span><i style={{ background: 'var(--cyan)' }} /> Reviewed</span>
        <span><i style={{ background: 'var(--green)' }} /> Approved</span>
      </div>
    </div>
  )
}

export default function AnalyticsDashboard({ courses = [] }) {
  const { user } = useAuth()
  const toast = useToast()
  const [preset, setPreset] = useState('Weekly')
  const [customFrom, setCustomFrom] = useState(isoDate(new Date(Date.now() - 7 * 86400000)))
  const [customTo, setCustomTo] = useState(isoDate(new Date()))
  const [courseId, setCourseId] = useState('')
  const [userId, setUserId] = useState('')
  const [users, setUsers] = useState([])
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)

  const range = useMemo(
    () => resolveRange(preset, customFrom, customTo),
    [preset, customFrom, customTo],
  )

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await adminAnalytics({
        from: range.from, to: range.to, bucket: range.bucket, courseId, userId,
      })
      setData(res)
    } catch (e) {
      toast.push({ kind: 'error', title: 'Could not load analytics', message: e.message })
    } finally {
      setLoading(false)
    }
  }, [range, courseId, userId, toast])

  useEffect(() => { load() }, [load])

  // Stable user list for the filter (elevated roles can read /admin/stats).
  useEffect(() => {
    let alive = true
    adminStats()
      .then((s) => { if (alive) setUsers(s?.users || []) })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  if (!ELEVATED.includes(user?.role)) {
    return (
      <main className="main">
        <EmptyState icon={BarChart3} title="Restricted"
          hint="Analytics are available to lead, manager and admin roles." />
      </main>
    )
  }

  const k = data?.kpis || {}
  const p = data?.percentages || {}

  function downloadReport() {
    if (!data) return
    const html = buildReportHtml(data, { courses, users })
    const blob = new Blob([html], { type: 'text/html' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `throughput_${range.from.slice(0, 10)}_${range.to.slice(0, 10)}.html`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  return (
    <>
      <header className="topbar">
        <div>
          <h1>Throughput Analytics</h1>
          <p className="topbar-sub">
            Questions generated, reviewed, approved and sent back for regeneration — across
            courses and users, for any time range.
          </p>
        </div>
        <div className="topbar-actions">
          <button className="btn btn-soft btn-sm" onClick={load} disabled={loading}>
            <RefreshCw size={14} /> Refresh
          </button>
          <button className="btn btn-primary btn-sm" onClick={downloadReport} disabled={!data}>
            <Download size={14} /> Download report
          </button>
        </div>
      </header>

      <div className="an-controls">
        <Segmented options={PRESETS} value={preset} onChange={setPreset} />
        {preset === 'Custom' && (
          <div className="an-dates">
            <input type="date" className="input" value={customFrom}
              max={customTo} onChange={(e) => setCustomFrom(e.target.value)} />
            <span className="an-dash">→</span>
            <input type="date" className="input" value={customTo}
              min={customFrom} onChange={(e) => setCustomTo(e.target.value)} />
          </div>
        )}
        <select className="input an-select" value={courseId} onChange={(e) => setCourseId(e.target.value)}>
          <option value="">All courses</option>
          {courses.map((c) => (
            <option key={c.course_id} value={c.course_id}>{c.course_name || c.course_id}</option>
          ))}
        </select>
        <select className="input an-select" value={userId} onChange={(e) => setUserId(e.target.value)}>
          <option value="">All users</option>
          {users.map((u) => (
            <option key={u.id} value={u.id}>{u.name || u.email}</option>
          ))}
        </select>
      </div>

      {loading && !data ? (
        <div className="admin-loading"><Spinner size={20} /></div>
      ) : (
        <>
          <div className="stats-row an-kpis">
            <div className="stat-card">
              <div className="stat-value">{fmt(k.generated)}</div>
              <div className="stat-label">Generated</div>
              <div className="an-sub">{fmt(k.sessions)} sessions · {fmt(k.generation_events)} runs</div>
            </div>
            <div className="stat-card">
              <div className="stat-value">{fmt(k.reviewed)}</div>
              <div className="stat-label">Reviewed</div>
              <div className="an-sub">{p.review_rate ?? 0}% of generated</div>
            </div>
            <div className="stat-card">
              <div className="stat-value an-ok">{fmt(k.approved)}</div>
              <div className="stat-label">Approved</div>
              <div className="an-sub">{p.approval_rate ?? 0}% of reviewed · {p.approved_of_generated ?? 0}% of generated</div>
            </div>
            <div className="stat-card">
              <div className="stat-value an-warn">{fmt(k.regen_requests)}</div>
              <div className="stat-label">Regen requests</div>
              <div className="an-sub">{p.regen_rate ?? 0}% rate · {fmt(k.session_regens)} session regens</div>
            </div>
          </div>

          <div className="admin-section">
            <h2 className="admin-h2">Throughput over time</h2>
            <ThroughputChart series={data?.timeseries} />
          </div>

          <div className="admin-section">
            <h2 className="admin-h2">By course <span className="admin-count">{data?.by_course?.length || 0}</span></h2>
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr><th>Course</th><th>Generated</th><th>Reviewed</th><th>Approved</th><th>Regen</th><th>Approval %</th></tr>
                </thead>
                <tbody>
                  {(data?.by_course || []).map((r) => (
                    <tr key={r.course_id}>
                      <td>{r.course_name || r.course_id}</td>
                      <td className="admin-num">{fmt(r.generated)}</td>
                      <td className="admin-num">{fmt(r.reviewed)}</td>
                      <td className="admin-num">{fmt(r.approved)}</td>
                      <td className="admin-num">{fmt(r.regen_requests)}</td>
                      <td className="admin-num">{r.reviewed ? Math.round((r.approved / r.reviewed) * 100) : 0}%</td>
                    </tr>
                  ))}
                  {(data?.by_course || []).length === 0 && <tr><td colSpan={6} className="admin-empty">No activity.</td></tr>}
                </tbody>
              </table>
            </div>
          </div>

          <div className="admin-section">
            <h2 className="admin-h2">By user <span className="admin-count">{data?.by_user?.length || 0}</span></h2>
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr><th>User</th><th>Generated</th><th>Reviewed</th><th>Approved</th><th>Regen</th><th>Approval %</th></tr>
                </thead>
                <tbody>
                  {(data?.by_user || []).map((r) => (
                    <tr key={r.user_id || 'none'}>
                      <td>
                        <div className="admin-user-name">{r.name}</div>
                        {r.email && <div className="admin-user-email">{r.email}</div>}
                      </td>
                      <td className="admin-num">{fmt(r.generated)}</td>
                      <td className="admin-num">{fmt(r.reviewed)}</td>
                      <td className="admin-num">{fmt(r.approved)}</td>
                      <td className="admin-num">{fmt(r.regen_requests)}</td>
                      <td className="admin-num">{r.reviewed ? Math.round((r.approved / r.reviewed) * 100) : 0}%</td>
                    </tr>
                  ))}
                  {(data?.by_user || []).length === 0 && <tr><td colSpan={6} className="admin-empty">No activity.</td></tr>}
                </tbody>
              </table>
            </div>
          </div>

          <div className="admin-section">
            <h2 className="admin-h2">Session regenerations <span className="admin-count">{data?.regen_by_session?.length || 0}</span></h2>
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr><th>When</th><th>Course</th><th>Session</th><th>Version</th><th>By</th><th>Reason</th></tr>
                </thead>
                <tbody>
                  {(data?.regen_by_session || []).map((r) => (
                    <tr key={r.run_id}>
                      <td className="admin-log-time">{new Date(r.created_at).toLocaleString()}</td>
                      <td>{r.course_name}</td>
                      <td className="admin-log-msg" title={r.unit_id}>{r.unit_id}</td>
                      <td className="admin-num">v{r.version}</td>
                      <td>{r.created_by_name}</td>
                      <td className="admin-log-msg" title={r.reason}>{r.reason || '—'}</td>
                    </tr>
                  ))}
                  {(data?.regen_by_session || []).length === 0 && <tr><td colSpan={6} className="admin-empty">No session regenerations.</td></tr>}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </>
  )
}

// --- Self-contained HTML report -------------------------------------------- #
function buildReportHtml(data, { courses, users }) {
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
  const k = data.kpis || {}
  const p = data.percentages || {}
  const r = data.range || {}
  const f = data.filters || {}
  const courseName = f.course_id
    ? (courses.find((c) => c.course_id === f.course_id)?.course_name || f.course_id)
    : 'All courses'
  const userName = f.user_id
    ? (users.find((u) => u.id === f.user_id)?.name || users.find((u) => u.id === f.user_id)?.email || f.user_id)
    : 'All users'
  const genAt = new Date().toLocaleString()
  const fmtN = (n) => (n ?? 0).toLocaleString()

  const kpi = (val, label, sub) =>
    `<div class="card"><div class="v">${fmtN(val)}</div><div class="l">${esc(label)}</div><div class="s">${esc(sub)}</div></div>`

  const rows = (arr, cells) =>
    arr.length
      ? arr.map((row) => `<tr>${cells(row)}</tr>`).join('')
      : `<tr><td class="empty" colspan="6">No data.</td></tr>`

  const courseRows = rows(data.by_course || [], (r) =>
    `<td>${esc(r.course_name || r.course_id)}</td><td class="n">${fmtN(r.generated)}</td><td class="n">${fmtN(r.reviewed)}</td><td class="n">${fmtN(r.approved)}</td><td class="n">${fmtN(r.regen_requests)}</td><td class="n">${r.reviewed ? Math.round((r.approved / r.reviewed) * 100) : 0}%</td>`)
  const userRows = rows(data.by_user || [], (r) =>
    `<td>${esc(r.name)}${r.email ? `<div class="sub">${esc(r.email)}</div>` : ''}</td><td class="n">${fmtN(r.generated)}</td><td class="n">${fmtN(r.reviewed)}</td><td class="n">${fmtN(r.approved)}</td><td class="n">${fmtN(r.regen_requests)}</td><td class="n">${r.reviewed ? Math.round((r.approved / r.reviewed) * 100) : 0}%</td>`)
  const regenRows = rows(data.regen_by_session || [], (r) =>
    `<td>${esc(new Date(r.created_at).toLocaleString())}</td><td>${esc(r.course_name)}</td><td>${esc(r.unit_id)}</td><td class="n">v${esc(r.version)}</td><td>${esc(r.created_by_name)}</td><td>${esc(r.reason || '—')}</td>`)

  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Throughput report · ${esc(r.from?.slice(0, 10))} → ${esc(r.to?.slice(0, 10))}</title>
<style>
  :root{--bg:#0f1014;--raised:#17181f;--inset:rgba(0,0,0,.28);--text:#eaebf0;--t2:#a2a6b4;--t3:#6b6f7e;--brand:#7b7bf5;--green:#4cc591;--amber:#e0a53c;--cyan:#54c4d6;--bd:rgba(255,255,255,.08)}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:32px}
  .wrap{max-width:1100px;margin:0 auto}
  h1{font-size:22px;margin:0 0 4px}
  .meta{color:var(--t2);font-size:13px;margin-bottom:24px}
  .meta b{color:var(--text)}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:28px}
  .card{background:var(--raised);border:1px solid var(--bd);border-radius:14px;padding:18px}
  .card .v{font-size:30px;font-weight:650}
  .card .l{color:var(--t2);font-size:13px;margin-top:2px}
  .card .s{color:var(--t3);font-size:12px;margin-top:8px}
  .ok{color:var(--green)}.warn{color:var(--amber)}
  h2{font-size:15px;margin:28px 0 10px}
  table{width:100%;border-collapse:collapse;background:var(--raised);border:1px solid var(--bd);border-radius:12px;overflow:hidden}
  th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--bd);font-size:13px}
  th{color:var(--t2);font-weight:600;background:var(--inset)}
  td.n{text-align:right;font-variant-numeric:tabular-nums}
  tr:last-child td{border-bottom:none}
  .sub{color:var(--t3);font-size:12px}
  .empty{color:var(--t3);text-align:center}
  .foot{color:var(--t3);font-size:12px;margin-top:28px}
</style></head><body><div class="wrap">
  <h1>Throughput report</h1>
  <div class="meta">
    <b>${esc(r.from?.slice(0, 16).replace('T', ' '))}</b> → <b>${esc(r.to?.slice(0, 16).replace('T', ' '))}</b>
    &nbsp;·&nbsp; Course: <b>${esc(courseName)}</b> &nbsp;·&nbsp; User: <b>${esc(userName)}</b>
    &nbsp;·&nbsp; Generated ${esc(genAt)}
  </div>
  <div class="cards">
    ${kpi(k.generated, 'Generated', `${fmtN(k.sessions)} sessions · ${fmtN(k.generation_events)} runs`)}
    ${kpi(k.reviewed, 'Reviewed', `${p.review_rate ?? 0}% of generated`)}
    <div class="card"><div class="v ok">${fmtN(k.approved)}</div><div class="l">Approved</div><div class="s">${p.approval_rate ?? 0}% of reviewed · ${p.approved_of_generated ?? 0}% of generated</div></div>
    <div class="card"><div class="v warn">${fmtN(k.regen_requests)}</div><div class="l">Regen requests</div><div class="s">${p.regen_rate ?? 0}% rate · ${fmtN(k.session_regens)} session regens</div></div>
  </div>
  <h2>By course</h2>
  <table><thead><tr><th>Course</th><th>Generated</th><th>Reviewed</th><th>Approved</th><th>Regen</th><th>Approval %</th></tr></thead><tbody>${courseRows}</tbody></table>
  <h2>By user</h2>
  <table><thead><tr><th>User</th><th>Generated</th><th>Reviewed</th><th>Approved</th><th>Regen</th><th>Approval %</th></tr></thead><tbody>${userRows}</tbody></table>
  <h2>Session regenerations</h2>
  <table><thead><tr><th>When</th><th>Course</th><th>Session</th><th>Version</th><th>By</th><th>Reason</th></tr></thead><tbody>${regenRows}</tbody></table>
  <div class="foot">Objective Content · Throughput report</div>
</div></body></html>`
}
