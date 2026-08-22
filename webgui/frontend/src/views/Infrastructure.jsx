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
  // Cadence steps 2 & 3: scaffold a starter Ansible playbook / Salt state into the
  // infra project and open it in the IDE, so Create flows into Configure → Maintain.
  const scaffold = async (r, stage) => {
    try {
      const d = await api(`infra/${r.project_id}/scaffold`, { method: 'POST', json: { stage } })
      onOpenProject({ id: r.project_id, name: r.project_name, slug: r.project_slug, openFile: d.path })
    } catch (e) { alert(e.message) }
  }

  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>Infrastructure</h2>
        <div className="spacer" />
        {writable && <button className="primary" onClick={() => setOpen(true)}>+ Create infrastructure</button>}
      </div>
      <div className="muted" style={{ marginBottom: 10 }}>Build VMs with Terraform/OpenTofu from a form — pick a provider and options, apply, then auto-enroll the new machines into a connected Controller.</div>
      <CadenceBar />
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
                  {writable && <button className="ghost sm" title="Terraform/OpenTofu plan" onClick={() => run(r, 'plan')}>Plan</button>}
                  {writable && <button className="ghost sm" title="Terraform/OpenTofu apply — create the VMs" onClick={() => run(r, 'apply')}>Apply</button>}
                  {writable && <button className="danger ghost sm" onClick={() => run(r, 'destroy')}>Destroy</button>}
                  {writable && r.controller_id ? <button className="primary sm" onClick={() => enroll(r)}>Enroll →</button> : null}
                  {writable && <button className="ghost sm" title="Scaffold an Ansible playbook and open it (Configure)" onClick={() => scaffold(r, 'configure')}>Configure</button>}
                  {writable && <button className="ghost sm" title="Scaffold a Salt state and open it (Maintain)" onClick={() => scaffold(r, 'maintain')}>Maintain</button>}
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

// The Sysible lifecycle cadence: create the machines (Terraform/OpenTofu), then
// configure them (Ansible), then keep them in a known state over time (Salt).
// Shown as a guide so the recommended flow is obvious from the Infrastructure page.
function CadenceBar() {
  const steps = [
    ['1', 'Create', 'Terraform / OpenTofu', 'Build the VMs on a hypervisor or cloud', true],
    ['2', 'Configure', 'Ansible', 'Install & set up software on the new hosts'],
    ['3', 'Maintain', 'Salt', 'Keep hosts in a known state over time'],
  ]
  return (
    <div className="cadence">
      {steps.map(([n, title, tool, sub, active], i) => (
        <React.Fragment key={n}>
          <div className={'cad-step' + (active ? ' active' : '')}>
            <span className="cad-n">{n}</span>
            <span className="cad-body">
              <span className="cad-t">{title} <span className="cad-tool">· {tool}</span></span>
              <span className="cad-s">{sub}</span>
            </span>
          </div>
          {i < steps.length - 1 && <span className="cad-arrow" aria-hidden="true">→</span>}
        </React.Fragment>
      ))}
    </div>
  )
}

function CreateWizard({ onClose, onDone }) {
  const [schema, setSchema] = useState(null)
  const [controllers, setControllers] = useState([])
  const [name, setName] = useState('')
  const [provider, setProvider] = useState('')
  const [values, setValues] = useState({})
  const [controllerId, setControllerId] = useState('')
  const [hvTest, setHvTest] = useState(null)   // {ok, output} | 'testing'
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

      {provider === 'libvirt' && (
        <div className="row" style={{ gap: 10, alignItems: 'center', margin: '2px 0 8px' }}>
          <button className="ghost sm" disabled={hvTest === 'testing' || !values.uri}
            onClick={() => wrap(async () => {
              setHvTest('testing')
              try { setHvTest(await api('infra/test-hypervisor', { method: 'POST', json: { uri: values.uri } })) }
              catch (e) { setHvTest({ ok: false, output: String(e.message || e) }) }
            })}>
            {hvTest === 'testing' ? 'Testing…' : '⚡ Test hypervisor connection'}
          </button>
          {hvTest && hvTest !== 'testing' && (
            <span style={{ fontSize: 12.5, color: hvTest.ok ? 'var(--green-bright)' : 'var(--danger)' }}>
              {hvTest.ok ? '✓ reachable' : '✗ failed'} — <span className="muted">{hvTest.output}</span>
            </span>
          )}
        </div>
      )}

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
