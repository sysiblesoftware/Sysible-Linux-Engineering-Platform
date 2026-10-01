import React, { useEffect, useState } from 'react'
import { api, canWrite } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

// Job templates — a saved, named automation. The thing you hand to someone who
// cannot write a playbook: they see the survey, not the playbook path.
//
// `ask_*` is the line between a template and a saved form: the author names the
// few fields a launcher may override, and they default CLOSED. The editor says
// so in those words, because the alternative is an author ticking boxes without
// knowing they are widening what anyone with the Launch button can change.

const KINDS = [
  ['ansible', 'Ansible playbook'],
  ['terraform', 'Terraform / OpenTofu'],
  ['salt', 'Salt state'],
  ['molecule', 'Molecule scenario'],
]
const FIELD_KINDS = [
  ['text', 'Text'], ['textarea', 'Long text'], ['password', 'Password'],
  ['integer', 'Whole number'], ['float', 'Number'],
  ['choice', 'Choose one'], ['multiselect', 'Choose several'], ['boolean', 'Yes / no'],
]
const needsChoices = (k) => k === 'choice' || k === 'multiselect'
const targetLabel = (k) => k === 'ansible' ? 'Playbook path'
  : k === 'terraform' ? 'Action (plan / apply / destroy)'
    : k === 'salt' ? 'State name' : 'Scenario name'

