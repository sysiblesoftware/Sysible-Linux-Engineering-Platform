import React, { useEffect, useState } from 'react'
import { api, canWrite } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

// Where a finished run goes besides the console. A nightly schedule that starts
// failing keeps failing until a human happens to look, which is the gap this
// closes.
//
// The Test button is not a nicety: a notification nobody has ever sent is one
// you find out about during the incident it was supposed to tell you about.

export default function Notifications() {
  const [rows, setRows] = useState([])
  const [edit, setEdit] = useState(null)
  const [testing, setTesting] = useState(0)
  const err = useErr()
  const load = () => api('notifications').then((d) => setRows(d.notifications || []))
    .catch((e) => err.setErr(e.message))
  useEffect(() => { load() }, [])
  const writable = canWrite()

  const test = (n) => err.wrap(async () => {
    setTesting(n.id)
    try {
      const r = await api(`notifications/${n.id}/test`, { method: 'POST' })
      if (!r.ok) err.setErr(`${n.name}: ${r.detail}`)
      load()
    } finally { setTesting(0) }
  })
  const toggle = (n) => err.wrap(async () => {
    await api(`notifications/${n.id}`, { method: 'PATCH', json: { enabled: !n.enabled } })
    load()
  })
  const remove = (n) => err.wrap(async () => {
    if (!confirm(`Delete “${n.name}”?`)) return
    await api(`notifications/${n.id}`, { method: 'DELETE' })
    load()
  })

  return (
    <>
      <h2>Notifications</h2>
      <div className="muted" style={{ marginBottom: 10 }}>
        Tell somebody when a run finishes. A rule with no project covers every project.
      </div>
      {err.node}
      {writable && <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={() => setEdit({})}>+ New notification</button>
      </div>}

      {rows.length === 0 ? <div className="muted">Nothing is being notified.</div> : (
        <table>
          <thead><tr>
            <th>Name</th><th>Where</th><th>Sends on</th><th>Scope</th><th>Last</th><th></th>
          </tr></thead>
          <tbody>
            {rows.map((n) => (
              <tr key={n.id} style={{ opacity: n.enabled ? 1 : 0.55 }}>
                <td>{n.name}</td>
                <td className="muted mono" style={{ fontSize: 12 }}>
                  {n.kind === 'webhook' ? (n.config.url || '') : (n.config.to || []).join(', ')}
                </td>
                <td className="muted" style={{ fontSize: 12 }}>
                  {[n.on_success && 'success', n.on_failure && 'failure'].filter(Boolean).join(' + ') || '—'}
                </td>
                <td className="muted" style={{ fontSize: 12 }}>{n.project_id ? 'one project' : 'all projects'}</td>
                <td>
                  {n.last_status
                    ? <span className={'pill ' + (n.last_status === 'ok' ? 'ok' : 'failed')}
                        title={n.last_detail}>{n.last_status}</span>
                    : <span className="muted">never sent</span>}
                </td>
                <td className="row">
                  {writable && <button className="ghost sm" disabled={testing === n.id}
                    onClick={() => test(n)}>{testing === n.id ? 'Sending…' : 'Test'}</button>}
                  {writable && <button className="ghost sm" onClick={() => toggle(n)}>
                    {n.enabled ? 'Disable' : 'Enable'}</button>}
                  {writable && <button className="ghost sm" onClick={() => setEdit(n)}>Edit</button>}
                  {writable && <button className="danger ghost sm" onClick={() => remove(n)}>Delete</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {edit && <NotificationEditor n={edit} onClose={() => setEdit(null)}
        onDone={() => { setEdit(null); load() }} />}
    </>
  )
}

function NotificationEditor({ n, onClose, onDone }) {
  const isNew = !n.id
  const cfg = n.config || {}
  const [f, setF] = useState({
    name: n.name || '', kind: n.kind || 'webhook',
    project_id: n.project_id || '',
    on_success: n.on_success ?? false, on_failure: n.on_failure ?? true,
    url: cfg.url || '', secret: '',
    host: cfg.host || '', port: cfg.port || 587, to: (cfg.to || []).join(', '),
    from: cfg.from || '', username: cfg.username || '', password: '',
    starttls: cfg.starttls ?? true,
  })
  const [projects, setProjects] = useState([])
  const [busy, setBusy] = useState(false)
  const err = useErr()
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }))
  useEffect(() => { api('projects').then((d) => setProjects(d.projects || [])).catch(() => {}) }, [])

  const save = () => err.wrap(async () => {
    setBusy(true)
    try {
      const config = f.kind === 'webhook'
        ? { url: f.url, ...(f.secret ? { secret: f.secret } : {}) }
        : {
          host: f.host, port: Number(f.port), to: f.to, from: f.from,
          username: f.username, starttls: f.starttls,
          ...(f.password ? { password: f.password } : {}),
        }
      const body = {
        name: f.name, kind: f.kind, config,
        on_success: f.on_success, on_failure: f.on_failure,
        project_id: f.project_id ? Number(f.project_id) : null,
      }
      if (isNew) await api('notifications', { method: 'POST', json: body })
      else await api(`notifications/${n.id}`, { method: 'PATCH', json: body })
      onDone()
    } finally { setBusy(false) }
  })

  return (
    <Modal title={isNew ? 'New notification' : `Edit “${n.name}”`} onClose={onClose}>
      {err.node}
      <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
        <Field label="Name"><input value={f.name} onChange={(e) => set('name', e.target.value)}
          placeholder="e.g. #ops on Slack" /></Field>
        <Field label="Kind">
          <select value={f.kind} onChange={(e) => set('kind', e.target.value)}>
            <option value="webhook">Webhook (Slack, Teams, PagerDuty…)</option>
            <option value="email">Email</option>
          </select>
        </Field>
        <Field label="Project">
          <select value={f.project_id} onChange={(e) => set('project_id', e.target.value)}>
            <option value="">All projects</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </Field>
      </div>

      {f.kind === 'webhook' ? (
        <>
          <Field label="URL (http or https)"><input className="mono" value={f.url}
            onChange={(e) => set('url', e.target.value)} placeholder="https://hooks.slack.com/services/…" /></Field>
          <Field label={n.id ? 'Signing secret (leave blank to keep the current one)' : 'Signing secret (optional)'}>
            <input type="password" value={f.secret} onChange={(e) => set('secret', e.target.value)} />
          </Field>
          <div className="muted" style={{ fontSize: 11 }}>
            With a secret, each POST carries an <span className="mono">X-Sysible-Signature</span>
            {' '}HMAC so the receiver can tell a real notification from anyone who learned the URL.
          </div>
        </>
      ) : (
        <>
          <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
            <Field label="SMTP host"><input value={f.host} onChange={(e) => set('host', e.target.value)} /></Field>
            <Field label="Port"><input type="number" value={f.port} onChange={(e) => set('port', e.target.value)} /></Field>
            <label className="row" style={{ gap: 6, alignItems: 'center' }}>
              <input type="checkbox" checked={f.starttls} onChange={(e) => set('starttls', e.target.checked)} />
              STARTTLS
            </label>
          </div>
          <Field label="To (comma separated)"><input value={f.to} onChange={(e) => set('to', e.target.value)} /></Field>
          <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
            <Field label="From"><input value={f.from} onChange={(e) => set('from', e.target.value)}
              placeholder="slep@your.domain" /></Field>
            <Field label="Username"><input value={f.username} onChange={(e) => set('username', e.target.value)} /></Field>
            <Field label={n.id ? 'Password (blank keeps the current one)' : 'Password'}>
              <input type="password" value={f.password} onChange={(e) => set('password', e.target.value)} />
            </Field>
          </div>
        </>
      )}

      <div className="row" style={{ gap: 14, marginTop: 8 }}>
        <label className="row" style={{ gap: 6, alignItems: 'center' }}>
          <input type="checkbox" checked={f.on_failure} onChange={(e) => set('on_failure', e.target.checked)} />
          When a run fails
        </label>
        <label className="row" style={{ gap: 6, alignItems: 'center' }}>
          <input type="checkbox" checked={f.on_success} onChange={(e) => set('on_success', e.target.checked)} />
          When a run succeeds
        </label>
      </div>

      <div className="row" style={{ marginTop: 14, gap: 8 }}>
        <button className="primary" disabled={busy} onClick={save}>{busy ? 'Saving…' : 'Save'}</button>
        <button className="ghost" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  )
}
