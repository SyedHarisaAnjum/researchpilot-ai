import { useEffect, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { api } from '../services/api'
import { formatDateTime } from '../utils/format'

const EMPTY = { email: '', name: '', password: '', is_admin: false }

/** Admins create accounts and switch them on or off. */
export default function UsersPage() {
  const { user: me } = useAuth()
  const [users, setUsers] = useState(null)
  const [form, setForm] = useState(EMPTY)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [refreshCount, setRefreshCount] = useState(0)

  useEffect(() => {
    let active = true
    api
      .listUsers()
      .then((data) => active && setUsers(data))
      .catch((err) => active && setError(err.message))
    return () => {
      active = false
    }
  }, [refreshCount])

  async function handleCreate(event) {
    event.preventDefault()
    setError(null)
    try {
      const created = await api.createUser(form)
      setForm(EMPTY)
      setNotice(`Created ${created.email}. Share the password with them securely.`)
      setRefreshCount((n) => n + 1)
    } catch (err) {
      setError(err.message)
    }
  }

  async function toggle(user, changes) {
    setError(null)
    try {
      await api.updateUser(user.id, changes)
      setRefreshCount((n) => n + 1)
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="page">
      <section>
        <h1>Users</h1>
        <p className="muted">Each user sees only their own research. Admins can see everything and manage accounts.</p>
      </section>

      <form className="card user-form" onSubmit={handleCreate}>
        <h2 className="card__title">Add a user</h2>
        <div className="user-form__grid">
          <label className="field">
            <span className="field__label">Email</span>
            <input className="input" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
          </label>
          <label className="field">
            <span className="field__label">Name</span>
            <input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </label>
          <label className="field">
            <span className="field__label">Temporary password</span>
            <input
              className="input"
              type="password"
              autoComplete="new-password"
              minLength={10}
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              required
            />
          </label>
        </div>
        <label className="row gap-sm small">
          <input type="checkbox" checked={form.is_admin} onChange={(e) => setForm({ ...form, is_admin: e.target.checked })} />
          Admin
        </label>
        {error && <div className="alert alert--error" role="alert">{error}</div>}
        {notice && <div className="alert alert--neutral">{notice}</div>}
        <div>
          <button type="submit" className="button button--primary">Add user</button>
        </div>
      </form>

      <section className="card">
        {users === null && !error && <p className="muted">Loading…</p>}
        {users && (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Email</th>
                  <th>Name</th>
                  <th>Role</th>
                  <th>Last sign-in</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.id}>
                    <td>{u.email}</td>
                    <td>{u.name || '—'}</td>
                    <td>{u.is_admin ? 'Admin' : 'User'}</td>
                    <td className="muted">{u.last_login_at ? formatDateTime(u.last_login_at) : 'Never'}</td>
                    <td>
                      {u.id === me.id ? (
                        <span className="muted small">You</span>
                      ) : (
                        <button type="button" className="button button--ghost button--small" onClick={() => toggle(u, { is_active: !u.is_active })}>
                          {u.is_active ? 'Deactivate' : 'Reactivate'}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
