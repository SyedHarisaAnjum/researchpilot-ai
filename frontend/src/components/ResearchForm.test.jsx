import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../services/api'
import ResearchForm from './ResearchForm'

afterEach(() => vi.restoreAllMocks())

const QUESTION = 'Compare free LLM APIs for agentic research'

describe('ResearchForm', () => {
  it('keeps submit disabled until the question is long enough', async () => {
    render(<ResearchForm onCreated={vi.fn()} />)
    const button = screen.getByRole('button', { name: 'Start research' })
    expect(button).toBeDisabled()
    await userEvent.type(screen.getByRole('textbox'), 'short')
    expect(button).toBeDisabled()
    expect(screen.getByText('At least 10 characters')).toBeInTheDocument()
    await userEvent.type(screen.getByRole('textbox'), ' question here')
    expect(button).toBeEnabled()
  })

  it('submits the trimmed question and chosen depth', async () => {
    const job = { id: 'job-1' }
    vi.spyOn(api, 'createResearch').mockResolvedValue(job)
    const onCreated = vi.fn()
    render(<ResearchForm onCreated={onCreated} />)
    await userEvent.type(screen.getByRole('textbox'), `  ${QUESTION}  `)
    await userEvent.click(screen.getByLabelText(/Deep/))
    await userEvent.click(screen.getByRole('button', { name: 'Start research' }))
    expect(api.createResearch).toHaveBeenCalledWith(QUESTION, 'deep')
    expect(onCreated).toHaveBeenCalledWith(job)
  })

  it('shows a loading state while submitting', async () => {
    vi.spyOn(api, 'createResearch').mockReturnValue(new Promise(() => {}))
    render(<ResearchForm onCreated={vi.fn()} />)
    await userEvent.type(screen.getByRole('textbox'), QUESTION)
    await userEvent.click(screen.getByRole('button', { name: 'Start research' }))
    expect(screen.getByRole('button', { name: 'Starting…' })).toBeDisabled()
    expect(screen.getByRole('textbox')).toBeDisabled()
  })

  it('shows API errors and lets the user try again', async () => {
    vi.spyOn(api, 'createResearch').mockRejectedValue(new ApiError("Can't reach the research API", { code: 'network_error' }))
    render(<ResearchForm onCreated={vi.fn()} />)
    await userEvent.type(screen.getByRole('textbox'), QUESTION)
    await userEvent.click(screen.getByRole('button', { name: 'Start research' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("Can't reach the research API")
    expect(screen.getByRole('button', { name: 'Start research' })).toBeEnabled()
  })
})
