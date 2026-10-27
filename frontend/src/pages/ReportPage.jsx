import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import ReportViewer from '../components/ReportViewer'
import ResearchProgress from '../components/ResearchProgress'
import SourceList from '../components/SourceList'
import StatusBadge from '../components/StatusBadge'
import { useResearchJob } from '../hooks/useResearchJob'
import { api, TERMINAL_STATUSES } from '../services/api'
import { formatDateTime } from '../utils/format'

export default function ReportPage() {
  const { jobId } = useParams()
  // A new key per job gives each job fresh state.
  return <JobView key={jobId} jobId={jobId} />
}

function JobView({ jobId }) {
  const { job, events, report, sources, error, resultsError, streamLost, cancel } = useResearchJob(jobId)
  const [tab, setTab] = useState('report')

  if (error && !job) {
    return (
      <div className="page">
        <Link to="/" className="back-link">
          ← New research
        </Link>
        <div className="alert alert--error">{error.status === 404 ? 'This research job does not exist.' : error.message}</div>
      </div>
    )
  }
  if (!job) {
    return (
      <div className="page">
        <p className="muted">Loading research…</p>
      </div>
    )
  }

  const finished = TERMINAL_STATUSES.has(job.status)
  const reportData = report?.report
  const citedIds = new Set(reportData?.sources.map((s) => s.source_id))

  return (
    <div className="page report-page">
      <Link to="/" className="back-link">
        ← New research
      </Link>

      <header className="job-header">
        <div className="row gap-sm wrap">
          <StatusBadge status={job.status} partial={reportData?.partial} />
          <span className="muted small">
            {job.depth} depth · started {formatDateTime(job.created_at)}
            {job.completed_at ? ` · finished ${formatDateTime(job.completed_at)}` : ''}
          </span>
        </div>
        <h1 className="job-header__question">{job.question}</h1>
      </header>

      {error && <div className="alert alert--error">Couldn't refresh this job: {error.message}</div>}

      {(!finished || !reportData) && (
        <ResearchProgress job={job} events={events} onCancel={cancel} streamLost={streamLost} />
      )}

      {finished && resultsError && <div className="alert alert--error">Couldn't load the report: {resultsError.message}</div>}

      {reportData && (
        <>
          <div className="toolbar">
            <div className="tabs" role="tablist">
              <button type="button" role="tab" aria-selected={tab === 'report'} className={`tab ${tab === 'report' ? 'is-active' : ''}`} onClick={() => setTab('report')}>
                Report
              </button>
              <button type="button" role="tab" aria-selected={tab === 'sources'} className={`tab ${tab === 'sources' ? 'is-active' : ''}`} onClick={() => setTab('sources')}>
                All sources <span className="tab__count">{sources.length}</span>
              </button>
              <button type="button" role="tab" aria-selected={tab === 'activity'} className={`tab ${tab === 'activity' ? 'is-active' : ''}`} onClick={() => setTab('activity')}>
                Activity
              </button>
            </div>
            <div className="row gap-sm">
              <a className="button button--secondary button--small" href={api.downloadUrl(report.report_id, 'pdf')}>
                Download PDF
              </a>
              <a className="button button--ghost button--small" href={api.downloadUrl(report.report_id, 'markdown')}>
                Markdown
              </a>
              <a className="button button--ghost button--small" href={api.downloadUrl(report.report_id, 'json')}>
                JSON
              </a>
            </div>
          </div>

          {tab === 'report' && <ReportViewer report={reportData} />}
          {tab === 'sources' && (
            <section className="card">
              <SourceList sources={sources} citedIds={citedIds} />
            </section>
          )}
          {tab === 'activity' && <ResearchProgress job={job} events={events} onCancel={cancel} streamLost={false} />}
        </>
      )}

      {finished && !reportData && sources.length > 0 && (
        <section className="card">
          <h2 className="card__title">Sources collected before the research stopped</h2>
          <SourceList sources={sources} />
        </section>
      )}
    </div>
  )
}
