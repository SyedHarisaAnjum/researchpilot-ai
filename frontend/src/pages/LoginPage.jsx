import { useState } from 'react'
import { useAuth } from '../auth/AuthContext'

export default function LoginPage() {
  const { login, error: startupError } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  async function handleSubmit(event) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await login(email, password)
    } catch (err) {
      setError(err.message)
      setSubmitting(false)
    }
  }

  return (
    <div className="page login-page">
      <form className="card login-card" onSubmit={handleSubmit}>
        <h1>Sign in</h1>
        <p className="muted">Accounts are created by your ResearchLab admin.</p>
        <label className="field">
          <span className="field__label">Email</span>
          <input
            className="input"
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoFocus
          />
        </label>
        <label className="field">
          <span className="field__label">Password</span>
          <input
            className="input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {(error || startupError) && (
          <div className="alert alert--error" role="alert">
            {error || startupError}
          </div>
        )}
        <button type="submit" className="button button--primary" disabled={submitting || !email || !password}>
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
