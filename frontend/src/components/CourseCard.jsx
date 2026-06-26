import { Fragment, useEffect, useRef, useState } from 'react'
import {
  ChevronRight,
  ChevronsUpDown,
  ChevronsDownUp,
  RefreshCw,
  GitMerge,
  FileText,
  Database,
  Hash,
  ExternalLink,
  Star,
  AlertTriangle,
  BookOpen,
  CheckCircle2,
} from 'lucide-react'
import { getCourse } from '../api'
import Pipeline from './Pipeline'
import ReadingMaterialPane from './ReadingMaterialPane'
import { coursePipeline } from '../lib/workflow'
import { EnvBadge, Skeleton, Spinner } from './ui'

// Header tag per container kind.
const KIND_META = {
  SESSION: { label: 'Session', cls: 'tag-session' },
  PRACTICE: { label: 'Practice', cls: 'tag-practice' },
}

// Fallback header tag for SINGLE containers, keyed by the part's portal type.
const TYPE_META = {
  LEARNING_SET: { label: 'Session', cls: 'tag-session' },
  QUESTION_SET: { label: 'Question Set', cls: 'tag-question' },
  QUIZ: { label: 'Quiz', cls: 'tag-quiz' },
  PRACTICE: { label: 'Practice', cls: 'tag-practice' },
}

// Colour class for a part's tag (Learning Resource, Reading Material, A/B/C, MCQ…).
function partTagClass(label) {
  const l = (label || '').toLowerCase()
  if (l === 'learning resource') return 'tag-learning'
  if (l === 'reading material') return 'tag-reading'
  if (l === 'mcq') return 'tag-mcq'
  if (l === 'coding') return 'tag-coding'
  if (l === 'quiz' || /^[a-z]$/.test(l)) return 'tag-quiz-part'
  return 'tag-default'
}

function headerTag(unit) {
  if (KIND_META[unit.kind]) return KIND_META[unit.kind]
  const firstType = unit.parts[0]?.unit_type
  return TYPE_META[firstType] || { label: firstType || 'Unit', cls: 'tag-default' }
}

// Small badge showing whether a reading material's content has been extracted.
function ContentBadge({ part }) {
  if (part.content_status === 'EXTRACTED') {
    return (
      <span className="content-badge ok">
        {Number(part.content_chars).toLocaleString()} chars
      </span>
    )
  }
  if (part.content_status === 'EMPTY') {
    return (
      <span className="content-badge empty" data-tip="Extraction ran but the portal returned no content">
        empty
      </span>
    )
  }
  if (part.content_status === 'ERROR') {
    return (
      <span className="content-badge err" data-tip={part.content_error || 'Extraction failed'}>
        extract failed
      </span>
    )
  }
  return <span className="content-badge none">not extracted</span>
}

// Badge shown on a reading material once it has been indexed into the RAG store.
function IngestBadge({ part }) {
  return (
    <span className="content-badge ingested" title="Indexed into the RAG store">
      <Database size={10} /> {Number(part.chunk_count).toLocaleString()} chunk
      {part.chunk_count === 1 ? '' : 's'}
    </span>
  )
}

