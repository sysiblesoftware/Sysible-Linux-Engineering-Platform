import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

// Infrastructure — a READ-ONLY status view of the VMs SLEP has built. Building and
// the lifecycle actions (apply/destroy/configure/maintain/cadence) live in Projects
// now (a project row's ⋯ menu, and on top of the IDE when the project is open); this
// pane just shows what exists and whether it's reachable. Click a row to open the
// project (where the actions are).
export default function Infrastructure({ onOpenProject }) {
  const [rows, setRows] = useState([])
  const [status, setStatus] = useState({})   // project_id → {loading|vms|error}
  useEffect(() => { api('infra').then((d) => setRows(d.infra || [])) }, [])

  // Best-effort live status per project: the domains on its hypervisor.
  useEffect(() => {
    let alive = true
    rows.forEach((r) => {
      setStatus((s) => (s[r.project_id] ? s : { ...s, [r.project_id]: { loading: true } }))
      api(`infra/${r.project_id}/vms`, { method: 'POST' })
        .then((d) => { if (alive) setStatus((s) => ({ ...s, [r.project_id]: { vms: d.vms || [], error: d.ok ? '' : d.output } })) })
        .catch((e) => { if (alive) setStatus((s) => ({ ...s, [r.project_id]: { error: e.message } })) })
    })
    return () => { alive = false }
  }, [rows])   // eslint-disable-line

  const vmsCell = (r) => {
    const st = status[r.project_id]
    if (!st || st.loading) return <span className="faint">checking…</span>
    if (st.error) return <span className="faint" title={st.error}>—</span>
    const list = st.vms || []
    if (!list.length) return <span className="faint">none yet</span>
    const running = list.filter((v) => /running/i.test(v.state)).length
    return <span title={list.map((v) => `${v.name}: ${v.state}`).join('\n')}>{running}/{list.length} running</span>
  }

  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>Infrastructure</h2>
      </div>
      <div className="muted" style={{ marginBottom: 12 }}>
        The machines SLEP has built, and their live status. Infrastructure lives inside a project:
        open a project and use <b>Build infra</b> to create it, then the lifecycle actions
        (apply, configure, maintain, enroll) appear on top of the IDE and on the project’s row&nbsp;⋯ menu.
      </div>
      {rows.length === 0 ? (
        <div className="muted">No infrastructure yet. Create a project in <b>Projects</b>, open it, and click <b>Build infra</b>.</div>
      ) : (
        <table>
          <thead><tr><th>Name</th><th>Provider</th><th>VMs on hypervisor</th><th>Jump host</th><th>Enroll target</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.project_id}>
                <td><a onClick={() => onOpenProject({ id: r.project_id, name: r.project_name, slug: r.project_slug })}>{r.project_name}</a></td>
                <td className="muted">{r.provider}</td>
                <td className="muted">{vmsCell(r)}</td>
                <td className="muted">{r.bastion || <span className="faint">direct</span>}</td>
                <td className="muted">{r.controller_id ? `Controller #${r.controller_id}${r.environment ? ' · ' + r.environment : ''}` : <span className="faint">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

// Project-level VM access settings: the login user the keys are installed on (kept
// consistent across cloud-init, the Terraform output, and the inventory), an
// optional login password sourced from a Vault variable (turns on password SSH so
// a VM is reachable even before its key lands), and the SSH jump host. All apply to
// every inventory the project owns, so you set them once instead of per inventory.
export function JumpHostEditor({ r, onClose, onSaved }) {
  const [bastion, setBastion] = useState(r.bastion || '')
  const [creds, setCreds] = useState([])
  // Login source: a stored credential id, or '__manual' (type user + password).
  const [src, setSrc] = useState(r.login_credential_id ? String(r.login_credential_id) : '__manual')
  const [sshUser, setSshUser] = useState(r.ssh_user || '')
  const [pw, setPw] = useState('')
  useEffect(() => {
    api('credentials').then((d) => {
      const list = (d.credentials || []).filter((c) => c.kind === 'ssh' || c.kind === 'ssh_password')
      setCreds(list)
      // Reconcile the dropdown: if the stored login_credential_id isn't in the list
      // (deleted, or not visible to this operator), the <select> would show the first
      // option while `src` still holds the phantom id — and Save would submit that
      // stale id and get "Credential not found." Snap `src` to a real option instead.
      setSrc((cur) => (cur === '__manual' || list.some((c) => String(c.id) === cur)
        ? cur : (list[0] ? String(list[0].id) : '__manual')))
    })
  }, [])
  const { wrap, node } = useErr()
  const picked = creds.find((c) => String(c.id) === src)
  return (
    <Modal title={`VM access — ${r.project_name}`} onClose={onClose}>
      <p className="muted" style={{ marginTop: 0 }}>
        One login account — like Sysible Controller: the same user + password (or key) is created on the VMs and
        used by SLEP to log in and to <span className="mono">sudo</span>. cloud-init creates it; Ansible and Salt
        both authenticate as it. Re-apply to rebuild existing VMs with these settings.
      </p>
      <Field label="Login credential — a stored credential used as the VM account (login + sudo)">
        <select value={src} onChange={(e) => setSrc(e.target.value)}>
          {creds.map((c) => <option key={c.id} value={c.id}>{c.name} — {c.username || '?'} ({c.kind === 'ssh_password' ? 'password' : 'key'})</option>)}
          <option value="__manual">＋ Enter a username + password manually…</option>
        </select>
        {picked
          ? <div className="faint" style={{ fontSize: 11.5 }}>
              cloud-init creates <b>{picked.username}</b> with this credential’s {picked.kind === 'ssh_password' ? 'password' : 'key'}; Ansible &amp; Salt log in as it.
            </div>
          : <div className="faint" style={{ fontSize: 11.5 }}>Type an account below — or pick one you already made in the <b>Credentials</b> tab.</div>}
      </Field>
      {src === '__manual' && (
        <>
          <Field label="Username (e.g. admin)">
            <input value={sshUser} onChange={(e) => setSshUser(e.target.value)} placeholder="admin" />
          </Field>
          <Field label={`Password — a literal, or a Vault variable like vault.admin_pw${r.has_password ? ' (a password is already set — leave blank to keep it)' : ''}`}>
            <input value={pw} onChange={(e) => setPw(e.target.value)} placeholder={r.has_password ? '•••••••• (set — type to change)' : 'admin_pass or vault.admin_pw'} />
          </Field>
        </>
      )}
      <Field label="Jump host (user@host[:port]) — empty for a direct connection">
        <input value={bastion} onChange={(e) => setBastion(e.target.value)} placeholder="admin@192.168.8.212" />
      </Field>
      <div style={{ background: 'rgba(240,180,40,0.10)', border: '1px solid rgba(240,180,40,0.35)',
        borderRadius: 8, padding: '8px 10px', margin: '10px 0 2px', fontSize: 12, color: 'var(--warn, #e0a83a)' }}>
        ⚠ These settings take effect when a VM is <b>built</b>. Existing VMs keep the account they were created
        with — <b>re-apply</b> (Destroy → Apply, or Apply if the name changed) to rebuild them with these, or use
        <b> Fix SSH</b> if you can still log in.
      </div>
      {node}
      <div className="row" style={{ marginTop: 12, justifyContent: 'flex-end' }}>
        <button className="ghost sm" onClick={onClose}>Cancel</button>
        <button className="primary sm" onClick={() => wrap(async () => {
          const body = { bastion: bastion.trim() }
          if (src === '__manual') {
            if (sshUser.trim() && sshUser.trim() !== (r.ssh_user || '')) body.ssh_user = sshUser.trim()
            if (pw.trim()) body.ssh_password = pw.trim()
          } else {
            body.login_credential_id = Number(src)   // backend derives user + password/key
          }
          await api(`infra/${r.project_id}`, { method: 'PATCH', json: body })
          onSaved()
        })}>Save</button>
      </div>
    </Modal>
  )
}

// Pick a Controller to enroll a project's VMs into, when the project has none set.
// The choice is remembered by the backend for future enrolls.
export function EnrollPicker({ r, controllers, onClose, onPick }) {
  const [cid, setCid] = useState(controllers[0] ? String(controllers[0].id) : '')
  const [busy, setBusy] = useState(false)
  return (
    <Modal title={`Enroll “${r.project_name}” VMs into a Controller`} onClose={onClose}>
      <p className="muted" style={{ marginTop: 0 }}>Register the VMs this project applied as SSH hosts in a connected Controller.</p>
      <Field label="Controller">
        <select value={cid} onChange={(e) => setCid(e.target.value)}>
          {controllers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </Field>
      <div className="row" style={{ marginTop: 12, justifyContent: 'flex-end' }}>
        <button className="ghost sm" onClick={onClose}>Cancel</button>
        <button className="primary sm" disabled={busy || !cid}
          onClick={() => { setBusy(true); onPick(cid) }}>{busy ? 'Enrolling…' : 'Enroll →'}</button>
      </div>
    </Modal>
  )
}

// The domains actually on a project's hypervisor (virsh list --all). A quick way
// to see what exists / is running without SSHing to the host.
export function VmsModal({ data, onClose }) {
  const stateColor = (s) => /running/i.test(s) ? 'var(--green-bright)'
    : /paused/i.test(s) ? 'var(--warn)' : /shut|off/i.test(s) ? 'var(--muted)' : 'var(--text)'
  return (
    <Modal title={`VMs on ${data.name}'s hypervisor`} onClose={onClose}>
      {data.loading ? <div className="muted">Querying the hypervisor…</div>
        : data.error ? <div style={{ color: 'var(--danger)', fontSize: 13, whiteSpace: 'pre-wrap' }}>✗ {data.error}</div>
          : (data.list && data.list.length) ? (
            <table style={{ width: '100%' }}>
              <thead><tr><th>Name</th><th>State</th></tr></thead>
              <tbody>
                {data.list.map((v) => (
                  <tr key={v.name}>
                    <td className="mono">{v.name}</td>
                    <td style={{ color: stateColor(v.state) }}>● {v.state}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="muted">No domains defined on this hypervisor.</div>}
      <div className="faint" style={{ fontSize: 11.5, marginTop: 8 }}>Read-only <span className="mono">virsh list --all</span> against this project's connection URI.</div>
    </Modal>
  )
}

// Test whether a key or a password actually logs in to this project's VMs THROUGH
// the jump host — a read-only probe (nothing on the VMs changes). Also reports who
// you logged in as and whether cloud-init is present, so a "login refused" turns
// into a real diagnosis (e.g. the base image has no cloud-init, so the baked-in
// account never got created).
export function TestAuthModal({ r, onClose }) {
  const [method, setMethod] = useState('key')
  const [creds, setCreds] = useState([])
  const [credId, setCredId] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [res, setRes] = useState(null)
  const { wrap, node } = useErr()
  useEffect(() => { api('credentials').then((d) => setCreds((d.credentials || []).filter((c) => c.kind === 'ssh' || c.kind === 'ssh_password'))).catch(() => {}) }, [])
  const keyCreds = creds.filter((c) => c.kind === 'ssh')
  const pwCreds = creds.filter((c) => c.kind === 'ssh_password')
  const run = () => wrap(async () => {
    setBusy(true); setRes(null)
    try {
      const json = { method }
      if (credId) json.credential_id = Number(credId)
      if (method === 'password' && password) json.password = password
      const d = await api(`infra/${r.project_id}/test-auth`, { method: 'POST', json })
      setRes(d)
    } finally { setBusy(false) }
  })
  return (
    <Modal title={`Test VM login — ${r.project_name}`} onClose={onClose} wide>
      <p className="muted" style={{ marginTop: 0 }}>
        Check whether a key or password authenticates to the VMs {r.bastion ? <>through the jump host <span className="mono">{r.bastion}</span></> : '(no jump host set)'} — read-only, nothing on the VMs is changed.
      </p>
      <Field label="What to test">
        <select value={method} onChange={(e) => { setMethod(e.target.value); setCredId('') }}>
          <option value="key">A key</option>
          <option value="password">A password</option>
        </select>
      </Field>
      {method === 'key' ? (
        <Field label="Key">
          <select value={credId} onChange={(e) => setCredId(e.target.value)}>
            <option value="">SLEP managed key (the one baked into the VMs)</option>
            {keyCreds.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </Field>
      ) : (
        <>
          <Field label="Password credential">
            <select value={credId} onChange={(e) => { setCredId(e.target.value); if (e.target.value) setPassword('') }}>
              <option value="">— the project's saved login password —</option>
              {pwCreds.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </Field>
          {!credId && (
            <Field label="…or type a password to test">
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="leave blank to use the saved one" autoComplete="off" />
            </Field>
          )}
        </>
      )}
      {node}
      <div className="row" style={{ marginTop: 12, justifyContent: 'flex-end' }}>
        <button className="ghost sm" onClick={onClose}>Close</button>
        <button className="primary sm" disabled={busy} onClick={run}>{busy ? 'Testing…' : 'Test login'}</button>
      </div>
      {res && (
        <div style={{ marginTop: 14 }}>
          {res.note ? <div className="muted" style={{ whiteSpace: 'pre-wrap' }}>{res.note}</div> : (
            <>
              <div className="muted" style={{ marginBottom: 6 }}>
                {res.ok}/{res.total} host(s) accepted the {res.label}{res.bastion ? <> via <span className="mono">{res.bastion}</span></> : ''}.
              </div>
              <table style={{ width: '100%' }}>
                <thead><tr><th>Host</th><th>Result</th><th>Logged in as</th><th>cloud-init</th></tr></thead>
                <tbody>
                  {(res.results || []).map((h) => (
                    <tr key={h.name}>
                      <td className="mono">{h.name}<br /><span className="faint" style={{ fontSize: 11 }}>{h.target || h.ip}</span></td>
                      <td style={{ color: h.ok ? 'var(--green-bright)' : 'var(--danger)' }}>{h.ok ? '✓ authenticated' : '✗ ' + h.detail}</td>
                      <td className="mono">{h.who || '—'}</td>
                      <td className="mono" style={{ fontSize: 12, color: /not installed|absent/i.test(h.cloud_init || '') ? 'var(--danger)' : 'var(--muted)' }}>{h.cloud_init ? h.cloud_init.replace(/^cloud-init:?\s*/i, '') : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="faint" style={{ fontSize: 11.5, marginTop: 8 }}>
                If login fails and cloud-init shows <span className="mono">not installed</span>, the base image has no cloud-init — the admin
                account, password and keys SLEP put in the config were never applied. Rebuild from an image that ships cloud-init (most
                cloud/“-cloudimg” images do), or bake the account into the base image yourself.
              </div>
            </>
          )}
        </div>
      )}
    </Modal>
  )
}

// The Sysible lifecycle cadence: create the machines (Terraform/OpenTofu), then
// configure them (Ansible), then keep them in a known state over time (Salt).
// Shown as a guide so the recommended flow is obvious from the Infrastructure page.
export function CadenceBar() {
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

// When `project` is passed, the wizard generates the Terraform INTO that existing
// project (the "build infra here" flow from the IDE) instead of creating a new one.
export function CreateWizard({ onClose, onDone, project }) {
  const [schema, setSchema] = useState(null)
  const [controllers, setControllers] = useState([])
  const [name, setName] = useState(project?.name || '')
  const [provider, setProvider] = useState('')
  const [values, setValues] = useState({})
  const [controllerId, setControllerId] = useState('')
  const [creds, setCreds] = useState([])
  const [deployCredId, setDeployCredId] = useState('')
  const [invs, setInvs] = useState([])
  const [invTarget, setInvTarget] = useState('')   // '' = dedicated, <id> = existing, '__new'
  const [invName, setInvName] = useState('')
  const [cloudImages, setCloudImages] = useState([])
  const { wrap, node } = useErr()

  useEffect(() => { api('infra/providers').then((d) => {
    setSchema(d.providers)
    setCloudImages(d.cloud_images || [])
    const first = Object.keys(d.providers)[0]
    setProvider(first); seed(d.providers, first)
  }) }, [])
  useEffect(() => { api('controllers').then((d) => setControllers(d.controllers)) }, [])
  // SSH key credentials only — those are the ones we can derive a public key from
  // and bake into the VMs so SLEP's Ansible/Salt can log in.
  useEffect(() => { api('credentials').then((d) => setCreds((d.credentials || []).filter((c) => c.kind === 'ssh'))) }, [])
  useEffect(() => { api('inventories').then((d) => setInvs(d.inventories || [])) }, [])

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
    <Modal title={project ? `Build infrastructure in “${project.name}”` : 'Create infrastructure'} onClose={onClose} wide>
      {project
        ? <div className="faint" style={{ fontSize: 12, marginBottom: 8 }}>Generates the Terraform (and cloud-init) into this project, then it gets the infra lifecycle actions.</div>
        : <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="prod-web" autoFocus /></Field>}
      <Field label="Provider">
        <select value={provider} onChange={(e) => pickProvider(e.target.value)}>
          {Object.entries(schema).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
        </select>
      </Field>
      <div className="faint" style={{ fontSize: 12, margin: '2px 0 8px' }}>{schema[provider]?.blurb}</div>

      {provider === 'libvirt' && <LibvirtConnect values={values} set={set} />}
      {provider === 'libvirt' && <PoolVolumePicker values={values} set={set} />}
      {provider === 'libvirt' && <BaseImageField values={values} set={set} catalog={cloudImages} />}

      <div className="task-palette" style={{ gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        {opts.filter((o) => !(provider === 'libvirt' && (o.key === 'uri' || o.key === 'base_volume' || o.key === 'base_image'))).map((o) => (
          <Field key={o.key} label={o.label}>
            {o.type === 'select'
              ? <select value={values[o.key] ?? ''} onChange={(e) => set(o.key, e.target.value)}>
                {o.choices.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              : o.type === 'textarea'
                ? <textarea rows={2} value={values[o.key] ?? ''} onChange={(e) => set(o.key, e.target.value)}
                  style={{ fontFamily: 'ui-monospace,monospace', fontSize: 12 }} />
                : <input type={o.type === 'number' ? 'number' : o.type === 'password' ? 'password' : 'text'}
                  autoComplete={o.type === 'password' ? 'new-password' : undefined} value={values[o.key] ?? ''}
                  onChange={(e) => set(o.key, o.type === 'number' ? Number(e.target.value) : e.target.value)} />}
            {o.help && <div className="faint" style={{ fontSize: 11 }}>{o.help}</div>}
          </Field>
        ))}
      </div>

      <Field label="Deploy SSH credential (so SLEP can log in to configure the VMs)">
        <select value={deployCredId} onChange={(e) => setDeployCredId(e.target.value)}>
          <option value="">None (VMs won’t be reachable by SLEP unless you add a key another way)</option>
          {creds.map((c) => <option key={c.id} value={c.id}>{c.name}{c.username ? ` (${c.username})` : ''}</option>)}
        </select>
      </Field>
      <div className="faint" style={{ fontSize: 12 }}>
        SLEP bakes this credential’s <b>public</b> key into the VMs’ cloud-init, so the same credential you pick for the cadence’s Ansible/Salt steps can SSH in. Pick an <b>SSH key</b> credential.
        {creds.length === 0 && <> No SSH key credentials yet — add one under <b>Credentials</b> first.</>}
      </div>

      <Field label="Add the new VMs to inventory">
        <select value={invTarget} onChange={(e) => setInvTarget(e.target.value)}>
          <option value="">A dedicated inventory — “{name || 'name'} (VMs)”</option>
          {invs.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
          <option value="__new">＋ New inventory…</option>
        </select>
      </Field>
      {invTarget === '__new' && <Field label="New inventory name"><input value={invName} onChange={(e) => setInvName(e.target.value)} placeholder="prod-web" /></Field>}
      <div className="faint" style={{ fontSize: 12 }}>On apply, the created VMs are read into this inventory automatically, and the cadence’s Ansible/Salt steps default to it.</div>

      <Field label="Auto-enroll new VMs into Controller (optional)">
        <select value={controllerId} onChange={(e) => setControllerId(e.target.value)}>
          <option value="">Don’t enroll</option>
          {controllers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </Field>
      <div className="faint" style={{ fontSize: 12 }}>If chosen, the Controller’s SSH key is also baked into the VMs’ cloud-init so it can reach them, and “Enroll →” registers them after apply.</div>
      {node}
      <button className="primary" onClick={() => wrap(async () => {
        if (!project && !name.trim()) throw new Error('Give it a name.')
        const d = await api('infra', { method: 'POST', json: {
          name: name || project?.name, provider, options: values,
          project_id: project ? project.id : undefined,
          controller_id: controllerId ? Number(controllerId) : null,
          deploy_credential_id: deployCredId ? Number(deployCredId) : null,
          inventory_id: (invTarget && invTarget !== '__new') ? Number(invTarget) : null,
          inventory_name: invTarget === '__new' ? invName.trim() : '',
        } })
        onDone(d.project_id, name || project?.name, d.slug)
      })}>{project ? 'Build in this project' : 'Generate Terraform'}</button>
    </Modal>
  )
}

// Base image to DOWNLOAD (used when no existing pool volume is chosen). Offers a
// catalog of common cloud images so you don't hunt for URLs, plus a free URL field.
function BaseImageField({ values, set, catalog }) {
  const url = values.base_image || ''
  const known = catalog.find((c) => c.url === url)
  const usingPool = !!(values.base_volume || '').trim()
  return (
    <Field label={'Download a base image' + (usingPool ? ' (ignored — using the pool volume above)' : '')}>
      <div className="row" style={{ gap: 6 }}>
        <select value={known ? known.url : ''} title="Common cloud images" disabled={usingPool}
          onChange={(e) => { if (e.target.value) set('base_image', e.target.value) }} style={{ width: 210 }}>
          <option value="">Pick a common image…</option>
          {catalog.map((c) => <option key={c.url} value={c.url}>{c.label}</option>)}
        </select>
        <input value={url} disabled={usingPool} onChange={(e) => set('base_image', e.target.value)}
          placeholder="https://…/image.qcow2" style={{ flex: 1 }} />
      </div>
      <div className="faint" style={{ fontSize: 11 }}>Pick a distro or paste a qcow2/img URL — downloaded once into the pool and shared (copy-on-write per VM).</div>
    </Field>
  )
}

// Pick a base image already in the hypervisor's pool (skips the download). Lists
// the pool's disk images via a read-only virsh probe; falls back to a text field
// (type the name) when nothing loads or virsh isn't available on the SLEP host.
function PoolVolumePicker({ values, set }) {
  const [vols, setVols] = useState(null)   // null=not loaded, []=none, [names]
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const cur = values.base_volume || ''
  const load = async () => {
    setBusy(true); setErr('')
    try {
      const d = await api('infra/hypervisor-volumes', { method: 'POST', json: { uri: values.uri, pool: values.pool } })
      if (d.ok) setVols(d.volumes || [])
      else { setVols([]); setErr(d.output || 'Could not list images.') }
    } catch (e) { setVols([]); setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <div style={{ margin: '2px 0 10px' }}>
      <Field label="Base image — use one already in the pool (skips download)">
        <div className="row" style={{ gap: 6 }}>
          {vols && vols.length > 0
            ? <select value={vols.includes(cur) ? cur : ''} onChange={(e) => set('base_volume', e.target.value)} style={{ flex: 1 }}>
                <option value="">(none — download the base image URL below)</option>
                {vols.map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
            : <input value={cur} onChange={(e) => set('base_volume', e.target.value)} style={{ flex: 1 }}
                     placeholder="e.g. jammy.qcow2 — or click “Load images” to pick" />}
          <button type="button" className="ghost sm" disabled={busy || !values.uri} title="List images in the pool on the hypervisor"
            onClick={load}>{busy ? 'Loading…' : 'Load images'}</button>
        </div>
        {vols && vols.length === 0 && !err && <div className="faint" style={{ fontSize: 11 }}>No images in pool “{values.pool || 'default'}”. Type a name, or leave blank to download the URL below.</div>}
        {err && <div className="faint" style={{ fontSize: 11, color: 'var(--danger)' }}>{err}</div>}
        {!vols && !err && <div className="faint" style={{ fontSize: 11 }}>Leave blank to download the base image URL below, or “Load images” to clone one already on the hypervisor.</div>}
        <div className="faint" style={{ fontSize: 11, marginTop: 4 }}>
          ⚠ Must be a <b>cloud image</b> (a <span className="mono">*-cloudimg</span> / <span className="mono">.img</span> that runs cloud-init on first boot). A disk you <b>installed from an ISO</b> won’t run cloud-init — the login account, password and SSH keys never get created and the VM stays unreachable.
        </div>
      </Field>
    </div>
  )
}

// Network + storage-pool picker for the libvirt connection panel. Reads what the
// hypervisor ACTUALLY has (virsh net-list / pool-list) and offers them as
// dropdowns — so you pick the real network ('homelab') instead of guessing
// 'default', and see at a glance which pool is inactive (with a one-click start).
function NetworkPoolPicker({ values, set }) {
  const [data, setData] = useState(null)   // {networks:[{name,active}], pools:[...]}
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [starting, setStarting] = useState('')
  const load = async () => {
    setBusy(true); setErr('')
    try {
      const d = await api('infra/hypervisor-networks', { method: 'POST', json: { uri: values.uri } })
      if (d.ok) setData({ networks: d.networks || [], pools: d.pools || [] })
      else { setData({ networks: [], pools: [] }); setErr(d.output || 'Could not list networks/pools.') }
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }
  const startPool = async (name) => {
    setStarting(name)
    try { await api('infra/hypervisor-pool-start', { method: 'POST', json: { uri: values.uri, pool: name } }); await load() }
    catch (e) { setErr(e.message) } finally { setStarting('') }
  }
  const net = values.network || 'default'
  const pool = values.pool || 'default'
  const nets = data?.networks || []
  const pools = data?.pools || []
  const poolObj = pools.find((p) => p.name === pool)
  const sel = (cur, list, key, ph) => (list.length > 0
    ? <select value={list.some((x) => x.name === cur) ? cur : ''} onChange={(e) => set(key, e.target.value)} style={{ flex: 1 }}>
        <option value="">(pick one)</option>
        {list.map((x) => <option key={x.name} value={x.name}>{x.name}{x.active ? '' : ' — inactive'}</option>)}
      </select>
    : <input value={cur} onChange={(e) => set(key, e.target.value)} style={{ flex: 1 }} placeholder={ph} />)
  return (
    <div style={{ margin: '2px 0 8px' }}>
      <div className="row" style={{ gap: 10 }}>
        <Field label="Network"><div className="row" style={{ gap: 6 }}>{sel(net, nets, 'network', 'e.g. homelab')}</div></Field>
        <Field label="Storage pool"><div className="row" style={{ gap: 6 }}>{sel(pool, pools, 'pool', 'e.g. default')}</div></Field>
        <button type="button" className="ghost sm" style={{ alignSelf: 'flex-end', whiteSpace: 'nowrap' }}
          disabled={busy || !values.uri} title="List the networks and pools on the hypervisor"
          onClick={load}>{busy ? 'Loading…' : 'Load from host'}</button>
      </div>
      {poolObj && !poolObj.active && (
        <div className="faint" style={{ fontSize: 11.5, color: 'var(--danger)', marginTop: 2 }}>
          Pool “{pool}” is defined but INACTIVE — apply will fail.{' '}
          <a onClick={() => startPool(pool)} style={{ cursor: 'pointer' }}>{starting === pool ? 'starting…' : 'Start it now'}</a>
        </div>
      )}
      {err && <div className="faint" style={{ fontSize: 11, color: 'var(--danger)', marginTop: 2 }}>{err}</div>}
      {!data && !err && <div className="faint" style={{ fontSize: 11, marginTop: 2 }}>“Load from host” to pick the real network + pool (e.g. this host has ‘homelab’, not ‘default’).</div>}
    </div>
  )
}

// Hypervisor connection helper for the libvirt provider. Turns the fiddly
// qemu+ssh URI + SSH-key dance into: pick Local or Remote, (for remote) enter
// host + user and click "Get deploy key" — SLEP returns its managed public key
// with a ready-to-paste install command and assembles the
// qemu+ssh://user@host/system?keyfile=…&no_verify=1 URI for you. The assembled
// URI is what the generated Terraform uses; you can still edit it by hand.
function LibvirtConnect({ values, set }) {
  const startRemote = /^qemu\+/.test(values.uri || '')
  const [mode, setMode] = useState(startRemote ? 'remote' : 'local')
  const [host, setHost] = useState('')
  const [user, setUser] = useState('root')
  const [pw, setPw] = useState('')             // one-time host password (or vault.NAME)
  const [installing, setInstalling] = useState(null)  // {ok, output} | 'installing'
  const [key, setKey] = useState(null)        // {public_key, keyfile}
  const [hvTest, setHvTest] = useState(null)   // {ok, output} | 'testing'
  const [copied, setCopied] = useState(false)
  const { wrap, node } = useErr()

  const buildUri = (m, h, u, k) => {
    if (m === 'local') return 'qemu:///system'
    const base = `qemu+ssh://${(u || 'root').trim()}@${(h || '').trim()}/system`
    const q = []
    if (k?.keyfile) q.push('keyfile=' + k.keyfile)
    q.push('no_verify=1')
    return base + '?' + q.join('&')
  }
  // Keep the wizard's uri value in step with the helper's fields. `set` isn't a
  // dep (it's a fresh closure each render); the field values are what matter.
  useEffect(() => { set('uri', buildUri(mode, host, user, key)) }, [mode, host, user, key])   // eslint-disable-line

  const cmd = key
    ? `ssh ${(user || 'root').trim()}@${(host || 'HYPERVISOR').trim()} 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo "${key.public_key}" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'`
    : ''
  const copy = async () => { try { await navigator.clipboard.writeText(cmd); setCopied(true); setTimeout(() => setCopied(false), 1500) } catch { /* ignore */ } }

  return (
    <div style={{ border: '1px solid var(--line)', borderRadius: 10, padding: 12, margin: '2px 0 12px' }}>
      <div className="row" style={{ gap: 10, alignItems: 'center' }}>
        <b style={{ fontSize: 13 }}>Hypervisor connection</b>
        <select value={mode} onChange={(e) => setMode(e.target.value)} style={{ width: 220 }}>
          <option value="local">Local host (qemu:///system)</option>
          <option value="remote">Remote KVM host over SSH</option>
        </select>
      </div>

      {mode === 'remote' && (
        <>
          <div className="row" style={{ gap: 10, marginTop: 8 }}>
            <Field label="Host (IP or name)"><input value={host} onChange={(e) => setHost(e.target.value)} placeholder="192.168.8.212" /></Field>
            <Field label="SSH user"><input value={user} onChange={(e) => setUser(e.target.value)} placeholder="root / admin" /></Field>
          </div>
          {/* One-time password: let SLEP install its key for you, breaking the
              chicken-and-egg (key auth can't work until the key is on the host).
              The password is used once and never stored; it may be a Vault variable. */}
          <div className="row" style={{ gap: 10, marginTop: 8, alignItems: 'flex-end' }}>
            <Field label="Host password (one-time — installs SLEP’s key; or vault.NAME)">
              <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} placeholder="password or vault.kvm_pw" />
            </Field>
            <button className="primary sm" disabled={installing === 'installing' || !host || !pw}
              onClick={() => wrap(async () => {
                setInstalling('installing')
                try {
                  const r = await api('infra/install-hypervisor-key', { method: 'POST', json: { host: host.trim(), user: user.trim(), password: pw.trim() } })
                  setInstalling(r)
                  // Point the qemu+ssh URI at the key we just installed — this is what
                  // was missing: the key was on the host but the URI never referenced it.
                  if (r.public_key || r.keyfile) setKey({ public_key: r.public_key || (key && key.public_key) || '', keyfile: r.keyfile || (key && key.keyfile) || '' })
                } catch (e) { setInstalling({ ok: false, output: String(e.message || e) }) }
              })} style={{ whiteSpace: 'nowrap' }}>
              {installing === 'installing' ? 'Installing…' : '🔐 Install key with password'}
            </button>
          </div>
          {installing && installing !== 'installing' && (
            <div style={{ fontSize: 12.5, marginTop: 4, color: installing.ok ? 'var(--green-bright)' : 'var(--danger)' }}>
              {installing.ok ? '✓ ' : '✗ '}<span className="muted" style={{ whiteSpace: 'pre-wrap' }}>{installing.output}</span>
            </div>
          )}
          <div className="row" style={{ gap: 10, alignItems: 'center', marginTop: 8 }}>
            {!key
              ? <button className="ghost sm" onClick={() => wrap(async () => setKey(await api('infra/hypervisor-key', { method: 'POST' })))}>Get deploy key</button>
              : <button className="ghost sm" title="Mint a brand-new SLEP key — the old one stops working; re-install it here or with the password button"
                  onClick={() => wrap(async () => {
                    if (!window.confirm('Regenerate SLEP’s managed key?\n\nA brand-new key is minted and the OLD one stops working immediately — you must re-install it on this (and every other) hypervisor. Continue?')) return
                    const r = await api('infra/managed-key/regenerate', { method: 'POST' })
                    setKey({ public_key: r.public_key, keyfile: (key && key.keyfile) || '' })
                  })}>Regenerate deploy key</button>}
            <span className="faint" style={{ fontSize: 12 }}>No password? Install SLEP’s key by hand once — then this and every future hypervisor just works.</span>
          </div>
          {key && cmd && (
            <div style={{ marginTop: 8 }}>
              <div className="faint" style={{ fontSize: 12, marginBottom: 4 }}>Run this once on the hypervisor (or via SSH from anywhere that can reach it):</div>
              <div className="row" style={{ gap: 6, alignItems: 'flex-start' }}>
                <textarea readOnly rows={3} value={cmd} onClick={(e) => e.target.select()}
                  style={{ fontFamily: 'ui-monospace,monospace', fontSize: 11.5, resize: 'vertical', flex: 1 }} />
                <button className="ghost sm" onClick={copy}>{copied ? '✓ Copied' : 'Copy'}</button>
              </div>
            </div>
          )}
        </>
      )}

      <Field label="Connection URI (assembled — editable)">
        <input value={values.uri || ''} onChange={(e) => set('uri', e.target.value)}
          style={{ fontFamily: 'ui-monospace,monospace', fontSize: 12 }} />
      </Field>
      <NetworkPoolPicker values={values} set={set} />
      <div className="row" style={{ gap: 10, alignItems: 'center' }}>
        <button className="ghost sm" disabled={hvTest === 'testing' || !values.uri}
          onClick={() => wrap(async () => {
            setHvTest('testing')
            try { setHvTest(await api('infra/test-hypervisor', { method: 'POST', json: { uri: values.uri, network: values.network, pool: values.pool } })) }
            catch (e) { setHvTest({ ok: false, output: String(e.message || e) }) }
          })}>
          {hvTest === 'testing' ? 'Testing…' : '⚡ Test hypervisor connection'}
        </button>
        {hvTest && hvTest !== 'testing' && (
          <span style={{ fontSize: 12.5, color: hvTest.ok ? 'var(--green-bright)' : 'var(--danger)' }}>
            {hvTest.ok ? '✓ reachable' : '✗ failed'} — <span className="muted" style={{ whiteSpace: 'pre-wrap' }}>{hvTest.output}</span>
          </span>
        )}
      </div>
      {node}
    </div>
  )
}
