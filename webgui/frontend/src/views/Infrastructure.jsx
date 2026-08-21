import React, { useEffect, useState } from 'react'
import { api, canWrite } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

// Create Infrastructure — a form-driven Terraform VM builder. The wizard's menus
// come from the backend provider schema; on create it generates a Terraform
// project you can Plan/Apply, then one-click enroll the new VMs into a Controller.
export default function Infrastructure({ onOpenProject, onOpenRun }) {
  const [rows, setRows] = useState([])
  const [open, setOpen] = useState(false)
  const load = () => api('infra').then((d) => setRows(d.infra))
  useEffect(() => { load() }, [])
  const writable = canWrite()

  const run = async (r, target) => {
    const d = await api('runs', { method: 'POST', json: { project_id: r.project_id, kind: 'terraform', target } })
    onOpenRun(d.run_id)
  }
  const enroll = async (r) => {
    try {
      const d = await api(`infra/${r.project_id}/enroll`, { method: 'POST' })
      const lines = d.results.map((h) => `${h.ok ? '✓' : '✗'} ${h.name} ${h.ip} — ${h.detail}`).join('\n')
      alert(`Enrolled ${d.enrolled}/${d.total} into ${d.controller}:\n\n${lines}`)
    } catch (e) { alert(e.message) }
  }

  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>Infrastructure</h2>
        <div className="spacer" />
        {writable && <button className="primary" onClick={() => setOpen(true)}>+ Create infrastructure</button>}
      </div>
      <div className="muted" style={{ marginBottom: 12 }}>Build VMs with Terraform from a form — pick a provider and options, apply, then auto-enroll the new machines into a connected Controller.</div>
      {rows.length === 0 ? <div className="muted">No infrastructure yet. “Create infrastructure” to build some.</div> : (
        <table>
          <thead><tr><th>Name</th><th>Provider</th><th>Enroll target</th><th></th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.project_id}>
                <td><a onClick={() => onOpenProject({ id: r.project_id, name: r.project_name, slug: r.project_slug })}>{r.project_name}</a></td>
                <td className="muted">{r.provider}</td>
                <td className="muted">{r.controller_id ? `Controller #${r.controller_id}${r.environment ? ' · ' + r.environment : ''}` : '—'}</td>
                <td className="row">
                  {writable && <button className="ghost sm" onClick={() => run(r, 'plan')}>Plan</button>}
                  {writable && <button className="ghost sm" onClick={() => run(r, 'apply')}>Apply</button>}
                  {writable && <button className="danger ghost sm" onClick={() => run(r, 'destroy')}>Destroy</button>}
                  {writable && r.controller_id ? <button className="primary sm" onClick={() => enroll(r)}>Enroll →</button> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && <CreateWizard onClose={() => setOpen(false)}
        onDone={(pid, name, slug) => { setOpen(false); load(); onOpenProject({ id: pid, name, slug }) }} />}
    </>
  )
}

function CreateWizard({ onClose, onDone }) {
  const [schema, setSchema] = useState(null)
  const [controllers, setControllers] = useState([])
  const [name, setName] = useState('')
  const [provider, setProvider] = useState('')
  const [values, setValues] = useState({})
  const [controllerId, setControllerId] = useState('')
  const { wrap, node } = useErr()

  useEffect(() => { api('infra/providers').then((d) => {
    setSchema(d.providers)
    const first = Object.keys(d.providers)[0]
    setProvider(first); seed(d.providers, first)
  }) }, [])
  useEffect(() => { api('controllers').then((d) => setControllers(d.controllers)) }, [])

  const seed = (providers, p) => {
    const v = {}
    for (const o of providers[p].options) v[o.key] = o.default
    setValues(v)
  }
  const pickProvider = (p) => { setProvider(p); if (schema) seed(schema, p) }
  const set = (k, val) => setValues((s) => ({ ...s, [k]: val }))

  if (!schema) return <Modal title="Create infrastructure" onClose={onClose}><div className="muted">Loading…</div></Modal>
  const opts = schema[provider]?.options || []

  return (
    <Modal title="Create infrastructure" onClose={onClose} wide>
      <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="prod-web" autoFocus /></Field>
      <Field label="Provider">
        <select value={provider} onChange={(e) => pickProvider(e.target.value)}>
          {Object.entries(schema).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
        </select>
      </Field>
      <div className="faint" style={{ fontSize: 12, margin: '2px 0 8px' }}>{schema[provider]?.blurb}</div>

      <div className="task-palette" style={{ gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        {opts.map((o) => (
          <Field key={o.key} label={o.label}>
            {o.type === 'select'
              ? <select value={values[o.key] ?? ''} onChange={(e) => set(o.key, e.target.value)}>
                {o.choices.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              : o.type === 'textarea'
                ? <textarea rows={2} value={values[o.key] ?? ''} onChange={(e) => set(o.key, e.target.value)}
                  style={{ fontFamily: 'ui-monospace,monospace', fontSize: 12 }} />
                : <input type={o.type === 'number' ? 'number' : 'text'} value={values[o.key] ?? ''}
                  onChange={(e) => set(o.key, o.type === 'number' ? Number(e.target.value) : e.target.value)} />}
            {o.help && <div className="faint" style={{ fontSize: 11 }}>{o.help}</div>}
          </Field>
        ))}
      </div>

      <Field label="Auto-enroll new VMs into Controller (optional)">
        <select value={controllerId} onChange={(e) => setControllerId(e.target.value)}>
          <option value="">Don’t enroll</option>
          {controllers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </Field>
      <div className="faint" style={{ fontSize: 12 }}>If chosen, the Controller’s SSH key is baked into the VMs’ cloud-init so it can reach them, and “Enroll →” registers them after apply.</div>
      {node}
      <button className="primary" onClick={() => wrap(async () => {
        if (!name.trim()) throw new Error('Give it a name.')
        const d = await api('infra', { method: 'POST', json: {
          name, provider, options: values, controller_id: controllerId ? Number(controllerId) : null,
        } })
        onDone(d.project_id, name, d.slug)
      })}>Generate Terraform</button>
    </Modal>
  )
}
