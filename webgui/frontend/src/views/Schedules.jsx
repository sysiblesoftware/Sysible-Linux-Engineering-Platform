import React, { useEffect, useState } from 'react'
import { api, canWrite } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const when = (s) => s.cadence === 'hourly' ? `hourly at :${(s.at || '00:00').slice(3)}`
  : s.cadence === 'weekly' ? `${WEEKDAYS[s.weekday] || 'Mon'} ${s.at}`
    : `daily ${s.at}`

// Recurring runs — a saved run spec fired on a cadence by the backend scheduler.
export default function Schedules() {
  const [rows, setRows] = useState([])
  const [open, setOpen] = useState(false)
  const load = () => api('schedules').then((d) => setRows(d.schedules))
  useEffect(() => { load() }, [])
  const writable = canWrite()

  const toggle = async (s) => { await api(`schedules/${s.id}`, { method: 'PATCH', json: { enabled: s.enabled ? 0 : 1 } }); load() }
  const remove = async (s) => { if (confirm(`Delete schedule "${s.name || s.id}"?`)) { await api(`schedules/${s.id}`, { method: 'DELETE' }); load() } }

  return (
    <>
      <h2>Schedules</h2>
      <div className="muted" style={{ marginBottom: 10 }}>Run a playbook, plan, or state automatically on a cadence — the platform launches it for you and records each firing in Runs.</div>
      {writable && <div className="row" style={{ marginBottom: 12 }}><button className="primary" onClick={() => setOpen(true)}>+ New schedule</button></div>}
      {rows.length === 0 ? <div className="muted">No schedules yet.</div> : (
        <table>
          <thead><tr><th>Name</th><th>Engine</th><th>Target</th><th>When</th><th>Next run</th><th>Last</th><th></th></tr></thead>
          <tbody>
            {rows.map((s) => (
              <tr key={s.id} style={{ opacity: s.enabled ? 1 : 0.55 }}>
                <td>{s.name || `#${s.id}`}</td>
                <td>{s.kind}</td>
                <td className="muted mono" style={{ fontSize: 12 }}>{s.target}</td>
                <td className="muted">{when(s)}</td>
                <td className="muted">{s.enabled && s.next_run ? new Date(s.next_run * 1000).toLocaleString() : '—'}</td>
                <td>{s.last_status ? <span className={'pill ' + (s.last_status === 'error' ? 'failed' : 'ok')}>{s.last_status}</span> : <span className="muted">—</span>}</td>
                <td className="row">
                  {writable && <button className="ghost sm" onClick={() => toggle(s)}>{s.enabled ? 'Pause' : 'Resume'}</button>}
                  {writable && <button className="danger ghost sm" onClick={() => remove(s)}>Delete</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && <NewSchedule onClose={() => setOpen(false)} onDone={() => { setOpen(false); load() }} />}
    </>
  )
}

function NewSchedule({ onClose, onDone }) {
  const [projects, setProjects] = useState([]); const [invs, setInvs] = useState([]); const [creds, setCreds] = useState([])
  const [name, setName] = useState(''); const [pid, setPid] = useState('')
  const [engine, setEngine] = useState('ansible'); const [target, setTarget] = useState('site.yml')
  const [inv, setInv] = useState(''); const [cred, setCred] = useState('')
  const [cadence, setCadence] = useState('daily'); const [at, setAt] = useState('02:00'); const [weekday, setWeekday] = useState(0)
  const { wrap, node } = useErr()

  useEffect(() => { api('projects').then((d) => { setProjects(d.projects); if (d.projects[0]) setPid(String(d.projects[0].id)) }) }, [])
  useEffect(() => { api('inventories').then((d) => { setInvs(d.inventories); if (d.inventories[0]) setInv(String(d.inventories[0].id)) }) }, [])
  useEffect(() => { api('credentials').then((d) => setCreds(d.credentials)) }, [])
  useEffect(() => { if (engine === 'terraform') setTarget('plan'); else if (engine === 'salt') setTarget('highstate'); else setTarget('site.yml') }, [engine])
  const needsInv = engine !== 'terraform'

  return (
    <Modal title="New schedule" onClose={onClose}>
      <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="Nightly patch" autoFocus /></Field>
      <Field label="Project">
        <select value={pid} onChange={(e) => setPid(e.target.value)}>
          {projects.length === 0 && <option value="">(create a project first)</option>}
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </Field>
      <Field label="Engine">
        <select value={engine} onChange={(e) => setEngine(e.target.value)}>
          <option value="ansible">Ansible — playbook</option>
          <option value="terraform">Terraform — plan / apply / destroy</option>
          <option value="salt">Salt — state.apply</option>
        </select>
      </Field>
      <Field label={engine === 'ansible' ? 'Playbook path' : engine === 'terraform' ? 'Action' : 'State'}>
        {engine === 'terraform'
          ? <select value={target} onChange={(e) => setTarget(e.target.value)}><option>plan</option><option>apply</option><option>destroy</option></select>
          : <input value={target} onChange={(e) => setTarget(e.target.value)} />}
      </Field>
      {needsInv && (
        <Field label="Inventory">
          <select value={inv} onChange={(e) => setInv(e.target.value)}>
            {invs.length === 0 && <option value="">(create an inventory first)</option>}
            {invs.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
          </select>
        </Field>
      )}
      <Field label={engine === 'terraform' ? 'Cloud/env credential' : 'SSH credential'}>
        <select value={cred} onChange={(e) => setCred(e.target.value)}>
          <option value="">(none)</option>
          {creds.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.kind})</option>)}
        </select>
      </Field>
      <Field label="Cadence">
        <select value={cadence} onChange={(e) => setCadence(e.target.value)}>
          <option value="hourly">Hourly</option><option value="daily">Daily</option><option value="weekly">Weekly</option>
        </select>
      </Field>
      <div className="row" style={{ gap: 10 }}>
        {cadence === 'weekly' && (
          <Field label="Day"><select value={weekday} onChange={(e) => setWeekday(Number(e.target.value))}>
            {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
          </select></Field>
        )}
        <Field label={cadence === 'hourly' ? 'Minute (MM of HH:MM)' : 'Time (HH:MM, local)'}>
          <input value={at} onChange={(e) => setAt(e.target.value)} placeholder="02:00" style={{ width: 110 }} />
        </Field>
      </div>
      {node}
      <button className="primary" onClick={() => wrap(async () => {
        if (!pid) throw new Error('Pick a project (create one first).')
        if (needsInv && !inv) throw new Error('Pick an inventory (create one first).')
        await api('schedules', { method: 'POST', json: {
          name, project_id: Number(pid), kind: engine, target,
          inventory_id: needsInv ? Number(inv) : null, credential_id: cred ? Number(cred) : null,
          cadence, at, weekday,
        } })
        onDone()
      })}>Create schedule</button>
    </Modal>
  )
}
