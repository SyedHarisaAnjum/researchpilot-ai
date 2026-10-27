import { useState } from 'react'
import StatusBadge from './StatusBadge'

const STEPS = [
  { label: 'Plan research', stages: ['pending', 'planning'] },
  { label: 'Search and read sources', stages: ['searching', 'reading'] },
  { label: 'Analyze evidence', stages: ['extracting', 'analyzing'] },
  { label: 'Write final report', stages: ['writing'] },
]
const STOPPED = new Set(['failed', 'cancelled'])
const STEP_LABELS = { complete: 'Complete', running: 'Running', pending: 'Pending', failed: 'Failed', cancelled: 'Stopped', waiting: 'Queued' }

/** The step the job is on, or was on when it stopped (taken from the event log). */
function currentStep(job, events) {
  if (job.stage === 'completed') return STEPS.length
  let stage = job.stage
  if (STOPPED.has(stage)) {
    const last = [...events].reverse().find((e) => e.stage && !STOPPED.has(e.stage) && e.stage !== 'completed')
    stage = last?.stage || 'planning'
  }
  const index = STEPS.findIndex((step) => step.stages.includes(stage))
  return index === -1 ? 0 : index
}

function stepState(index, current, job) {
  if (index < current) return 'complete'
  if (index > current) return 'pending'
  if (job.status === 'pending') return 'waiting'
  if (STOPPED.has(job.status)) return job.status
  return 'running'
}

export default function ResearchProgress({ job, events, onCancel, streamLost }) {
  const [cancelling, setCancelling] = useState(false)
  const [cancelError, setCancelError] = useState(null)
  const current = currentStep(job, events)
  const stats = job.stats || {}
  const limits = job.limits || {}
  const warnings = events.filter((e) => e.kind === 'warning')
  const isActive = job.status === 'pending' || job.status === 'running'
  const lastMessage = [...events].reverse().find((e) => e.message)?.message

  async function handleCancel() {
    setCancelling(true)
    setCancelError(null)
    try {
      await onCancel()
    } catch (err) {
      setCancelError(err.message)
    } finally {
      setCancelling(false)
    }
  }

  return (
    <section className="card progress-card" aria-live="polite">
      <div className="card__header">
        <div>
          <h2 className="card__title">Research progress</h2>
          {lastMessage && <p className="muted small">{lastMessage}</p>}
        </div>
        <div className="row gap-sm">
          <StatusBadge status={job.status} />
          {isActive && (
            <button type="button" className="button button--ghost button--small" onClick={handleCancel} disabled={cancelling}>
              {cancelling ? 'Cancelling…' : 'Cancel'}
            </button>
          )}
        </div>
      </div>

      <div className="progress-bar" role="progressbar" aria-valuenow={job.progress} aria-valuemin={0} aria-valuemax={100}>
        <div className={`progress-bar__fill progress-bar__fill--${job.status}`} style={{ width: `${job.progress}%` }} />
      </div>

      <ol className="steps">
        {STEPS.map((step, index) => {
          const state = stepState(index, current, job)
          return (
            <li key={step.label} className={`step step--${state}`}>
              <span className="step__marker" aria-hidden="true">
                {state === 'complete' ? '✓' : state === 'failed' || state === 'cancelled' ? '✕' : index + 1}
              </span>
              <span className="step__label">{step.label}</span>
              <span className="step__state">{STEP_LABELS[state]}</span>
            </li>
          )
        })}
      </ol>

      <dl className="stats">
        <div>
          <dt>Round</dt>
          <dd>
            {stats.rounds ?? 0} <span className="muted">of {limits.max_rounds ?? '?'}</span>
          </dd>
        </div>
        <div>
          <dt>Searches</dt>
          <dd>
            {stats.searches ?? 0} <span className="muted">of {limits.max_total_searches ?? '?'}</span>
          </dd>
        </div>
        <div>
          <dt>Pages read</dt>
          <dd>
            {stats.pages_read ?? 0} <span className="muted">of {stats.pages_attempted ?? 0} tried</span>
          </dd>
        </div>
        <div>
          <dt>Evidence</dt>
          <dd>{stats.evidence_items ?? 0}</dd>
        </div>
        <div>
          <dt>Model calls</dt>
          <dd>
            {stats.llm_calls ?? 0} <span className="muted">of {limits.max_llm_calls ?? '?'}</span>
          </dd>
        </div>
      </dl>

      {job.status === 'failed' && job.error && (
        <div className="alert alert--error" role="alert">
          <strong>Research failed.</strong> {job.error}
        </div>
      )}
      {job.status === 'cancelled' && <div className="alert alert--neutral">This research was cancelled.</div>}
      {cancelError && <div className="alert alert--error">{cancelError}</div>}
      {streamLost && isActive && (
        <div className="alert alert--neutral">Live updates were interrupted; checking for progress every few seconds.</div>
      )}

      {warnings.length > 0 && (
        <details className="disclosure">
          <summary>
            {warnings.length} issue{warnings.length === 1 ? '' : 's'} during research
            <span className="muted small"> — pages that couldn't be read, discarded evidence, and similar</span>
          </summary>
          <ul className="event-list">
            {warnings.map((e) => (
              <li key={e.id} className="event event--warning">
                {e.round ? <span className="event__round">Round {e.round}</span> : null}
                {e.message}
              </li>
            ))}
          </ul>
        </details>
      )}

      <details className="disclosure" open={isActive}>
        <summary>Activity log</summary>
        <ol className="event-list">
          {events
            .filter((e) => e.kind !== 'warning')
            .map((e) => (
              <li key={e.id} className={`event event--${e.kind}`}>
                <span className="event__progress">{e.progress ?? ''}%</span>
                {e.message}
              </li>
            ))}
        </ol>
      </details>
    </section>
  )
}
