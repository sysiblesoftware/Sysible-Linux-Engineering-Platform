// Thin API client for the SLEP console. Talks to the BFF at /api/*, carrying the
// bearer token the backend issued at login. On 401 it clears the token and fires
// a 'slep-logout' event so the app drops back to the sign-in screen.

// URL prefix the console is served under (see vite.config base). "" standalone,
// "/slep" behind the SLOP gateway, which path-routes /slep/* to this app on one
// shared origin. import.meta.env.BASE_URL carries the build-time base; every
// request is prefixed with it (apiUrl) so the SAME code works in both layouts.
export const BASE = (import.meta.env.BASE_URL || '/').replace(/\/+$/, '')
export const apiUrl = (p) => BASE + p

let token = localStorage.getItem('slep_token') || ''
let role = localStorage.getItem('slep_role') || ''

export const getToken = () => token
export function setToken(t) {
  token = t || ''
  if (t) localStorage.setItem('slep_token', t)
  else localStorage.removeItem('slep_token')
}

export const getRole = () => role
export function setRole(r) {
  role = r || ''
  if (r) localStorage.setItem('slep_role', r)
  else localStorage.removeItem('slep_role')
}
export const canWrite = () => role === 'operator' || role === 'superuser'
export const isSuperuser = () => role === 'superuser'

// Theme (dark default / light), persisted per browser and applied to <html>.
export const getTheme = () => localStorage.getItem('slep_theme') || 'dark'
export function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t)
  localStorage.setItem('slep_theme', t)
}

export async function api(path, { method = 'GET', json, headers = {} } = {}) {
  const h = { ...headers }
  if (token) h.Authorization = 'Bearer ' + token
  let body
  if (json !== undefined) { h['Content-Type'] = 'application/json'; body = JSON.stringify(json) }
  const r = await fetch(apiUrl('/api/' + path), { method, headers: h, body })
  if (r.status === 401) { setToken(''); window.dispatchEvent(new Event('slep-logout')); throw new Error('Session expired — sign in again.') }
  const ct = r.headers.get('content-type') || ''
  const data = ct.includes('json') ? await r.json() : await r.text()
  if (!r.ok) throw new Error((data && data.detail) || ('HTTP ' + r.status))
  return data
}

// Log tailing: returns the chunk plus the next offset and run status headers.
export async function tail(path) {
  const r = await fetch(apiUrl('/api/' + path), { headers: token ? { Authorization: 'Bearer ' + token } : {} })
  return { text: await r.text(), next: Number(r.headers.get('X-Log-Next') || 0), status: r.headers.get('X-Run-Status') }
}
