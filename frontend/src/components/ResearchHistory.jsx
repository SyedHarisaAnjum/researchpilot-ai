import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../services/api'
import { timeAgo } from '../utils/format'
import StatusBadge from './StatusBadge'

const REFRESH_MS = 4000

export default function ResearchHistory({ limit = 10 }) {
  const [jobs, setJobs] = useState(null)
  const [error, setError] = useState(null)
  const [refreshCount, setRefreshCount] = useState(0)

  useEffect(() => {
    let active = true
    api
      .listResearch({ limit })
      .then((data) => {
        if (!active) return
        setJobs(data.jobs)
        setError(null)
      })
      .catch((err) => active && setError(err.message))
    return () => {
      active = false
    }
  }, [limit, refreshCount])

  const hasActive = jobs?.some((j) => j.status === 'pending' || j.status === 'running')

  // Keep statuses fresh only while something is still running.
  useEffect(() => {
    if (!hasActive) return undefined
    const timer = setInterval(() => setRefreshCount((n) => n + 1), REFRESH_MS)
    return () => clearInterval(timer)
  }, [hasActive])

  return (
    <section className="history">
      <div className="section-header">
        <h2>Recent research</h2>
        <button type="button" className="button button--ghost button--small" onClick={() => setRefreshCount((n) => n + 1)}>
          Refresh
        </button>
      </div>

      {error && <div className="alert alert--error">{error}</div>}
      {!error && jobs === null && <p className="muted">Loading…</p>}
      {jobs?.length === 0 && <p className="muted empty">No research yet. Your reports will appear here.</p>}

      {jobs?.length > 0 && (
        <ul className="history-list">
          {jobs.map((job) => (
            <li key={job.id}>
              <Link to={`/research/${job.id}`} className="history-item">
                <span className="history-item__question">{job.question}</span>
                <span className="history-item__meta">
                  <StatusBadge status={job.status} />
                  <span className="muted small">
                    {job.depth} · {timeAgo(job.created_at)}
                    {job.status === 'running' ? ` · ${job.progress}%` : ''}
                  </span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
