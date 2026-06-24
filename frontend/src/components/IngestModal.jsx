import { useEffect, useMemo, useState } from 'react'
import { Database, ListChecks, CheckCircle2, RefreshCw } from 'lucide-react'
import Modal from './Modal'
import { EnvBadge, Skeleton } from './ui'
import { getCourse } from '../api'
import { useToast } from './Toast'

// Pull every reading-material part out of a course detail, one row per
// learning set, keeping its topic/session context and extraction status.
function collectRows(detail) {
  const rows = []
  for (const topic of detail.topics) {
    for (const unit of topic.units) {
      for (const part of unit.parts) {
        if (part.label !== 'Reading Material') continue
        rows.push({
          id: part.unit_id,
          session: unit.label || part.name || part.unit_id,
          topic: topic.topic_name || topic.topic_id,
          status: part.content_status,
          chars: part.content_chars,
          extracted: part.content_status === 'EXTRACTED',
          ingested: part.is_ingested,
          // Content changed since it was last ingested → still offer for re-ingestion.
          stale: Boolean(part.ingest_stale),
          chunks: part.chunk_count,
        })
      }
    }
  }
  return rows
}

// Hide resources that are already ingested AND unchanged since — only show what's
// not yet ingested or was modified after its last ingestion.
const isVisible = (r) => !(r.ingested && !r.stale)
// Selectable = visible AND has extracted content.
const isSelectable = (r) => isVisible(r) && r.extracted

function StatusBadge({ row }) {
  if (row.extracted) {
    return <span className="content-badge ok">{Number(row.chars).toLocaleString()} chars</span>
  }
  if (row.status === 'EMPTY') return <span className="content-badge empty">empty</span>
  if (row.status === 'ERROR') return <span className="content-badge err">extract failed</span>
  return <span className="content-badge none">not extracted</span>
}

