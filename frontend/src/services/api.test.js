import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, API_BASE_URL } from './api'

function mockFetch(status, body) {
  globalThis.fetch = vi.fn().mockResolvedValue({ ok: status < 400, status, json: () => Promise.resolve(body) })
}

afterEach(() => vi.restoreAllMocks())

describe('api', () => {
  it('uses the configured base URL', async () => {
    mockFetch(200, { status: 'ok' })
    await api.health()
    expect(API_BASE_URL).toBe('http://api.test')
    expect(fetch).toHaveBeenCalledWith('http://api.test/api/health', expect.anything())
  })

  it('sends the question and depth as JSON', async () => {
    mockFetch(202, { id: 'j1' })
    await api.createResearch('A good question', 'quick')
    const [, options] = fetch.mock.calls[0]
    expect(options.method).toBe('POST')
    expect(JSON.parse(options.body)).toEqual({ question: 'A good question', depth: 'quick' })
  })

  it('explains when the backend cannot be reached', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))
    const error = await api.health().catch((e) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('network_error')
    expect(error.message).toMatch(/Can't reach the research API/)
  })

  it('turns validation errors into readable text', async () => {
    mockFetch(422, { error: { code: 'validation_error', details: [{ loc: ['body', 'question'], msg: 'too short' }] } })
    await expect(api.createResearch('x', 'standard')).rejects.toThrow('question: too short')
  })

  it.each([
    ['llm_rate_limited', /rate limited/],
    ['search_quota_exceeded', /quota is used up/],
    ['search_auth_failed', /TAVILY_API_KEY/],
  ])('uses a friendly message for %s', async (code, message) => {
    mockFetch(429, { error: { code, message: 'raw' } })
    await expect(api.getResearch('j1')).rejects.toThrow(message)
  })

  it('falls back to the server message, then the status code', async () => {
    mockFetch(404, { error: { code: 'http_404', message: 'Research job j1 not found' } })
    await expect(api.getResearch('j1')).rejects.toMatchObject({ status: 404, message: 'Research job j1 not found' })
    mockFetch(500, null)
    await expect(api.getResearch('j1')).rejects.toThrow('HTTP 500')
  })

  it('builds download links', () => {
    expect(api.downloadUrl('r1', 'json')).toBe('http://api.test/api/reports/r1/download?format=json')
  })
})
