import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { fetchMe, getToken, loginUser, registerUser, setToken } from '../api'

const AuthCtx = createContext(null)
export const useAuth = () => useContext(AuthCtx)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  const refreshMe = useCallback(async () => {
    if (!getToken()) {
      setUser(null)
      setLoading(false)
      return null
    }
    try {
      const u = await fetchMe()
      setUser(u)
      return u
    } catch {
      setUser(null)
      return null
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshMe()
  }, [refreshMe])

  // api.js fires this when any request gets a 401 (expired/invalid token).
  useEffect(() => {
    const onLogout = () => setUser(null)
    window.addEventListener('auth:logout', onLogout)
    return () => window.removeEventListener('auth:logout', onLogout)
  }, [])

  const login = useCallback(async (email, password) => {
    const res = await loginUser(email, password)
    setToken(res.access_token)
    setUser(res.user)
    return res.user
  }, [])

  const register = useCallback(
    (email, password, name) => registerUser(email, password, name), [])

  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
  }, [])

  return (
    <AuthCtx.Provider value={{ user, setUser, loading, login, register, logout, refreshMe }}>
      {children}
    </AuthCtx.Provider>
  )
}
