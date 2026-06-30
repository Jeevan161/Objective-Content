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

  function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  async function downloadReport() {
    if (!data) return
    const stem = `throughput_${range.from.slice(0, 10)}_${range.to.slice(0, 10)}`
    const inner = buildReportInner(data, { courses, users })
    try {
      const blob = await htmlToPng(inner)
      triggerDownload(blob, `${stem}.png`)
    } catch (e) {
      // Rasterization unsupported/failed → fall back to the self-contained HTML report.
      const doc = `<!doctype html><html lang="en"><head><meta charset="utf-8">`
        + `<meta name="viewport" content="width=device-width, initial-scale=1">`
        + `<title>Throughput report</title></head><body style="margin:0">${inner}</body></html>`
      triggerDownload(new Blob([doc], { type: 'text/html' }), `${stem}.html`)
      toast.push({ kind: 'info', title: 'Saved as HTML', message: 'Image export was unavailable; downloaded the HTML report instead.' })
    }
  }

  return (
    <>
      <header className="topbar">
        <div>
          <h1>MCQ Generator Throughput <span className="an-byline">— Jeevan</span></h1>
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
              <div className="stat-value an-warn">{fmt(k.regen_questions)}</div>
              <div className="stat-label">Questions regenerated</div>
              <div className="an-sub">{p.regen_question_rate ?? 0}% of generated · {fmt(k.regen_events)} events · {k.avg_regens_per_question ?? 0}×/question</div>
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
                  <tr><th>Course</th><th>Generated</th><th>Reviewed</th><th>Approved</th><th title="Distinct questions regenerated at least once">Regen Q</th><th>Approval %</th></tr>
                </thead>
                <tbody>
                  {(data?.by_course || []).map((r) => (
                    <tr key={r.course_id}>
                      <td>{r.course_name || r.course_id}</td>
                      <td className="admin-num">{fmt(r.generated)}</td>
                      <td className="admin-num">{fmt(r.reviewed)}</td>
                      <td className="admin-num">{fmt(r.approved)}</td>
                      <td className="admin-num" title={`${fmt(r.regen_events)} regen events`}>{fmt(r.regen_questions)}</td>
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
                  <tr><th>User</th><th>Generated</th><th>Reviewed</th><th>Approved</th><th title="Distinct questions regenerated at least once">Regen Q</th><th>Approval %</th></tr>
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
                      <td className="admin-num" title={`${fmt(r.regen_events)} regen events`}>{fmt(r.regen_questions)}</td>
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

// --- Downloadable report (rasterized to PNG) ------------------------------- #
// Self-contained report markup (scoped `.an-report`, literal colors, no CSS vars / external
// assets) so it renders identically inside an <svg><foreignObject> for rasterization AND as a
// standalone HTML fallback.
function buildReportInner(data, { courses, users }) {
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

  const kpi = (val, label, sub, cls = '') =>
    `<div class="card"><div class="v ${cls}">${fmtN(val)}</div><div class="l">${esc(label)}</div><div class="s">${esc(sub)}</div></div>`
  const rows = (arr, cells) =>
    arr.length ? arr.map((row) => `<tr>${cells(row)}</tr>`).join('')
      : `<tr><td class="empty" colspan="6">No data.</td></tr>`
  const pct = (n, d) => (d ? Math.round((n / d) * 100) : 0)

  const courseRows = rows(data.by_course || [], (x) =>
    `<td>${esc(x.course_name || x.course_id)}</td><td class="n">${fmtN(x.generated)}</td><td class="n">${fmtN(x.reviewed)}</td><td class="n">${fmtN(x.approved)}</td><td class="n">${fmtN(x.regen_questions)}</td><td class="n">${pct(x.approved, x.reviewed)}%</td>`)
  const userRows = rows(data.by_user || [], (x) =>
    `<td>${esc(x.name)}${x.email ? `<div class="sub">${esc(x.email)}</div>` : ''}</td><td class="n">${fmtN(x.generated)}</td><td class="n">${fmtN(x.reviewed)}</td><td class="n">${fmtN(x.approved)}</td><td class="n">${fmtN(x.regen_questions)}</td><td class="n">${pct(x.approved, x.reviewed)}%</td>`)
  const regenRows = rows(data.regen_by_session || [], (x) =>
    `<td>${esc(new Date(x.created_at).toLocaleString())}</td><td>${esc(x.course_name)}</td><td>${esc(x.unit_id)}</td><td class="n">v${esc(x.version)}</td><td>${esc(x.created_by_name)}</td><td>${esc(x.reason || '-')}</td>`)

  const css = `
.an-report{width:1100px;background:#0f1014;color:#eaebf0;padding:32px;margin:0;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5}
.an-report *{box-sizing:border-box}
.an-report h1{font-size:24px;font-weight:650;margin:0 0 4px}
.an-report h1 .byline{font-weight:400;font-size:.55em;color:#6b6f7e}
.an-report .meta{color:#a2a6b4;font-size:13px;margin-bottom:24px}
.an-report .meta b{color:#eaebf0}
.an-report .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:28px}
.an-report .card{background:#17181f;border:1px solid #262830;border-radius:14px;padding:18px}
.an-report .card .v{font-size:30px;font-weight:650}
.an-report .card .l{color:#a2a6b4;font-size:13px;margin-top:2px}
.an-report .card .s{color:#6b6f7e;font-size:12px;margin-top:8px}
.an-report .ok{color:#4cc591}.an-report .warn{color:#e0a53c}
.an-report h2{font-size:15px;margin:26px 0 10px}
.an-report table{width:100%;border-collapse:collapse;background:#17181f;border:1px solid #262830;border-radius:12px;overflow:hidden}
.an-report th,.an-report td{text-align:left;padding:10px 14px;border-bottom:1px solid #262830;font-size:13px;
  overflow-wrap:anywhere;word-break:break-word;vertical-align:top}
.an-report th{color:#a2a6b4;font-weight:600;background:#1e2027}
.an-report td.n{text-align:right;white-space:nowrap}
.an-report tr:last-child td{border-bottom:none}
.an-report .sub{color:#6b6f7e;font-size:12px}
.an-report .empty{color:#6b6f7e;text-align:center}
.an-report .foot{color:#6b6f7e;font-size:12px;margin-top:28px}`

  return `<style>${css}</style><div class="an-report">
  <h1>MCQ Generator Throughput <span class="byline">- Jeevan</span></h1>
  <div class="meta"><b>${esc((r.from || '').slice(0, 16).replace('T', ' '))}</b> &#8594; <b>${esc((r.to || '').slice(0, 16).replace('T', ' '))}</b>
    &#160;&#183;&#160; Course: <b>${esc(courseName)}</b> &#160;&#183;&#160; User: <b>${esc(userName)}</b> &#160;&#183;&#160; Generated ${esc(genAt)}</div>
  <div class="cards">
    ${kpi(k.generated, 'Generated', `${fmtN(k.sessions)} sessions, ${fmtN(k.generation_events)} runs`)}
    ${kpi(k.reviewed, 'Reviewed', `${p.review_rate ?? 0}% of generated`)}
    ${kpi(k.approved, 'Approved', `${p.approval_rate ?? 0}% of reviewed, ${p.approved_of_generated ?? 0}% of generated`, 'ok')}
    ${kpi(k.regen_questions, 'Questions regenerated', `${p.regen_question_rate ?? 0}% of generated, ${fmtN(k.regen_events)} events, ${k.avg_regens_per_question ?? 0}x/question`, 'warn')}
  </div>
  <h2>By course</h2>
  <table><thead><tr><th>Course</th><th>Generated</th><th>Reviewed</th><th>Approved</th><th>Regen Q</th><th>Approval %</th></tr></thead><tbody>${courseRows}</tbody></table>
  <h2>By user</h2>
  <table><thead><tr><th>User</th><th>Generated</th><th>Reviewed</th><th>Approved</th><th>Regen Q</th><th>Approval %</th></tr></thead><tbody>${userRows}</tbody></table>
  <h2>Session regenerations</h2>
  <table><thead><tr><th>When</th><th>Course</th><th>Session</th><th>Version</th><th>By</th><th>Reason</th></tr></thead><tbody>${regenRows}</tbody></table>
  <div class="foot">MCQ Generator Throughput report</div>
</div>`
}

// Rasterize self-contained report markup to a PNG Blob via <svg><foreignObject> → canvas.
// No external dependency; throws if the browser can't rasterize (caller falls back to HTML).
async function htmlToPng(inner) {
  // Render offscreen at the report's natural width, then measure the ACTUAL content box
  // (scrollWidth/Height, which include any overflow) so the canvas can't clip it.
  const probe = document.createElement('div')
  probe.style.cssText = 'position:fixed;left:-100000px;top:0;'
  probe.innerHTML = inner
  document.body.appendChild(probe)
  const node = probe.querySelector('.an-report') || probe
  const rect = node.getBoundingClientRect()
  // +pad guards against sub-pixel rounding / a last row clipping at the bottom edge.
  const width = Math.max(1100, Math.ceil(node.scrollWidth), Math.ceil(rect.width))
  const height = Math.max(Math.ceil(node.scrollHeight), Math.ceil(rect.height)) + 24
  document.body.removeChild(probe)

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">`
    + `<foreignObject x="0" y="0" width="${width}" height="${height}">`
    + `<div xmlns="http://www.w3.org/1999/xhtml" style="width:${width}px">${inner}</div>`
    + `</foreignObject></svg>`
  const img = new Image()
  await new Promise((resolve, reject) => {
    img.onload = resolve
    img.onerror = () => reject(new Error('rasterize failed'))
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg)
  })
  const scale = 2
  const canvas = document.createElement('canvas')
  canvas.width = width * scale
  canvas.height = height * scale
  const ctx = canvas.getContext('2d')
  ctx.scale(scale, scale)
  ctx.fillStyle = '#0f1014'
  ctx.fillRect(0, 0, width, height)
  ctx.drawImage(img, 0, 0, width, height)
  return await new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('toBlob failed'))), 'image/png'))
}
