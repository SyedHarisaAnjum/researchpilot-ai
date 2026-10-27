import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { api, SESSION_EXPIRED_EVENT } from '../services/api'

const AuthContext = createContext(null)

/** Who is signed in. `user` is undefined while checking, null when signed out. */
export function AuthProvider({ children }) {
  const [user, setUser] = useState(undefined)
  const [error, setError] = useState(null)

  useEffect(() => {
    let active = true
    api
      .me()
      .then((me) => active && setUser(me))
      .catch((err) => {
        if (!active) return
        setUser(null)
        if (err.status !== 401) setError(err.message)
      })
    const onExpired = () => setUser(null)
    window.addEventListener(SESSION_EXPIRED_EVENT, onExpired)
    return () => {
      active = false
      window.removeEventListener(SESSION_EXPIRED_EVENT, onExpired)
    }
  }, [])

  const login = useCallback(async (email, password) => {
    const me = await api.login(email, password)
    setError(null)
    setUser(me)
    return me
  }, [])

  const logout = useCallback(async () => {
    await api.logout().catch(() => {})
    setUser(null)
  }, [])

  const value = useMemo(() => ({ user, error, login, logout }), [user, error, login, logout])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// eslint-disable-next-line react/only-export-components
export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>')
  return context
}
