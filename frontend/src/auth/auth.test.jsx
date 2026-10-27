import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { api, ApiError, SESSION_EXPIRED_EVENT } from '../services/api'

const user = { id: 'u1', email: 'tester@example.com', name: 'Tester', is_admin: false }

afterEach(() => vi.restoreAllMocks())

function stubDashboard() {
  vi.spyOn(api, 'health').mockResolvedValue({ status: 'ok' })
  vi.spyOn(api, 'listResearch').mockResolvedValue({ jobs: [], total: 0 })
}

describe('sign-in', () => {
  it('shows the sign-in page when signed out, then the dashboard after signing in', async () => {
    stubDashboard()
    vi.spyOn(api, 'me').mockRejectedValue(new ApiError('Sign in', { status: 401 }))
    vi.spyOn(api, 'login').mockResolvedValue(user)
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()

    await userEvent.type(screen.getByLabelText('Email'), user.email)
    await userEvent.type(screen.getByLabelText('Password'), 'correct horse battery')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(api.login).toHaveBeenCalledWith(user.email, 'correct horse battery')
    expect(await screen.findByRole('heading', { name: 'What do you want to research?' })).toBeInTheDocument()
    expect(screen.getByText('Tester')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Users' })).not.toBeInTheDocument()
  })

  it('shows sign-in errors', async () => {
    stubDashboard()
    vi.spyOn(api, 'me').mockRejectedValue(new ApiError('Sign in', { status: 401 }))
    vi.spyOn(api, 'login').mockRejectedValue(new ApiError('Incorrect email or password', { status: 401 }))
    render(<App />)
    await userEvent.type(await screen.findByLabelText('Email'), user.email)
    await userEvent.type(screen.getByLabelText('Password'), 'wrong')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Incorrect email or password')
  })

  it('returns to sign-in when the session expires', async () => {
    stubDashboard()
    vi.spyOn(api, 'me').mockResolvedValue({ ...user, is_admin: true })
    render(<App />)
    expect(await screen.findByRole('link', { name: 'Users' })).toBeInTheDocument()
    act(() => window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT)))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('signs out', async () => {
    stubDashboard()
    vi.spyOn(api, 'me').mockResolvedValue(user)
    vi.spyOn(api, 'logout').mockResolvedValue(null)
    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(api.logout).toHaveBeenCalled()
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })
})

describe('requests', () => {
  it('send the CSRF header and the session cookie', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({}) })
    await api.createResearch('A long enough question', 'quick')
    const [, options] = fetch.mock.calls[0]
    expect(options.credentials).toBe('include')
    expect(options.headers['X-Requested-With']).toBe('ResearchLab')
  })

  it('announce an expired session on 401', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 401, json: () => Promise.resolve({ error: { code: 'not_authenticated' } }) })
    const listener = vi.fn()
    window.addEventListener(SESSION_EXPIRED_EVENT, listener)
    await expect(api.listResearch()).rejects.toMatchObject({ status: 401 })
    expect(listener).toHaveBeenCalled()
    window.removeEventListener(SESSION_EXPIRED_EVENT, listener)
  })
})
