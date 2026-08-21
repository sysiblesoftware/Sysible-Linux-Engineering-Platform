import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

// Tamper-evident activity feed (superuser): who did what, newest first. Backed by
// the hash-chained audit log; a "verify" button re-checks the chain end-to-end.
const LABEL = {
  login: 'signed in', login_failed: 'failed sign-in', login_throttled: 'sign-in throttled',
  setup: 'created first admin', user_created: 'created user', user_deleted: 'deleted user',
  user_role_changed: 'changed a role', user_password_reset: 'reset a password',
  run_launched: 'launched a run', schedule_created: 'created a schedule',
  schedule_deleted: 'deleted a schedule', schedule_fired: 'schedule fired',
}
const isAuth = (e) => e.event.startsWith('login')

export default function Activity() {
  const [entries, setEntries] = useState([])
  const [q, setQ] = useState('')
  const [hideAuto, setHideAuto] = useState(false)
  const [verify, setVerify] = useState(null)

  const load = () => api('audit?limit=200').then((d) => setEntries(d.entries))
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [])

  const needle = q.trim().toLowerCase()
  const rows = entries.filter((e) => (!hideAuto || e.username !== 'system')
    && (!needle || (e.username || '').toLowerCase().includes(needle)
      || (e.event || '').includes(needle) || (e.detail || '').toLowerCase().includes(needle)))

  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>Activity</h2>
        <div className="spacer" />
        <button className="ghost sm" onClick={async () => setVerify(await api('audit/verify'))}>Verify chain</button>
      </div>
      {verify && (
        <div className={verify.ok ? 'muted' : 'err'} style={{ marginBottom: 8 }}>
          {verify.ok ? `✓ Audit chain intact — ${verify.entries} entries verified.`
            : `✗ Audit chain broken at entry #${verify.broken_at}.`}
        </div>
      )}
      <div className="row" style={{ marginBottom: 12, gap: 10 }}>
        <input placeholder="Filter by user, action, detail…" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 320 }} />
        <label className="row" style={{ gap: 6, fontSize: 13, cursor: 'pointer' }}>
          <input type="checkbox" checked={hideAuto} onChange={(e) => setHideAuto(e.target.checked)} style={{ width: 'auto' }} /> Hide automation
        </label>
      </div>
      {rows.length === 0 ? <div className="muted">No activity yet.</div> : (
        <table>
          <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Detail</th></tr></thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.id}>
                <td className="muted" style={{ whiteSpace: 'nowrap' }}>{new Date(e.ts * 1000).toLocaleString()}</td>
                <td>{e.username === 'system' ? <span className="muted">system</span> : e.username || '—'}</td>
                <td><span className={'pill ' + (isAuth(e) && e.event !== 'login' ? 'failed' : 'ok')}>{LABEL[e.event] || e.event}</span></td>
                <td className="muted mono" style={{ fontSize: 12 }}>{e.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
