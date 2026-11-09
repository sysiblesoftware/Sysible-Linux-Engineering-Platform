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

// Theme. The decision lives in ONE place — the pre-paint script in index.html,
// which owns the "sysible-theme" key every Sysible console reads and the
// migration off the old per-app keys. These are thin wrappers over it, NOT a
// second source of truth: reading localStorage here with its own key is exactly
// how SLEP came to disagree with SLOP and the Controller about what the theme
// was. The fallbacks only matter if the script somehow did not run.
export const getTheme = () => {
  try {
    const t = window.sysibleTheme?.get()
    if (t === 'light' || t === 'dark') return t
  } catch { /* no-op */ }
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark'
}
export function applyTheme(t) {
  try {
    window.sysibleTheme.set(t)
  } catch {
    document.documentElement.setAttribute('data-theme', t)
  }
}

// A session that expires at the GATEWAY does not come back as a 401.
//
// SLOP's gateway answers an unauthenticated request — XHR included — with
// `redir * /login?next={uri} 302`; fetch FOLLOWS that, and the IdP serves its
// sign-in page as 200 text/html. r.ok is true and the status is 200, so this
// used to hand the login page's HTML back as the API result. App.jsx assigned
// it to `me`, so me.username and me.role were undefined and it still called
// setAuthed(true): the console rendered with its placeholders — "admin" /
// "operator" — for a session that no longer existed, and every view that read a
// key off its own response crashed the React root to a white page. The run-log
// pane printed the sign-in page's raw HTML. SLOP's session TTL is 12 hours, so
// this was what a tab left open overnight did.
//
// Two signals, either one is enough: the response was redirected away from the
// URL we asked for, or it is HTML where this client only ever expects JSON or
// plain text. Every /api/* path is proxied to the backend, so HTML on one of
// them never comes from SLEP itself.
const _isSignInPage = (r, ct) => r.redirected || ct.includes('html')

function _sessionGone({ reload }) {
  setToken('')
  window.dispatchEvent(new Event('slep-logout'))
  // A top-level navigation is what the gateway will redirect properly: reload,
  // and the browser lands on the platform sign-in instead of a dead console.
  if (reload) { try { window.location.reload() } catch { /* non-browser host */ } }
  return new Error('Session expired — sign in again.')
}

export async function api(path, { method = 'GET', json, headers = {} } = {}) {
  const h = { ...headers }
  if (token) h.Authorization = 'Bearer ' + token
  let body
  if (json !== undefined) { h['Content-Type'] = 'application/json'; body = JSON.stringify(json) }
  const r = await fetch(apiUrl('/api/' + path), { method, headers: h, body })
  const ct = r.headers.get('content-type') || ''
  if (r.status === 401) throw _sessionGone({ reload: false })
  if (_isSignInPage(r, ct)) throw _sessionGone({ reload: true })
  const data = ct.includes('json') ? await r.json() : await r.text()
  if (!r.ok) throw new Error((data && data.detail) || ('HTTP ' + r.status))
  return data
}

// Log tailing: returns the chunk plus the next offset and run status headers.
// Same expiry check — without it the pane rendered the sign-in page's HTML into
// the run log and kept polling it for ever.
export async function tail(path) {
  const r = await fetch(apiUrl('/api/' + path), { headers: token ? { Authorization: 'Bearer ' + token } : {} })
  const ct = r.headers.get('content-type') || ''
  if (r.status === 401) throw _sessionGone({ reload: false })
  if (_isSignInPage(r, ct)) throw _sessionGone({ reload: true })
  return { text: await r.text(), next: Number(r.headers.get('X-Log-Next') || 0), status: r.headers.get('X-Run-Status') }
}
