import { act, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../services/api'
import { report, runningJob } from '../test/fixtures'
import ReportPage from './ReportPage'

/** A controllable stand-in for the browser's EventSource. */
class FakeEventSource {
  static instances = []
  static CLOSED = 2
  constructor(url) {
    this.url = url
    this.listeners = {}
    this.readyState = 1
    FakeEventSource.instances.push(this)
  }
  addEventListener(type, fn) {
    this.listeners[type] = fn
  }
  emit(type, data) {
    act(() => this.listeners[type]?.({ data: JSON.stringify(data) }))
  }
  close() {
    this.readyState = FakeEventSource.CLOSED
  }
}

function renderPage(jobId = 'job-1') {
  return render(
    <MemoryRouter initialEntries={[`/research/${jobId}`]}>
      <Routes>
        <Route path="/research/:jobId" element={<ReportPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  FakeEventSource.instances = []
  globalThis.EventSource = FakeEventSource
})
afterEach(() => vi.restoreAllMocks())

describe('ReportPage', () => {
  it('shows a loading state, then live progress from the event stream', async () => {
    vi.spyOn(api, 'getResearch').mockResolvedValue({ ...runningJob, stage: 'searching', progress: 10, stats: {} })
    renderPage()
    expect(screen.getByText('Loading research…')).toBeInTheDocument()
    expect(await screen.findByText('Research progress')).toBeInTheDocument()

    const stream = FakeEventSource.instances[0]
    expect(stream.url).toBe('http://api.test/api/research/job-1/events')
    stream.emit('research_progress', { id: 5, stage: 'extracting', progress: 47, message: 'Round 1: read 4 of 5 pages', stats: { pages_read: 4, pages_attempted: 5 } })
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '47')
    expect(screen.getAllByText('Round 1: read 4 of 5 pages').length).toBeGreaterThan(0)
    expect(screen.getByText('Pages read').nextSibling).toHaveTextContent('4 of 5 tried')
  })

  it('loads and shows the report when the job completes', async () => {
    const completed = { ...runningJob, status: 'completed', stage: 'completed', progress: 100, report_id: 'r1' }
    vi.spyOn(api, 'getResearch').mockResolvedValueOnce(runningJob).mockResolvedValue(completed)
    vi.spyOn(api, 'getReport').mockResolvedValue({ report_id: 'r1', job_id: 'job-1', report })
    vi.spyOn(api, 'getSources').mockResolvedValue([])
    renderPage()
    await screen.findByText('Research progress')

    const stream = FakeEventSource.instances[0]
    stream.emit('research_completed', { id: 9, stage: 'completed', progress: 100, message: 'Report ready' })
    stream.emit('end', { status: 'completed' })
    expect(await screen.findByRole('heading', { name: report.title })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Download PDF' })).toHaveAttribute('href', 'http://api.test/api/reports/r1/download?format=pdf')
    expect(screen.getByRole('link', { name: 'Markdown' })).toHaveAttribute('href', 'http://api.test/api/reports/r1/download?format=markdown')
  })

  it('does not ask for a report when the job failed', async () => {
    vi.spyOn(api, 'getResearch').mockResolvedValue({ ...runningJob, status: 'failed', stage: 'failed', error: 'Search provider quota is used up' })
    const getReport = vi.spyOn(api, 'getReport')
    vi.spyOn(api, 'getSources').mockResolvedValue([])
    renderPage()
    expect(await screen.findByRole('alert')).toHaveTextContent('Search provider quota is used up')
    expect(getReport).not.toHaveBeenCalled()
  })

  it('explains an unknown job', async () => {
    vi.spyOn(api, 'getResearch').mockRejectedValue(new ApiError('Research job x not found', { status: 404 }))
    renderPage('missing')
    expect(await screen.findByText('This research job does not exist.')).toBeInTheDocument()
  })

  it('falls back to polling when the stream is lost', async () => {
    vi.spyOn(api, 'getResearch').mockResolvedValue(runningJob)
    renderPage()
    await screen.findByText('Research progress')
    const stream = FakeEventSource.instances[0]
    stream.readyState = FakeEventSource.CLOSED
    act(() => stream.onerror())
    expect(screen.getByText(/Live updates were interrupted/)).toBeInTheDocument()
  })
})
