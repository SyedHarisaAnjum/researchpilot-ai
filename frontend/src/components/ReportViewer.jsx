import { Fragment } from 'react'
import { formatDate, formatDateTime } from '../utils/format'

// Statements the backend flagged are listed under "Flagged claims", not in the findings.
const FLAGGED = new Set(['not_supported', 'no_citation'])

function Citation({ id, reference }) {
  return (
    <a
      className="cite"
      href={`#ref-${id}`}
      title={reference ? `${reference.title || reference.domain} — ${reference.domain}` : `Source ${id}`}
    >
      {id}
    </a>
  )
}

function Citations({ ids, references }) {
  if (!ids?.length) return null
  return (
    <span className="cites">
      {ids.map((id) => (
        <Citation key={id} id={id} reference={references[id]} />
      ))}
    </span>
  )
}

/** Text with inline [S01] markers turned into citation links. */
function CitedText({ text, references }) {
  const parts = text.split(/(\[S\d+\])/g)
  return parts.map((part, i) => {
    const match = part.match(/^\[(S\d+)\]$/)
    return match ? <Citation key={i} id={match[1]} reference={references[match[1]]} /> : <Fragment key={i}>{part}</Fragment>
  })
}

function VerificationTag({ statement }) {
  if (statement.kind === 'interpretation') return <span className="tag tag--neutral">Interpretation</span>
  if (statement.verification === 'partially_supported')
    return (
      <span className="tag tag--warning" title={statement.verification_note || ''}>
        Partly supported
      </span>
    )
  if (statement.verification === 'unchecked')
    return (
      <span className="tag tag--neutral" title={statement.verification_note || ''}>
        Not verified
      </span>
    )
  return null
}

function Statement({ statement, references }) {
  return (
    <li className={`statement ${statement.kind === 'interpretation' ? 'statement--interpretation' : ''}`}>
      <span>{statement.text}</span> <Citations ids={statement.source_ids} references={references} />{' '}
      <VerificationTag statement={statement} />
    </li>
  )
}

export default function ReportViewer({ report }) {
  const references = Object.fromEntries(report.sources.map((s) => [s.source_id, s]))
  const conclusions = report.conclusions.filter((s) => !FLAGGED.has(s.verification))
  const verification = report.verification || {}
  const checked = (verification.supported || 0) + (verification.partially_supported || 0) + (verification.not_supported || 0)

  return (
    <article className="report">
      <header className="report__header">
        <h1 className="report__title">{report.title}</h1>
        <p className="report__question">{report.question}</p>
        <p className="muted small">Generated {formatDateTime(report.generated_at)}</p>
      </header>

      {report.partial && (
        <div className="alert alert--warning">
          <strong>Partial report.</strong> {report.stop_reason ? `${report.stop_reason}. ` : ''}
          Some questions could not be fully answered — see Limitations.
        </div>
      )}

      <section className="report__section">
        <h2>Executive summary</h2>
        <p className="report__summary">
          <CitedText text={report.summary} references={references} />
        </p>
        {checked > 0 && (
          <p className="muted small">
            {verification.supported || 0} of {checked} factual statements were confirmed against their cited sources
            {verification.not_supported
              ? `; ${verification.not_supported} ${verification.not_supported === 1 ? 'was' : 'were'} flagged and left out`
              : ''}
            .
          </p>
        )}
      </section>

      <section className="report__section">
        <h2>Detailed findings</h2>
        {report.findings.map((finding) => (
          <div key={finding.heading} className="finding">
            <h3>{finding.heading}</h3>
            <ul className="statements">
              {finding.statements
                .filter((s) => !FLAGGED.has(s.verification))
                .map((s) => (
                  <Statement key={s.statement_id} statement={s} references={references} />
                ))}
            </ul>
          </div>
        ))}
      </section>

      {report.comparison && (
        <section className="report__section">
          <h2>{report.comparison.title}</h2>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  {report.comparison.columns.map((c) => (
                    <th key={c}>{c}</th>
                  ))}
                  <th>Sources</th>
                </tr>
              </thead>
              <tbody>
                {report.comparison.rows.map((row, i) => (
                  <tr key={i}>
                    {row.cells.map((cell, j) => (
                      <td key={j} className={cell === 'unknown' ? 'muted' : ''}>
                        {cell}
                      </td>
                    ))}
                    <td>
                      {row.source_ids.length ? (
                        <Citations ids={row.source_ids} references={references} />
                      ) : (
                        <span className="tag tag--warning">No source</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {conclusions.length > 0 && (
        <section className="report__section">
          <h2>Conclusions</h2>
          <ul className="statements">
            {conclusions.map((s) => (
              <Statement key={s.statement_id} statement={s} references={references} />
            ))}
          </ul>
        </section>
      )}

      <section className="report__section">
        <h2>Limitations and unresolved questions</h2>
        {report.limitations.length ? (
          <ul className="bullets">
            {report.limitations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        ) : (
          <p className="muted">No limitations were recorded.</p>
        )}
        {report.unresolved_questions.length > 0 && (
          <>
            <h3>Unresolved questions</h3>
            <ul className="bullets">
              {report.unresolved_questions.map((q) => (
                <li key={q}>{q}</li>
              ))}
            </ul>
          </>
        )}
        {report.flagged_claims.length > 0 && (
          <div className="alert alert--warning flagged">
            <strong>Flagged claims</strong> — left out of the findings because the cited sources did not support them.
            Don't rely on these:
            <ul className="bullets">
              {report.flagged_claims.map((claim) => (
                <li key={claim}>{claim}</li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <details className="report__section disclosure">
        <summary>Methodology and scope</summary>
        <ul className="bullets">
          {report.methodology.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <ul className="bullets">
          {report.scope.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </details>

      <section className="report__section">
        <h2>References</h2>
        {report.sources.length ? (
          <ol className="references">
            {report.sources.map((ref) => (
              <li key={ref.source_id} id={`ref-${ref.source_id}`} className="reference">
                <span className="source__id source__id--cited">{ref.source_id}</span>
                <div>
                  <a href={ref.url} target="_blank" rel="noopener noreferrer">
                    {ref.title || ref.domain}
                  </a>
                  <div className="source__meta">
                    <span>{ref.domain}</span>
                    <span>Published {formatDate(ref.published_at)}</span>
                    <span>Retrieved {formatDateTime(ref.retrieved_at)}</span>
                  </div>
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <p className="muted">This report cites no sources.</p>
        )}
      </section>
    </article>
  )
}
