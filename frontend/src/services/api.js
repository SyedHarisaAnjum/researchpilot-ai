// HTTP calls to the FastAPI backend. Every error is turned into an ApiError with a readable message.
//
// By default the API is on the same origin as the page: Vite proxies /api in development and
// Nginx does in production. Set VITE_API_BASE_URL only to call an API on another origin.

export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/+$/, '')

// Sent with every request: the backend refuses state-changing requests without it (CSRF protection).
const CSRF_HEADERS = { 'X-Requested-With': 'ResearchLab' }

/** Fired when the session has ended, so the app can show the sign-in page. */
export const SESSION_EXPIRED_EVENT = 'researchlab:session-expired'

export class ApiError extends Error {
  constructor(message, { status = 0, code = 'error', details = null } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

// Clearer wording for errors the backend reports by code.
export const FRIENDLY_MESSAGES = {
  llm_rate_limited: 'The AI model is rate limited right now. Wait a minute and try again.',
  llm_timeout: 'The AI model took too long to respond. Try again, or use a quicker depth.',
  llm_error: 'The AI model request failed. Check GEMINI_API_KEY and GEMINI_MODEL on the server.',
  llm_invalid_response: 'The AI model returned an unusable answer. Try again.',
  llm_invalid_output: 'The AI model returned an unusable answer. Try again.',
  search_rate_limited: 'The search provider is rate limited right now. Wait a minute and try again.',
  search_quota_exceeded: 'The search provider quota is used up. Check your Tavily plan.',
  search_auth_failed: 'The search provider rejected the API key. Check TAVILY_API_KEY on the server.',
  internal_error: 'The server hit an unexpected error. Check the backend logs.',
  interrupted: 'The server restarted while this research was running.',
  too_many_active_jobs: 'Several research jobs are already running. Try again when one finishes.',
  csrf_check_failed: 'The request was refused for security reasons. Reload the page and try again.',
}

function messageFor(error, status) {
  if (error?.code === 'validation_error' && Array.isArray(error.details)) {
    return error.details.map((d) => `${d.loc?.at(-1) ?? 'input'}: ${d.msg}`).join('; ')
  }
  // Rate-limit messages from the server already say how long to wait.
  if (error?.code === 'rate_limited' && error.message) return error.message
  return FRIENDLY_MESSAGES[error?.code] || error?.message || `Request failed (HTTP ${status})`
}

async function request(path, { method = 'GET', body, signal, quietAuth = false } = {}) {
  let response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      signal,
      credentials: 'include',
      headers: body ? { ...CSRF_HEADERS, 'Content-Type': 'application/json' } : CSRF_HEADERS,
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch (error) {
    if (error.name === 'AbortError') throw error
    throw new ApiError(`Can't reach the research API${API_BASE_URL ? ` at ${API_BASE_URL}` : ''}. Is the backend running?`, {
      code: 'network_error',
    })
  }
  const data = response.status === 204 ? null : await response.json().catch(() => null)
  if (!response.ok) {
    const error = data?.error
    if (response.status === 401 && !quietAuth) window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT))
    throw new ApiError(messageFor(error, response.status), {
      status: response.status,
      code: error?.code || `http_${response.status}`,
      details: error?.details,
    })
  }
  return data
}

export const api = {
  health: (options) => request('/api/health', options),

  // Accounts
  me: (options) => request('/api/auth/me', { ...options, quietAuth: true }),
  login: (email, password) => request('/api/auth/login', { method: 'POST', body: { email, password }, quietAuth: true }),
  logout: () => request('/api/auth/logout', { method: 'POST', quietAuth: true }),
  listUsers: () => request('/api/admin/users'),
  createUser: (user) => request('/api/admin/users', { method: 'POST', body: user }),
  updateUser: (userId, changes) => request(`/api/admin/users/${encodeURIComponent(userId)}`, { method: 'PATCH', body: changes }),

  // Research
  createResearch: (question, depth) => request('/api/research', { method: 'POST', body: { question, depth } }),
  listResearch: ({ limit = 20, offset = 0, status } = {}, options) => {
    const params = new URLSearchParams({ limit, offset })
    if (status) params.set('status', status)
    return request(`/api/research?${params}`, options)
  },
  getResearch: (jobId, options) => request(`/api/research/${encodeURIComponent(jobId)}`, options),
  getReport: (jobId, options) => request(`/api/research/${encodeURIComponent(jobId)}/report`, options),
  getSources: (jobId, options) => request(`/api/research/${encodeURIComponent(jobId)}/sources`, options),
  cancelResearch: (jobId) => request(`/api/research/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
  downloadUrl: (reportId, format = 'markdown') =>
    `${API_BASE_URL}/api/reports/${encodeURIComponent(reportId)}/download?format=${format}`,
}

// SSE event names sent by the backend (see backend/app/api/events.py).
export const EVENT_KINDS = ['queued', 'started', 'progress', 'warning', 'completed', 'failed', 'cancelled']
export const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])

/**
 * Follow a job's progress over Server-Sent Events. Each connection starts with the job's
 * current state (onState), then replays stored events and streams new ones (onEvent), so
 * the log is always rebuilt from the server, not from browser memory. Each event gets a
 * `kind` ("progress", "warning", ...) without the "research_" prefix.
 * Returns a function that closes the connection.
 */
export function subscribeToEvents(jobId, { onState, onEvent, onEnd, onConnectionError }) {
  // The session cookie authenticates the stream (EventSource can't send custom headers).
  const source = new EventSource(`${API_BASE_URL}/api/research/${encodeURIComponent(jobId)}/events`, {
    withCredentials: true,
  })
  source.addEventListener('research_state', (message) => onState?.(JSON.parse(message.data)))
  for (const kind of EVENT_KINDS) {
    source.addEventListener(`research_${kind}`, (message) => onEvent({ ...JSON.parse(message.data), kind }))
  }
  source.addEventListener('end', (message) => {
    source.close()
    onEnd?.(JSON.parse(message.data))
  })
  // EventSource reconnects by itself (resuming from the last event ID); report only when it gives up.
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) onConnectionError?.()
  }
  return () => source.close()
}
