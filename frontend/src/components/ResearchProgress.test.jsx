import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { runningJob } from '../test/fixtures'
import ResearchProgress from './ResearchProgress'

const events = [
  { id: 1, kind: 'queued', stage: 'pending', progress: 0, message: 'Research job created' },
  { id: 2, kind: 'progress', stage: 'searching', progress: 10, message: 'Planned 3 subquestions' },
  { id: 3, kind: 'warning', stage: 'extracting', round: 1, message: 'blocked: HTTP 403' },
  { id: 4, kind: 'progress', stage: 'analyzing', progress: 66, message: 'Round 1: extracted 12 verified evidence items' },
]

function stepStates() {
  return screen.getAllByRole('listitem').filter((li) => li.classList.contains('step')).map((li) => li.className.replace('step step--', ''))
}

describe('ResearchProgress', () => {
  it('shows the current step, counts and latest message', () => {
    render(<ResearchProgress job={runningJob} events={events} onCancel={vi.fn()} />)
    expect(stepStates()).toEqual(['complete', 'complete', 'running', 'pending'])
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '66')
    expect(screen.getByText('Searches').nextSibling).toHaveTextContent('3 of 9')
    expect(screen.getByText('Pages read').nextSibling).toHaveTextContent('4 of 5 tried')
    expect(screen.getAllByText('Round 1: extracted 12 verified evidence items').length).toBeGreaterThan(0)
  })

  it('lists warnings separately', () => {
    render(<ResearchProgress job={runningJob} events={events} onCancel={vi.fn()} />)
    const issues = screen.getByText(/1 issue during research/).closest('details')
    expect(within(issues).getByText('blocked: HTTP 403')).toBeInTheDocument()
  })

  it('shows the failure reason and where it stopped', () => {
    const failed = { ...runningJob, status: 'failed', stage: 'failed', error: 'Model request failed (404)' }
    render(<ResearchProgress job={failed} events={events} onCancel={vi.fn()} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Research failed. Model request failed (404)')
    expect(stepStates()).toEqual(['complete', 'complete', 'failed', 'pending'])
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
  })

  it('marks every step complete when done', () => {
    render(<ResearchProgress job={{ ...runningJob, status: 'completed', stage: 'completed', progress: 100 }} events={events} onCancel={vi.fn()} />)
    expect(stepStates()).toEqual(['complete', 'complete', 'complete', 'complete'])
  })

  it('cancels and reports cancel errors', async () => {
    const onCancel = vi.fn().mockRejectedValue(new Error('Job is already completed'))
    render(<ResearchProgress job={runningJob} events={events} onCancel={onCancel} />)
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalled()
    expect(await screen.findByText('Job is already completed')).toBeInTheDocument()
  })

  it('says when live updates fell back to polling', () => {
    render(<ResearchProgress job={runningJob} events={events} onCancel={vi.fn()} streamLost />)
    expect(screen.getByText(/Live updates were interrupted/)).toBeInTheDocument()
  })
})
