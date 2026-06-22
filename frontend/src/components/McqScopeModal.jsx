import { useEffect, useState } from 'react'
import { Sparkles, ChevronRight, GitMerge } from 'lucide-react'
import Modal from './Modal'
import { EnvBadge, Skeleton } from './ui'
import { getCourse } from '../api'
import { useToast } from './Toast'

// A session's reading-material portal unit_id (what the RAG scope filters on).
function sessionUnitId(unit) {
  const rm = (unit.parts || []).find((p) => p.label === 'Reading Material' && p.unit_id)
  return rm?.unit_id || ''
}

function collectUnits(detail) {
  const out = []
  for (const topic of detail.topics || []) {
    for (const unit of topic.units || []) {
      const uid = sessionUnitId(unit)
      if (uid) out.push({ unitId: uid, label: unit.label || uid, topic: topic.topic_name || '' })
    }
  }
  return out
}

// The current course's sessions that come BEFORE `currentUnitId` (document order:
// topic order, then unit order) — selectable as prerequisites for this session.
function earlierSessions(detail, currentUnitId) {
  const all = collectUnits(detail || {})
  const idx = all.findIndex((u) => u.unitId === currentUnitId)
  return idx > 0 ? all.slice(0, idx) : []
}

// Pre-generation scope picker: choose which PREREQUISITE units ground the MCQs.
// Each prerequisite course is a collapsible section listing its units.
function McqScopeModal({ course, prerequisites, currentUnitId, onClose, onConfirm }) {
  const toast = useToast()
  const [groups, setGroups] = useState(null) // null = loading
  const [selected, setSelected] = useState(new Set())
  const [open, setOpen] = useState(new Set()) // expanded course ids

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const details = await Promise.all(
          (prerequisites || []).map((p) => getCourse(p.course_id).catch(() => null)),
        )
        if (cancelled) return
        const prereqGroups = details.filter(Boolean).map((d) => ({
          courseId: d.course_id,
          name: d.course_name || d.course_id,
          environment: d.environment,
          units: collectUnits(d),
        }))
        // The current course's earlier sessions — selectable as prerequisites, but
        // OPT-IN (not selected by default): the current course is already in scope,
        // so this only marks specific earlier sessions as explicit prerequisites.
        const earlier = earlierSessions(course, currentUnitId)
        const currentGroup = earlier.length > 0 ? {
          courseId: course.course_id,
          name: `${course.course_name || course.course_id} · earlier sessions`,
          environment: course.environment,
          units: earlier,
          isCurrentCourse: true,
        } : null

        const built = currentGroup ? [currentGroup, ...prereqGroups] : prereqGroups
        setGroups(built)
        // Default: every PREREQUISITE-COURSE unit selected (current behaviour);
        // earlier same-course sessions start unselected.
        setSelected(new Set(prereqGroups.flatMap((g) => g.units.map((u) => u.unitId))))
        if (currentGroup) setOpen(new Set([currentGroup.courseId]))
      } catch (e) {
        toast.push({ kind: 'error', title: 'Could not load prerequisites', message: e.message })
        onClose()
      }
    }
    load()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [course.course_id])

  function toggleUnit(id) {
    setSelected((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }
  function toggleCourse(g) {
    const ids = g.units.map((u) => u.unitId)
    const allSel = ids.length > 0 && ids.every((id) => selected.has(id))
    setSelected((prev) => {
      const next = new Set(prev)
      ids.forEach((id) => (allSel ? next.delete(id) : next.add(id)))
      return next
    })
  }
  function toggleOpen(id) {
    setOpen((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  const totalUnits = (groups || []).reduce((n, g) => n + g.units.length, 0)
  const noPrereqs = groups !== null && groups.length === 0

  return (
    <Modal
      size="lg"
      title="Choose prerequisite scope"
      subtitle={
        <>
          Pick which prerequisite units should ground the MCQs for{' '}
          <code>{course.course_name || course.course_id}</code>. The selected session's own
          course is always included.
        </>
      }
      onClose={onClose}
      footer={
        groups && (
          <div className="ingest-footer">
            <span className="muted">
              {noPrereqs ? 'No prerequisites' : `${selected.size} of ${totalUnits} prerequisite units`}
            </span>
            <div className="ingest-footer-actions">
              <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
              <button type="button" className="btn btn-primary" onClick={() => onConfirm([...selected])}>
                <Sparkles size={14} /> Generate MCQs
              </button>
            </div>
          </div>
        )
      }
    >
      {groups === null ? (
        <div className="form-stack">
          <Skeleton height={40} />
          <Skeleton height={40} width="90%" />
        </div>
      ) : noPrereqs ? (
        <p className="muted">
          This course has no prerequisites — the MCQs will be grounded on this session's course only.
        </p>
      ) : (
        <>
          <p className="field-hint">
            <GitMerge size={13} /> Prerequisite-course content is included by default — deselect what
            you don't want. Earlier sessions of this course are optional: select any that are
            prerequisites for this session.
          </p>
          {groups.map((g) => {
            const ids = g.units.map((u) => u.unitId)
            const sel = ids.filter((id) => selected.has(id)).length
            const allSel = ids.length > 0 && sel === ids.length
            const isOpen = open.has(g.courseId)
            return (
              <div className="ingest-group" key={g.courseId}>
                <div className="mcq-scope-head">
                  <button
                    type="button"
                    className={`expander expander-sm ${isOpen ? 'open' : ''}`}
                    onClick={() => toggleOpen(g.courseId)}
                    aria-label={isOpen ? 'Collapse' : 'Expand'}
                  >
                    <ChevronRight size={14} />
                  </button>
                  <input
                    type="checkbox"
                    checked={allSel}
                    ref={(el) => el && (el.indeterminate = sel > 0 && !allSel)}
                    disabled={ids.length === 0}
                    onChange={() => toggleCourse(g)}
                  />
                  <span className="ingest-group-name">{g.name}</span>
                  <EnvBadge env={g.environment} />
                  <span className="ingest-group-count">{sel}/{ids.length}</span>
                </div>
                {isOpen && (
                  <div className={`collapse open`}>
                    <div className="collapse-inner mcq-scope-units">
                      {g.units.length === 0 ? (
                        <p className="ingest-empty muted">No units with reading material.</p>
                      ) : (
                        g.units.map((u) => (
                          <label key={u.unitId} className="mcq-scope-unit">
                            <input
                              type="checkbox"
                              checked={selected.has(u.unitId)}
                              onChange={() => toggleUnit(u.unitId)}
                            />
                            <span className="mcq-scope-unit-label">{u.label}</span>
                            {u.topic && <span className="mcq-scope-unit-topic">{u.topic}</span>}
                          </label>
                        ))
                      )}
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </>
      )}
    </Modal>
  )
}

export default McqScopeModal
