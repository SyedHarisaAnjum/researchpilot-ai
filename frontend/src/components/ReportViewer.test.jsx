import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { report } from '../test/fixtures'
import ReportViewer from './ReportViewer'

describe('ReportViewer', () => {
  it('turns inline citations into links to the references', () => {
    render(<ReportViewer report={report} />)
    const summary = screen.getByText(/Gemini allows 15 RPM/).closest('p')
    const links = within(summary).getAllByRole('link')
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([['S01', '#ref-S01'], ['S02', '#ref-S02']])
    expect(links[0]).toHaveAttribute('title', expect.stringContaining('Gemini docs'))
  })

  it('links references to their sources and shows both dates', () => {
    render(<ReportViewer report={report} />)
    const reference = document.getElementById('ref-S01')
    const link = within(reference).getByRole('link', { name: 'Gemini docs' })
    expect(link).toHaveAttribute('href', 'https://ai.google.dev/limits')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))
    expect(reference).toHaveTextContent(/Published .*2026/)
    expect(reference).toHaveTextContent(/Retrieved/)
    expect(document.getElementById('ref-S02')).toHaveTextContent('Published unknown')
  })

  it('keeps flagged claims out of the findings and lists them separately', () => {
    render(<ReportViewer report={report} />)
    const findings = screen.getByRole('heading', { name: 'Detailed findings' }).closest('section')
    expect(within(findings).queryByText('Gemini usage is unlimited.')).not.toBeInTheDocument()
    expect(screen.getByText('Gemini usage is unlimited. (Not in excerpt)')).toBeInTheDocument()
  })

  it('labels interpretations and partly supported claims', () => {
    render(<ReportViewer report={report} />)
    expect(screen.getByText('Good enough for small agents.').closest('li')).toHaveTextContent('Interpretation')
    expect(screen.getByText('Groq is faster.').closest('li')).toHaveTextContent('Partly supported')
  })

  it('renders the comparison table and flags rows without sources', () => {
    render(<ReportViewer report={report} />)
    const table = screen.getByRole('table')
    expect(within(table).getAllByRole('row')).toHaveLength(3)
    expect(within(table).getByText('No source')).toBeInTheDocument()
  })

  it('warns that the report is partial and lists unresolved questions', () => {
    render(<ReportViewer report={report} />)
    expect(screen.getByText(/Partial report\./).closest('div')).toHaveTextContent('Round limit reached (3)')
    expect(screen.getByText('What are daily caps?')).toBeInTheDocument()
    expect(screen.getByText(/1 of 3 factual statements were confirmed/)).toHaveTextContent('1 was flagged')
  })
})
