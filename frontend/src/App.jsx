import { useEffect, useState } from 'react'
import { BrowserRouter, Link, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth/AuthContext'
import Dashboard from './pages/Dashboard'
import LoginPage from './pages/LoginPage'
import ReportPage from './pages/ReportPage'
import UsersPage from './pages/UsersPage'
import { api } from './services/api'

function ApiStatus() {
  const [online, setOnline] = useState(null)

  useEffect(() => {
    const controller = new AbortController()
    api
      .health({ signal: controller.signal })
      .then(() => setOnline(true))
      .catch((err) => err.name !== 'AbortError' && setOnline(false))
    return () => controller.abort()
  }, [])

  if (online !== false) return null
  return (
    <div className="banner" role="alert">
      Can't reach the research API. If you're running it locally, start the backend with{' '}
      <code>uvicorn app.main:app --reload --port 8000</code> from the <code>backend</code> folder, then reload.
    </div>
  )
}

function NotFound() {
  return (
    <div className="page">
      <h1>Page not found</h1>
      <Link to="/">Back to the dashboard</Link>
    </div>
  )
}

function Shell() {
  const { user, logout } = useAuth()

  return (
    <>
      <header className="topbar">
        <Link to="/" className="brand">
          <span className="brand__mark" aria-hidden="true">◆</span>
          ResearchLab
        </Link>
        {user && (
          <nav className="topbar__nav">
            <Link to="/">Dashboard</Link>
            {user.is_admin && <Link to="/admin/users">Users</Link>}
            <span className="topbar__user muted small">{user.name || user.email}</span>
            <button type="button" className="button button--ghost button--small" onClick={logout}>
              Sign out
            </button>
          </nav>
        )}
      </header>
      <ApiStatus />
      <main className="main">
        {user === undefined && <p className="page muted">Loading…</p>}
        {user === null && <LoginPage />}
        {user && (
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/research/:jobId" element={<ReportPage />} />
            {user.is_admin && <Route path="/admin/users" element={<UsersPage />} />}
            <Route path="*" element={<NotFound />} />
          </Routes>
        )}
      </main>
    </>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Shell />
      </AuthProvider>
    </BrowserRouter>
  )
}