export default function Templates({ onOpenRun }) {
  const [rows, setRows] = useState([])
  const [edit, setEdit] = useState(null)       // template object, or {} for new
  const [launch, setLaunch] = useState(null)
  const err = useErr()
  const load = () => api('templates').then((d) => setRows(d.templates || [])).catch((e) => err.setErr(e.message))
  useEffect(() => { load() }, [])
  const writable = canWrite()

  const remove = (t) => err.wrap(async () => {
    if (!confirm(`Delete the template “${t.name}”?\n\nSchedules and pipeline steps that point at it will stop working.`)) return
    await api(`templates/${t.id}`, { method: 'DELETE' })
    load()
  })

  return (
    <>
      <h2>Job Templates</h2>
      <div className="muted" style={{ marginBottom: 10 }}>
        A saved automation: what to run, where, as whom, and which questions to ask.
        Schedules and pipeline steps can point at one instead of carrying their own copy.
      </div>
      {err.node}
      {writable && <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={() => setEdit({})}>+ New template</button>
      </div>}

      {rows.length === 0 ? <div className="muted">No templates yet.</div> : (
        <table>
          <thead><tr>
            <th>Name</th><th>Project</th><th>Runs</th><th>Asks for</th><th>Launcher may set</th><th></th>
          </tr></thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.id}>
                <td>
                  <div>{t.name}</div>
                  {t.description && <div className="muted" style={{ fontSize: 12 }}>{t.description}</div>}
                </td>
                <td className="muted">{t.project_name}</td>
                <td className="muted mono" style={{ fontSize: 12 }}>{t.kind} · {t.target}</td>
                <td className="muted" style={{ fontSize: 12 }}>
                  {(t.survey || []).length ? `${t.survey.length} question${t.survey.length === 1 ? '' : 's'}` : '—'}
                </td>
                <td className="muted" style={{ fontSize: 12 }}>
                  {[['ask_inventory', 'inventory'], ['ask_credential', 'credential'],
                    ['ask_limit', 'limit'], ['ask_tags', 'tags']]
                    .filter(([k]) => t[k]).map(([, l]) => l).join(', ') || 'nothing'}
                </td>
                <td className="row">
                  {writable && <button className="primary sm" onClick={() => setLaunch(t)}>Launch</button>}
                  {writable && <button className="ghost sm" onClick={() => setEdit(t)}>Edit</button>}
                  {writable && <button className="danger ghost sm" onClick={() => remove(t)}>Delete</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {edit && <TemplateEditor t={edit} onClose={() => setEdit(null)}
        onDone={() => { setEdit(null); load() }} />}
      {launch && <LaunchTemplate t={launch} onClose={() => setLaunch(null)}
        onLaunched={(runId) => { setLaunch(null); if (onOpenRun) onOpenRun(runId) }} />}
    </>
  )
}

// ---------------------------------------------------------------- the editor
function TemplateEditor({ t, onClose, onDone }) {
  const isNew = !t.id
  const [f, setF] = useState({
    project_id: t.project_id || '', name: t.name || '', description: t.description || '',
    kind: t.kind || 'ansible', target: t.target || '',
    inventory_id: t.inventory_id || '', credential_id: t.credential_id || '',
    extra_vars: JSON.stringify(t.extra_vars || {}, null, 2),
    ask_inventory: !!t.ask_inventory, ask_credential: !!t.ask_credential,
    ask_limit: !!t.ask_limit, ask_tags: !!t.ask_tags,
  })
  const [opts, setOpts] = useState(t.job_opts || {})
  const [survey, setSurvey] = useState(t.survey || [])
  const [projects, setProjects] = useState([]); const [invs, setInvs] = useState([]); const [creds, setCreds] = useState([])
  const [busy, setBusy] = useState(false)
  const err = useErr()
  const set = (k, v) => setF((s) => ({ ...s, [k]: v }))

  useEffect(() => {
    api('projects').then((d) => setProjects(d.projects || [])).catch(() => {})
    api('inventories').then((d) => setInvs(d.inventories || [])).catch(() => {})
    api('credentials').then((d) => setCreds(d.credentials || [])).catch(() => {})
  }, [])

  const save = () => err.wrap(async () => {
    setBusy(true)
    try {
      let extra = {}
      if (f.extra_vars.trim()) {
        try { extra = JSON.parse(f.extra_vars) } catch { throw new Error('Variables must be valid JSON.') }
      }
      const body = {
        ...f, extra_vars: extra, job_opts: opts, survey,
        inventory_id: f.inventory_id || null, credential_id: f.credential_id || null,
      }
      if (isNew) await api('templates', { method: 'POST', json: { ...body, project_id: Number(f.project_id) } })
      else await api(`templates/${t.id}`, { method: 'PATCH', json: body })
      onDone()
    } finally { setBusy(false) }
  })

  return (
    <Modal title={isNew ? 'New job template' : `Edit “${t.name}”`} onClose={onClose} wide>
      {err.node}
      <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
        {isNew && <Field label="Project">
          <select value={f.project_id} onChange={(e) => set('project_id', e.target.value)}>
            <option value="">Choose…</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </Field>}
        <Field label="Name"><input value={f.name} onChange={(e) => set('name', e.target.value)}
          placeholder="e.g. Patch the web tier" /></Field>
        <Field label="Engine">
          <select value={f.kind} onChange={(e) => set('kind', e.target.value)}>
            {KINDS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
        </Field>
        <Field label={targetLabel(f.kind)}><input value={f.target} className="mono"
          onChange={(e) => set('target', e.target.value)} placeholder="site.yml" /></Field>
      </div>
      <Field label="Description (what this does, for whoever launches it)">
        <input value={f.description} onChange={(e) => set('description', e.target.value)} />
      </Field>

      <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
        <Field label="Inventory">
          <select value={f.inventory_id} onChange={(e) => set('inventory_id', e.target.value)}>
            <option value="">— none —</option>
            {invs.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
          </select>
        </Field>
        <Field label="Credential">
          <select value={f.credential_id} onChange={(e) => set('credential_id', e.target.value)}>
            <option value="">— none —</option>
            {creds.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </Field>
      </div>

      {f.kind === 'ansible' && <AnsibleOptions opts={opts} setOpts={setOpts} />}

      <Field label="Fixed variables (JSON) — the author's, always applied">
        <textarea rows={3} className="mono" value={f.extra_vars}
          onChange={(e) => set('extra_vars', e.target.value)} />
      </Field>

      <SurveyBuilder survey={survey} setSurvey={setSurvey} />

      <h3 style={{ margin: '14px 0 2px' }}>What the launcher may change</h3>
      <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
        Everything else is fixed by this template. Leave these off unless you mean
        whoever presses Launch to be able to point it somewhere else.
      </div>
      <div className="row" style={{ gap: 14, flexWrap: 'wrap' }}>
        {[['ask_inventory', 'Inventory'], ['ask_credential', 'Credential'],
          ['ask_limit', 'Limit (which hosts)'], ['ask_tags', 'Tags']].map(([k, l]) => (
          <label key={k} className="row" style={{ gap: 6, alignItems: 'center' }}>
            <input type="checkbox" checked={!!f[k]} onChange={(e) => set(k, e.target.checked)} />
            {l}
          </label>
        ))}
      </div>

      <div className="row" style={{ marginTop: 14, gap: 8 }}>
        <button className="primary" disabled={busy} onClick={save}>{busy ? 'Saving…' : 'Save template'}</button>
        <button className="ghost" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  )
}

function AnsibleOptions({ opts, setOpts }) {
  const set = (k, v) => setOpts((s) => ({ ...s, [k]: v }))
  return (
    <>
      <h3 style={{ margin: '14px 0 2px' }}>Run options</h3>
      <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
        <Field label="Tags"><input className="mono" value={opts.tags || ''}
          onChange={(e) => set('tags', e.target.value)} placeholder="web,db" /></Field>
        <Field label="Skip tags"><input className="mono" value={opts.skip_tags || ''}
          onChange={(e) => set('skip_tags', e.target.value)} /></Field>
        <Field label="Limit"><input className="mono" value={opts.limit || ''}
          onChange={(e) => set('limit', e.target.value)} /></Field>
        <Field label="Verbosity">
          <select value={opts.verbosity || 0} onChange={(e) => set('verbosity', Number(e.target.value))}>
            {[0, 1, 2, 3, 4].map((v) => <option key={v} value={v}>{v ? '-' + 'v'.repeat(v) : 'normal'}</option>)}
          </select>
        </Field>
      </div>
      <div className="row" style={{ gap: 14, flexWrap: 'wrap', marginTop: 6 }}>
        <label className="row" style={{ gap: 6, alignItems: 'center' }}>
          <input type="checkbox" checked={!!opts.check} onChange={(e) => set('check', e.target.checked)} />
          Check mode (change nothing)
        </label>
        <label className="row" style={{ gap: 6, alignItems: 'center' }}>
          <input type="checkbox" checked={!!opts.diff} onChange={(e) => set('diff', e.target.checked)} />
          Show diffs
        </label>
        <label className="row" style={{ gap: 6, alignItems: 'center' }}
          title="A handler fires at the end of a play. If the play fails first, the service is never restarted — and re-running does not notify again, because the task that notified is already in the desired state.">
          <input type="checkbox" checked={!!opts.force_handlers}
            onChange={(e) => set('force_handlers', e.target.checked)} />
          Run handlers even if the play fails
        </label>
        <label className="row" style={{ gap: 6, alignItems: 'center' }}
          title="Runs the playbook a second time and fails if anything changed. A playbook that changes something every run is doing work every run.">
          <input type="checkbox" checked={!!opts.idempotence}
            onChange={(e) => set('idempotence', e.target.checked)} />
          Check idempotence (run twice)
        </label>
      </div>
    </>
  )
}

// ------------------------------------------------------------ survey builder
function SurveyBuilder({ survey, setSurvey }) {
  const up = (i, patch) => setSurvey(survey.map((f, n) => (n === i ? { ...f, ...patch } : f)))
  const add = () => setSurvey([...survey, { var: '', kind: 'text', label: '', required: false }])
  const del = (i) => setSurvey(survey.filter((_, n) => n !== i))
  const move = (i, d) => {
    const j = i + d
    if (j < 0 || j >= survey.length) return
    const next = survey.slice()
    ;[next[i], next[j]] = [next[j], next[i]]
    setSurvey(next)
  }
  return (
    <>
      <h3 style={{ margin: '14px 0 2px' }}>Survey — what to ask when it is launched</h3>
      <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
        Each answer becomes a variable the playbook can use. A template with a survey
        is a form someone can fill in without reading the playbook.
        <br />A <b>required</b> question with no default cannot be scheduled — there
        is nobody to answer it at 02:00.
      </div>
      {survey.map((fl, i) => (
        <div key={i} className="card" style={{ padding: 10, marginBottom: 8 }}>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <Field label="Variable"><input className="mono" value={fl.var || ''}
              onChange={(e) => up(i, { var: e.target.value })} placeholder="app_version" /></Field>
            <Field label="Question"><input value={fl.label || ''}
              onChange={(e) => up(i, { label: e.target.value })} placeholder="Which version?" /></Field>
            <Field label="Type">
              <select value={fl.kind} onChange={(e) => up(i, { kind: e.target.value })}>
                {FIELD_KINDS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
              </select>
            </Field>
            {fl.kind !== 'password' && <Field label="Default"><input value={fl.default ?? ''}
              onChange={(e) => up(i, { default: e.target.value })} /></Field>}
            <label className="row" style={{ gap: 6, alignItems: 'center' }}>
              <input type="checkbox" checked={!!fl.required}
                onChange={(e) => up(i, { required: e.target.checked })} />Required
            </label>
            <div className="row" style={{ gap: 4 }}>
              <button className="ghost sm" onClick={() => move(i, -1)} disabled={i === 0}>↑</button>
              <button className="ghost sm" onClick={() => move(i, 1)} disabled={i === survey.length - 1}>↓</button>
              <button className="danger ghost sm" onClick={() => del(i)}>Remove</button>
            </div>
          </div>
          {needsChoices(fl.kind) && (
            <Field label="Choices (one per line)">
              <textarea rows={3} className="mono"
                value={Array.isArray(fl.choices) ? fl.choices.join('\n') : (fl.choices || '')}
                onChange={(e) => up(i, { choices: e.target.value.split('\n') })} />
            </Field>
          )}
        </div>
      ))}
      <button className="ghost sm" onClick={add}>+ Add a question</button>
    </>
  )
}

// ------------------------------------------------------------ launching one
function LaunchTemplate({ t, onClose, onLaunched }) {
  const [answers, setAnswers] = useState(() => {
    const a = {}
    for (const f of t.survey || []) if (f.default !== undefined) a[f.var] = f.default
    return a
  })
  const [over, setOver] = useState({})
  const [invs, setInvs] = useState([]); const [creds, setCreds] = useState([])
  const [busy, setBusy] = useState(false)
  const err = useErr()
  useEffect(() => {
    if (t.ask_inventory) api('inventories').then((d) => setInvs(d.inventories || [])).catch(() => {})
    if (t.ask_credential) api('credentials').then((d) => setCreds(d.credentials || [])).catch(() => {})
  }, [])

  const go = () => err.wrap(async () => {
    setBusy(true)
    try {
      const body = { answers }
      // Only the fields this template actually opened — sending one it did not is
      // refused by the API, and rightly: it would be running something other than
      // what the template says.
      for (const [k, flag] of [['inventory_id', 'ask_inventory'], ['credential_id', 'ask_credential'],
        ['limit', 'ask_limit'], ['tags', 'ask_tags']]) {
        if (t[flag] && over[k]) body[k] = k.endsWith('_id') ? Number(over[k]) : over[k]
      }
      const r = await api(`templates/${t.id}/launch`, { method: 'POST', json: body })
      onLaunched(r.run_id)
    } finally { setBusy(false) }
  })

  return (
    <Modal title={`Launch “${t.name}”`} onClose={onClose}>
      {t.description && <div className="muted" style={{ marginBottom: 8 }}>{t.description}</div>}
      {err.node}
      {(t.survey || []).map((f) => (
        <SurveyField key={f.var} f={f} value={answers[f.var]}
          onChange={(v) => setAnswers((a) => ({ ...a, [f.var]: v }))} />
      ))}
      {t.ask_inventory && <Field label="Inventory">
        <select value={over.inventory_id || ''} onChange={(e) => setOver((o) => ({ ...o, inventory_id: e.target.value }))}>
          <option value="">— as the template says —</option>
          {invs.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
        </select>
      </Field>}
      {t.ask_credential && <Field label="Credential">
        <select value={over.credential_id || ''} onChange={(e) => setOver((o) => ({ ...o, credential_id: e.target.value }))}>
          <option value="">— as the template says —</option>
          {creds.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </Field>}
      {t.ask_limit && <Field label="Limit to hosts"><input className="mono" value={over.limit || ''}
        onChange={(e) => setOver((o) => ({ ...o, limit: e.target.value }))} /></Field>}
      {t.ask_tags && <Field label="Tags"><input className="mono" value={over.tags || ''}
        onChange={(e) => setOver((o) => ({ ...o, tags: e.target.value }))} /></Field>}

      <div className="row" style={{ marginTop: 14, gap: 8 }}>
        <button className="primary" disabled={busy} onClick={go}>{busy ? 'Launching…' : 'Launch'}</button>
        <button className="ghost" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  )
}

function SurveyField({ f, value, onChange }) {
  const label = (f.label || f.var) + (f.required ? ' *' : '')
  if (f.kind === 'boolean') {
    return (
      <label className="row" style={{ gap: 6, alignItems: 'center', margin: '6px 0' }}>
        <input type="checkbox" checked={value === true || value === 'true'}
          onChange={(e) => onChange(e.target.checked)} />{label}
      </label>
    )
  }
  if (f.kind === 'choice') {
    return (
      <Field label={label}>
        <select value={value ?? ''} onChange={(e) => onChange(e.target.value)}>
          <option value="">— choose —</option>
          {(f.choices || []).map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </Field>
    )
  }
  if (f.kind === 'multiselect') {
    const sel = Array.isArray(value) ? value : []
    return (
      <Field label={label}>
        <div className="col" style={{ gap: 2 }}>
          {(f.choices || []).map((c) => (
            <label key={c} className="row" style={{ gap: 6, alignItems: 'center' }}>
              <input type="checkbox" checked={sel.includes(c)}
                onChange={(e) => onChange(e.target.checked ? [...sel, c] : sel.filter((x) => x !== c))} />{c}
            </label>
          ))}
        </div>
      </Field>
    )
  }
  const type = f.kind === 'password' ? 'password'
    : (f.kind === 'integer' || f.kind === 'float') ? 'number' : 'text'
  if (f.kind === 'textarea') {
    return <Field label={label}><textarea rows={3} value={value ?? ''}
      onChange={(e) => onChange(e.target.value)} /></Field>
  }
  return (
    <Field label={label}>
      <input type={type} value={value ?? ''} onChange={(e) => onChange(e.target.value)} />
      {f.help && <span className="muted" style={{ fontSize: 11 }}>{f.help}</span>}
    </Field>
  )
}
