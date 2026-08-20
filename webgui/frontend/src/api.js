// Thin API client for the SLEP console. Talks to the BFF at /api/*, carrying the
// bearer token the backend issued at login. On 401 it clears the token and fires
// a 'slep-logout' event so the app drops back to the sign-in screen.
let token = localStorage.getItem('slep_token') || ''

export const getToken = () => token
export function setToken(t) {
  token = t || ''
  if (t) localStorage.setItem('slep_token', t)
  else localStorage.removeItem('slep_token')
}

export async function api(path, { method = 'GET', json, headers = {} } = {}) {
  const h = { ...headers }
  if (token) h.Authorization = 'Bearer ' + token
  let body
  if (json !== undefined) { h['Content-Type'] = 'application/json'; body = JSON.stringify(json) }
  const r = await fetch('/api/' + path, { method, headers: h, body })
  if (r.status === 401) { setToken(''); window.dispatchEvent(new Event('slep-logout')); throw new Error('Session expired — sign in again.') }
  const ct = r.headers.get('content-type') || ''
  const data = ct.includes('json') ? await r.json() : await r.text()
  if (!r.ok) throw new Error((data && data.detail) || ('HTTP ' + r.status))
  return data
}

// Log tailing: returns the chunk plus the next offset and run status headers.
export async function tail(path) {
  const r = await fetch('/api/' + path, { headers: token ? { Authorization: 'Bearer ' + token } : {} })
  return { text: await r.text(), next: Number(r.headers.get('X-Log-Next') || 0), status: r.headers.get('X-Run-Status') }
}