// Popup shown before ingestion: lists every learning resource of the course
// and its prerequisites so the user can deselect what shouldn't be ingested.
// Only extracted resources are selectable; the rest explain why not.
function IngestModal({ course, onClose, onSubmit }) {
  const toast = useToast()
  const [groups, setGroups] = useState(null) // null = loading
  const [selected, setSelected] = useState(new Set())

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const main = await getCourse(course.course_id)
        const prereqs = await Promise.all(
          (main.prerequisites || []).map((p) => getCourse(p.course_id).catch(() => null)),
        )
        if (cancelled) return
        const built = [main, ...prereqs.filter(Boolean)].map((d) => ({
          courseId: d.course_id,
          name: d.course_name || d.course_id,
          environment: d.environment,
          rows: collectRows(d),
        }))
        setGroups(built)
        // Everything that can be ingested (not-yet-ingested or modified) starts selected.
        setSelected(
          new Set(built.flatMap((g) => g.rows.filter(isSelectable).map((r) => r.id))),
        )
      } catch (e) {
        toast.push({
          kind: 'error',
          title: 'Could not load learning resources',
          message: e.message,
        })
        onClose()
      }
    }
    load()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [course.course_id])

  const selectableIds = useMemo(
    () => (groups || []).flatMap((g) => g.rows.filter(isSelectable).map((r) => r.id)),
    [groups],
  )
  const hiddenCount = useMemo(
    () => (groups || []).reduce((n, g) => n + g.rows.filter((r) => !isVisible(r)).length, 0),
    [groups],
  )
  const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selected.has(id))

  function toggleRow(id) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function toggleGroup(group) {
    const ids = group.rows.filter(isSelectable).map((r) => r.id)
    const everySelected = ids.length > 0 && ids.every((id) => selected.has(id))
    setSelected((prev) => {
      const next = new Set(prev)
      ids.forEach((id) => (everySelected ? next.delete(id) : next.add(id)))
      return next
    })
  }

  function toggleAll() {
    setSelected(allSelected ? new Set() : new Set(selectableIds))
  }

  function handleSubmit() {
    onSubmit(course, [...selected])
    onClose()
  }

  const totalRows = (groups || []).reduce((n, g) => n + g.rows.length, 0)
  const totalVisible = (groups || []).reduce((n, g) => n + g.rows.filter(isVisible).length, 0)

  return (
    <Modal
      size="lg"
      title="Ingest content"
      subtitle={
        <>
          Choose which learning resources of{' '}
          <code>{course.course_name || course.course_id}</code> and its prerequisites to ingest
          into the RAG index.
        </>
      }
      onClose={onClose}
      footer={
        groups && (
          <div className="ingest-footer">
            <span className="muted">
              {selected.size} of {selectableIds.length} resources selected
            </span>
            <div className="ingest-footer-actions">
              <button type="button" className="btn btn-ghost" onClick={onClose}>
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={selected.size === 0}
                onClick={handleSubmit}
              >
                <Database size={14} /> Ingest {selected.size} resource
                {selected.size === 1 ? '' : 's'}
              </button>
            </div>
          </div>
        )
      }
    >
      {groups === null ? (
        <div className="form-stack">
          <Skeleton height={42} />
          <Skeleton height={42} width="92%" />
          <Skeleton height={42} width="85%" />
        </div>
      ) : totalRows === 0 ? (
        <p className="muted">
          No learning resources with reading material were found. Sync the course (and extract
          content) first.
        </p>
      ) : totalVisible === 0 ? (
        <p className="muted">
          <CheckCircle2 size={13} /> Everything is already ingested and up to date
          {hiddenCount > 0 ? ` (${hiddenCount} resource${hiddenCount === 1 ? '' : 's'} hidden)` : ''}.
          Re-extract a learning set to make it available for re-ingestion.
        </p>
      ) : (
        <>
          <div className="ingest-toolbar">
            <span className="field-hint">
              Already-ingested resources are hidden unless modified
              {hiddenCount > 0 ? ` — ${hiddenCount} up-to-date one${hiddenCount === 1 ? '' : 's'} hidden` : ''}.
            </span>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={toggleAll}
              disabled={selectableIds.length === 0}
            >
              <ListChecks size={13} /> {allSelected ? 'Deselect all' : 'Select all'}
            </button>
          </div>

          {groups.map((g) => {
            const visibleRows = g.rows.filter(isVisible)
            const ids = g.rows.filter(isSelectable).map((r) => r.id)
            const groupChecked = ids.length > 0 && ids.every((id) => selected.has(id))
            const groupCount = g.rows.filter((r) => selected.has(r.id)).length
            const groupHidden = g.rows.length - visibleRows.length
            // Skip a course entirely when it has nothing left to show.
            if (visibleRows.length === 0) return null
            return (
              <div className="ingest-group" key={g.courseId}>
                <label className="ingest-group-head">
                  <input
                    type="checkbox"
                    checked={groupChecked}
                    disabled={ids.length === 0}
                    onChange={() => toggleGroup(g)}
                  />
                  <span className="ingest-group-name">{g.name}</span>
                  <EnvBadge env={g.environment} />
                  <span
                    className="ingest-group-count"
                    title="Selected out of ingestable reading materials (empty / failed ones are excluded)"
                  >
                    {groupCount}/{ids.length}
                  </span>
                  {groupHidden > 0 && (
                    <span className="ingest-hidden-note" title="Already ingested and unchanged">
                      {groupHidden} up-to-date hidden
                    </span>
                  )}
                </label>
                {visibleRows.map((r) => (
                  <label
                    key={r.id}
                    className={`ingest-row ${r.extracted ? '' : 'disabled'} ${selected.has(r.id) ? 'selected' : ''}`}
                  >
                    <input
                      type="checkbox"
                      disabled={!r.extracted}
                      checked={selected.has(r.id)}
                      onChange={() => toggleRow(r.id)}
                    />
                    <span className="ingest-row-main">
                      <span className="ingest-row-title">{r.session}</span>
                      <span className="ingest-row-topic">{r.topic}</span>
                    </span>
                    {r.stale && (
                      <span
                        className="content-badge stale"
                        title="Content changed since last ingested — re-ingest to refresh"
                      >
                        <RefreshCw size={10} /> modified
                      </span>
                    )}
                    {r.ingested && (
                      <span
                        className="content-badge ingested"
                        title="Already indexed in the RAG store"
                      >
                        <CheckCircle2 size={10} /> {Number(r.chunks).toLocaleString()} chunk
                        {r.chunks === 1 ? '' : 's'}
                      </span>
                    )}
                    <StatusBadge row={r} />
                  </label>
                ))}
              </div>
            )
          })}
        </>
      )}
    </Modal>
  )
}

export default IngestModal
