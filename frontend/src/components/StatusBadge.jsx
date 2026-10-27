const LABELS = {
  pending: 'Queued',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

export default function StatusBadge({ status, partial = false }) {
  const label = status === 'completed' && partial ? 'Partial' : LABELS[status] || status
  const tone = status === 'completed' && partial ? 'warning' : status
  return (
    <span className={`badge badge--${tone}`}>
      {status === 'running' && <span className="badge__pulse" aria-hidden="true" />}
      {label}
    </span>
  )
}