// One unit container: a header (kind tag + label) and its labelled parts.
function UnitCard({ unit, course, onSyncUnit, index = 0 }) {
  const meta = headerTag(unit)
  // Which reading-material part (if any) is expanded inline to show its markdown.
  const [openPart, setOpenPart] = useState(null)

  // Reading materials are the only parts with extractable content. A per-unit
  // "Sync content" re-extracts just this learning set; it needs a token only
  // when some reading material has no stored resource id (token-free otherwise).
  const readingParts = unit.parts.filter((p) => p.label === 'Reading Material')
  const canSync = onSyncUnit && readingParts.length > 0
  const needsToken = readingParts.some((p) => !(p.resource_ids?.length))

  return (
    <div className="unit-card" style={{ '--stagger': `${Math.min(index, 10) * 35}ms` }}>
      <div className="unit-card-head">
        <span className={`unit-tag ${meta.cls}`}>{meta.label}</span>
        <span className="unit-title">{unit.label}</span>
        {canSync && (
          <button
            type="button"
            className="btn btn-ghost btn-sm unit-sync"
            data-tip={
              needsToken
                ? 'Re-fetch this learning set’s content (a Bearer token will be requested)'
                : 'Re-fetch this learning set’s latest content via the admin panel'
            }
            onClick={() =>
              onSyncUnit(course, readingParts.map((p) => p.unit_id), needsToken)
            }
          >
            <RefreshCw size={12} /> Sync
          </button>
        )}
      </div>
      <div className="unit-card-body">
        {unit.parts.map((part, idx) => {
          const flagged =
            part.label === 'Reading Material' &&
            (part.content_status === 'EMPTY' || part.content_status === 'ERROR')
          const key = `${part.unit_id}-${idx}`
          // Reading material with extracted content can be expanded to read its markdown.
          const canView =
            part.label === 'Reading Material' &&
            part.content_status === 'EXTRACTED' &&
            Boolean(part.unit_id)
          const isOpen = openPart === key
          return (
          <Fragment key={key}>
          <div className={`resource-row ${flagged ? 'flagged' : ''}`}>
            <span className={`res-tag ${partTagClass(part.label)}`}>{part.label}</span>
            {part.error ? (
              <span className="error-text">
                <AlertTriangle size={12} /> {part.error}
              </span>
            ) : part.link ? (
              <a className="resource-link" href={part.link} target="_blank" rel="noreferrer">
                {part.name || part.unit_id} <ExternalLink size={11} />
              </a>
            ) : (
              <span className="resource-name">{part.name || part.unit_id}</span>
            )}
            {part.label === 'Reading Material' && <ContentBadge part={part} />}
            {part.label === 'Reading Material' && part.is_ingested && <IngestBadge part={part} />}
            {part.resource_ids?.map((rid) => (
                <button
                  type="button"
                  key={rid}
                  className="resource-id"
                  data-tip="Learning Resource ID — click to copy"
                  onClick={() => navigator.clipboard?.writeText(rid)}
                >
                  <Hash size={10} />
                  {rid}
                </button>
              ))}
            {canView && (
              <button
                type="button"
                className="btn btn-ghost btn-sm res-view"
                aria-expanded={isOpen}
                data-tip="Read the extracted reading material"
                onClick={() => setOpenPart(isOpen ? null : key)}
              >
                <BookOpen size={12} /> {isOpen ? 'Hide' : 'View'}
              </button>
            )}
          </div>
          {canView && (
            <div className={`collapse ${isOpen ? 'open' : ''}`}>
              <div className="collapse-inner">
                <div className="part-reading-wrap">
                  {isOpen && (
                    <ReadingMaterialPane courseId={course.course_id} unitId={part.unit_id} />
                  )}
                </div>
              </div>
            </div>
          )}
          </Fragment>
          )
        })}
      </div>
    </div>
  )
}

