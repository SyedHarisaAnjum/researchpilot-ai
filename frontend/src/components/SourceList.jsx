import { formatDate, formatDateTime } from '../utils/format'

const FETCH_LABELS = {
  blocked: 'Blocked',
  unsupported: 'Unsupported',
  unsafe: 'Unsafe URL',
  failed: 'Failed',
}

export default function SourceList({ sources, citedIds }) {
  if (!sources.length) {
    return <p className="muted">No sources were recorded for this research.</p>
  }
  const read = sources.filter((s) => s.fetch_status === 'success').length

  return (
    <div className="sources">
      <p className="muted small">
        {sources.length} pages tried · {read} read · {sources.length - read} could not be read
      </p>
      <ul className="source-list">
        {sources.map((source) => {
          const url = source.final_url || source.url
          const ok = source.fetch_status === 'success'
          return (
            <li key={source.source_id} className={`source ${ok ? '' : 'source--failed'}`}>
              <span className={`source__id ${citedIds?.has(source.source_id) ? 'source__id--cited' : ''}`}>
                {source.source_id}
              </span>
              <div className="source__body">
                <a className="source__title" href={url} target="_blank" rel="noopener noreferrer">
                  {source.title || source.domain}
                </a>
                <div className="source__meta">
                  <span>{source.domain}</span>
                  <span>Published {formatDate(source.published_at)}</span>
                  <span>Retrieved {formatDateTime(source.retrieved_at)}</span>
                  {ok && <span>{source.evidence_count} evidence item{source.evidence_count === 1 ? '' : 's'}</span>}
                  {!ok && <span className="tag tag--warning">{FETCH_LABELS[source.fetch_status] || source.fetch_status}</span>}
                </div>
                {!ok && source.error && <div className="source__error">{source.error}</div>}
                <div className="source__query">Found by “{source.query}” · round {source.round}</div>
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
