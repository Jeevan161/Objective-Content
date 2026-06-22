// Derives the workflow pipeline for a course from its summary fields.
// States: 'done' | 'running' | 'ready' (next actionable) | 'todo' (blocked
// on earlier steps) | 'soon' (feature not built yet). Steps with `action`
// are rendered as clickable when 'ready'; `hint` feeds the tooltip.
// Labels follow the state so the strip always reads as live status.
export function coursePipeline(
  course,
  { syncing = false, extracting = false, ingesting = false } = {},
) {
  const synced = Boolean(course.last_synced_at)
  const extracted = Boolean(course.content_extracted_at)
  const ingested = Boolean(course.is_ingested)
  return [
    {
      key: 'sync',
      action: true,
      state: syncing ? 'running' : synced ? 'done' : 'ready',
      label: syncing ? 'Syncing…' : synced ? 'Synced' : 'Sync',
      hint: syncing
        ? 'Fetching the course hierarchy from the portal…'
        : synced
          ? 'Course hierarchy fetched — click Sync to refresh'
          : 'Click to fetch the course hierarchy',
    },
    {
      key: 'extract',
      action: true,
      state: extracting ? 'running' : extracted ? 'done' : synced ? 'ready' : 'todo',
      label: extracting ? 'Extracting…' : extracted ? 'Content extracted' : 'Extract content',
      hint: extracting
        ? 'Fetching reading material content…'
        : extracted
          ? 'Reading material extracted'
          : synced
            ? 'Click to extract content (Bearer tokens required)'
            : 'Sync the course first',
    },
    {
      key: 'rag',
      action: true,
      state: ingesting ? 'running' : ingested ? 'done' : extracted ? 'ready' : 'todo',
      label: ingesting
        ? 'Ingesting…'
        : ingested
          ? `Ingested${course.ingested_chunk_count ? ` · ${course.ingested_chunk_count} chunks` : ''}`
          : extracted
            ? 'Ingest content'
            : 'Ingestion',
      hint: ingesting
        ? 'Indexing the selected reading material into the RAG store…'
        : ingested
          ? 'Indexed into the RAG store — click Ingest Content to re-index'
          : extracted
            ? 'Click to choose which learning resources to ingest'
            : 'Extract content first',
    },
  ]
}