// A topic with its units; collapsible, and responds to the course-level
// expand/collapse-all-units signal.
function TopicSection({ topic, unitsSignal, course, onSyncUnit }) {
  const [open, setOpen] = useState(true)

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- sync with the bulk toggle broadcast
    if (unitsSignal) setOpen(unitsSignal.mode === 'expand')
  }, [unitsSignal])

  return (
    <div className="topic">
      <div className="topic-head">
        <button
          className={`expander expander-sm ${open ? 'open' : ''}`}
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          aria-label={open ? 'Collapse topic' : 'Expand topic'}
        >
          <ChevronRight size={14} />
        </button>
        <span className="unit-tag tag-topic">
          <BookOpen size={10} /> Topic
        </span>
        <a className="topic-title" href={topic.topic_link} target="_blank" rel="noreferrer">
          {topic.topic_name || topic.topic_id}
          <ExternalLink size={12} />
        </a>
        <span className="topic-count">
          {topic.units.length} unit{topic.units.length === 1 ? '' : 's'}
        </span>
      </div>
      <div className={`collapse ${open ? 'open' : ''}`}>
        <div className="collapse-inner">
          <div className="unit-grid">
            {topic.units.map((unit, idx) => (
              <UnitCard
                key={`${unit.kind}-${idx}`}
                unit={unit}
                index={idx}
                course={course}
                onSyncUnit={onSyncUnit}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

// One saved course: summary header + actionable pipeline + animated
// expandable hierarchy.
function CourseCard({
  course,
  onSync,
  onAddPrerequisite,
  onExtract,
  onIngest,
  onSyncUnit,
  activeJobsByCourse,
  expandSignal,
  dataVersion = 0,
  nested = false,
  index = 0,
}) {
  const [expanded, setExpanded] = useState(false)
  const [detail, setDetail] = useState(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  // Course-level broadcast to its topic sections.
  const [allUnitsOpen, setAllUnitsOpen] = useState(true)
  const [unitsSignal, setUnitsSignal] = useState(null)
  const seenVersion = useRef(dataVersion)

  const activeJobs = activeJobsByCourse?.get(course.course_id)
  const syncing = Boolean(activeJobs?.has('SYNC'))
  const extracting = Boolean(activeJobs?.has('EXTRACT'))
  const ingesting = Boolean(activeJobs?.has('RAG'))

  async function loadDetail() {
    setLoadingDetail(true)
    try {
      setDetail(await getCourse(course.course_id))
    } catch {
      setDetail(null)
    } finally {
      setLoadingDetail(false)
    }
  }

  function setOpen(next) {
    setExpanded(next)
    if (next && !detail) loadDetail()
  }

  // Re-fetch detail when data changes upstream (e.g. a sync added a prerequisite).
  useEffect(() => {
    if (seenVersion.current !== dataVersion) {
      seenVersion.current = dataVersion
      // eslint-disable-next-line react-hooks/set-state-in-effect -- refresh stale detail after an upstream sync
      if (expanded) loadDetail()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataVersion])

  // Page-level expand/collapse-all broadcast.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- sync with the bulk toggle broadcast
    if (expandSignal) setOpen(expandSignal.mode === 'expand')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expandSignal])

  function toggleAllUnits() {
    const mode = allUnitsOpen ? 'collapse' : 'expand'
    setAllUnitsOpen(!allUnitsOpen)
    setUnitsSignal((s) => ({ mode, seq: (s?.seq || 0) + 1 }))
  }

  // Pipeline steps double as actions: clicking the next 'ready' step runs it.
  // Extraction/RAG run at the top level (they include prerequisites).
  function handleStepAction(key) {
    if (key === 'sync') onSync(course)
    if (nested) return
    if (key === 'extract') onExtract(course)
    if (key === 'rag') onIngest(course)
  }

  const hasBody = loadingDetail || detail
  const busy = syncing || extracting || ingesting

  return (
    <div
      className={`course-card ${nested ? 'nested' : ''} ${expanded ? 'expanded' : ''} ${busy ? 'syncing' : ''}`}
      style={{ '--stagger': `${Math.min(index, 8) * 50}ms` }}
    >
      <div className="course-card-head">
        <button
          className={`expander ${expanded ? 'open' : ''}`}
          onClick={() => setOpen(!expanded)}
          aria-expanded={expanded}
          aria-label={expanded ? 'Collapse course' : 'Expand course'}
        >
          <ChevronRight size={16} />
        </button>

        <div className="course-card-info" onClick={() => setOpen(!expanded)}>
          <div className="course-card-title">
            <strong>{course.course_name || course.course_id}</strong>
            <EnvBadge env={course.environment} />
            {course.is_latest_version && (
              <span className="badge badge-latest">
                <Star size={10} /> latest
              </span>
            )}
            {course.is_ingested && (
              <span
                className="badge badge-ingested"
                data-tip={
                  course.ingested_chunk_count
                    ? `${course.ingested_chunk_count.toLocaleString()} chunks indexed in the RAG store`
                    : 'Indexed in the RAG store'
                }
              >
                <CheckCircle2 size={10} /> ingested
              </span>
            )}
            {course.content_issue_count > 0 && (
              <span
                className="badge badge-warn"
                data-tip={`${course.content_issue_count} reading material${course.content_issue_count === 1 ? '' : 's'} with no content or a failed extraction — expand to see which`}
              >
                <AlertTriangle size={10} /> {course.content_issue_count} flagged
              </span>
            )}
          </div>
          <div className="course-card-meta">
            {course.course_category && !/^-+$/.test(course.course_category) && (
              <span>{course.course_category}</span>
            )}
            {course.selected_version_id && <span>{course.selected_version_id}</span>}
            <span>
              {course.topic_count} topic{course.topic_count === 1 ? '' : 's'}
            </span>
            {course.prerequisite_count > 0 && (
              <span>
                {course.prerequisite_count} prerequisite
                {course.prerequisite_count === 1 ? '' : 's'}
              </span>
            )}
          </div>
        </div>

        <div className="course-card-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => onAddPrerequisite(course)}
            data-tip="Link another course as a prerequisite"
          >
            <GitMerge size={14} /> Prerequisite
          </button>
          {!nested && (
            <>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={extracting}
                onClick={() => onExtract(course)}
                data-tip={
                  extracting
                    ? 'Extraction in progress…'
                    : 'Extract reading material (Bearer tokens required)'
                }
              >
                {extracting ? <Spinner size={13} /> : <FileText size={14} />}
                {extracting ? 'Extracting…' : 'Extract'}
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={!course.content_extracted_at || ingesting}
                data-tip={
                  ingesting
                    ? 'Ingestion in progress…'
                    : course.is_ingested
                      ? 'Re-index the selected learning resources into the RAG store'
                      : course.content_extracted_at
                        ? 'Choose which learning resources to ingest into the RAG index'
                        : 'Extract content first'
                }
                onClick={() => onIngest(course)}
              >
                {ingesting ? <Spinner size={13} /> : <Database size={14} />}
                {ingesting ? 'Ingesting…' : course.is_ingested ? 'Re-ingest' : 'Ingest Content'}
              </button>
            </>
          )}
          <button
            className="btn btn-primary btn-sm"
            disabled={syncing}
            onClick={() => onSync(course)}
          >
            {syncing ? (
              <>
                <Spinner size={13} /> Syncing…
              </>
            ) : (
              <>
                <RefreshCw size={13} /> Sync
              </>
            )}
          </button>
        </div>
      </div>

      <div className="course-card-pipeline">
        <Pipeline
          steps={coursePipeline(course, { syncing, extracting, ingesting })}
          onStepAction={handleStepAction}
        />
      </div>

      <div className={`collapse ${expanded ? 'open' : ''}`}>
        <div className="collapse-inner">
          {hasBody ? (
            <div className="course-card-body">
              {loadingDetail && (
                <div className="form-stack">
                  <Skeleton height={16} width="40%" />
                  <Skeleton height={64} />
                  <Skeleton height={64} width="85%" />
                </div>
              )}

              {detail && detail.prerequisites && detail.prerequisites.length > 0 && (
                <div className="prereq-block">
                  <div className="section-label">
                    <GitMerge size={13} /> Prerequisites
                  </div>
                  {detail.prerequisites.map((p, i) => (
                    <CourseCard
                      key={p.course_id}
                      course={p}
                      index={i}
                      onSync={onSync}
                      onAddPrerequisite={onAddPrerequisite}
                      onExtract={onExtract}
                      onIngest={onIngest}
                      onSyncUnit={onSyncUnit}
                      activeJobsByCourse={activeJobsByCourse}
                      expandSignal={expandSignal}
                      dataVersion={dataVersion}
                      nested
                    />
                  ))}
                </div>
              )}

              {detail && detail.topics.length === 0 && (
                <p className="muted">No topics stored for this course yet.</p>
              )}

              {detail && detail.topics.length > 0 && (
                <div className="topics-toolbar">
                  <div className="section-label">
                    <BookOpen size={13} /> Topics
                  </div>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={toggleAllUnits}
                    data-tip={
                      allUnitsOpen ? 'Collapse every topic' : 'Expand every topic'
                    }
                  >
                    {allUnitsOpen ? <ChevronsDownUp size={13} /> : <ChevronsUpDown size={13} />}
                    {allUnitsOpen ? 'Collapse units' : 'Expand units'}
                  </button>
                </div>
              )}
              {detail &&
                detail.topics.map((topic) => (
                  <TopicSection
                    key={topic.topic_id}
                    topic={topic}
                    unitsSignal={unitsSignal}
                    course={course}
                    onSyncUnit={onSyncUnit}
                  />
                ))}
            </div>
          ) : (
            <div />
          )}
        </div>
      </div>
    </div>
  )
}

export default CourseCard
