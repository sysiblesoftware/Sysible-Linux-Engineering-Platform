import React, { useEffect, useState } from 'react'
import { api, canWrite } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'
import { PipelineModal } from './Ide.jsx'

// Create Infrastructure — a form-driven Terraform VM builder. The wizard's menus
// come from the backend provider schema; on create it generates a Terraform
// project you can Plan/Apply, then one-click enroll the new VMs into a Controller.
export default function Infrastructure({ onOpenProject, onOpenRun }) {
  const [rows, setRows] = useState([])
  const [open, setOpen] = useState(false)
  const [pipe, setPipe] = useState(null)   // {project, steps} → opens the pipeline builder
  const [vms, setVms] = useState(null)     // {name, loading?|list?|error?} → VMs-on-hypervisor modal
  const [controllers, setControllers] = useState([])
  const [enrollFor, setEnrollFor] = useState(null)   // {r} → Controller picker when none set
  const [jumpFor, setJumpFor] = useState(null)       // {r} → jump-host editor
  const load = () => api('infra').then((d) => setRows(d.infra))
  useEffect(() => { load(); api('controllers').then((d) => setControllers(d.controllers || [])).catch(() => {}) }, [])
  const writable = canWrite()

  const run = async (r, target) => {
    const d = await api('runs', { method: 'POST', json: { project_id: r.project_id, kind: 'terraform', target } })
    onOpenRun(d.run_id)
  }
  // Register this project's applied VMs into a Controller. Uses the project's set
  // Controller if it has one; otherwise opens a picker so you can choose on demand
  // (the choice is remembered for next time). This is the "enroll the new VMs"
  // button — always available on infra projects, no need to hunt for a run.
  const enroll = async (r, controllerId) => {
    if (!r.controller_id && !controllerId) {
      if (!controllers.length) { alert('Connect a Controller first (Controllers tab).'); return }
      setEnrollFor(r); return
    }
    try {
      const body = controllerId ? { controller_id: Number(controllerId) } : {}
      const d = await api(`infra/${r.project_id}/enroll`, { method: 'POST', json: body })
      setEnrollFor(null); load()
      const lines = d.results.map((h) => `${h.ok ? '✓' : '✗'} ${h.name} ${h.ip} — ${h.detail}`).join('\n')
      alert(`Enrolled ${d.enrolled}/${d.total} into ${d.controller}:\n\n${lines || '(no hosts yet — apply the VMs first)'}`)
    } catch (e) { alert(e.message) }
  }
  // Read the applied VMs (terraform/tofu output) into a SLEP Ansible inventory, so
  // the Configure/Maintain (Ansible/Salt) steps can target them. Run after apply.
  const toInventory = async (r) => {
    try {
      const d = await api(`infra/${r.project_id}/inventory`, { method: 'POST' })
      alert(`Built inventory “${d.name}” with ${d.hosts} host(s).\nIt's now selectable in Ansible/Salt runs and pipelines.`)
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
  // One-click cadence: scaffold configure/maintain (idempotent), then open the
  // pipeline builder pre-filled with apply → build-inventory → configure → maintain
  // so it runs as a sequence. The Inventory step reads the VMs the apply just
  // created into this project's inventory and auto-targets the Ansible/Salt steps
  // at them — so Create flows straight into Configure → Maintain with no manual
  // inventory hop.
  // Show the domains actually on this project's hypervisor (virsh list --all) —
  // so you can see what exists without SSHing to the host.
  const listVms = async (r) => {
    setVms({ name: r.project_name, loading: true })
    try {
      const d = await api(`infra/${r.project_id}/vms`, { method: 'POST' })
      setVms({ name: r.project_name, list: d.vms || [], error: d.ok ? '' : d.output })
    } catch (e) { setVms({ name: r.project_name, error: e.message }) }
  }
  const cadence = async (r) => {
    try {
      await api(`infra/${r.project_id}/scaffold`, { method: 'POST', json: { stage: 'configure' } })
      await api(`infra/${r.project_id}/scaffold`, { method: 'POST', json: { stage: 'maintain' } })
      setPipe({
        project: { id: r.project_id, name: r.project_name, slug: r.project_slug },
        steps: [
          { kind: 'terraform', target: 'apply', tool: 'terraform' },
          { kind: 'inventory', target: 'from VMs' },
          { kind: 'ansible', target: 'configure.yml' },
          { kind: 'salt', target: 'maintain.sls' },
        ],
      })
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
                <td className="muted">{r.provider}{r.bastion ? <><br /><span className="faint" style={{ fontSize: 11 }} title="VMs are reached through this hypervisor jump host">↳ via {r.bastion}</span></> : null}</td>
                <td className="muted">{r.controller_id ? `Controller #${r.controller_id}${r.environment ? ' · ' + r.environment : ''}` : '—'}</td>
                <td className="row">
                  {writable && <button className="ghost sm" title="Terraform/OpenTofu plan" onClick={() => run(r, 'plan')}>Plan</button>}
                  {writable && <button className="ghost sm" title="Terraform/OpenTofu apply — create the VMs" onClick={() => run(r, 'apply')}>Apply</button>}
                  {writable && <button className="danger ghost sm" onClick={() => run(r, 'destroy')}>Destroy</button>}
                  {writable && <button className="ghost sm" title="List the VMs actually on this project's hypervisor" onClick={() => listVms(r)}>🖥 VMs</button>}
                  {writable && <button className="ghost sm" title="Read the applied VMs into a SLEP Ansible inventory" onClick={() => toInventory(r)}>→ Inventory</button>}
                  {writable && <button className="ghost sm" title="Login user, password (Vault) and jump host used to reach these VMs" onClick={() => setJumpFor(r)}>⚙ Access</button>}
                  {writable && <button className="primary sm" title="Register the applied VMs into a connected Controller (agent enroll)" onClick={() => enroll(r)}>Enroll → Controller</button>}
                  {writable && <button className="ghost sm" title="Scaffold an Ansible playbook and open it (Configure)" onClick={() => scaffold(r, 'configure')}>Configure</button>}
                  {writable && <button className="ghost sm" title="Scaffold a Salt state and open it (Maintain)" onClick={() => scaffold(r, 'maintain')}>Maintain</button>}
                  {writable && <button className="primary sm" title="Run the whole cadence in sequence: apply → configure → maintain" onClick={() => cadence(r)}>▶ Cadence</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && <CreateWizard onClose={() => setOpen(false)}
        onDone={(pid, name, slug) => { setOpen(false); load(); onOpenProject({ id: pid, name, slug }) }} />}
      {pipe && <PipelineModal project={pipe.project} initialSteps={pipe.steps}
        onClose={() => setPipe(null)} onLaunched={(id) => { setPipe(null); onOpenRun(id) }} />}
      {vms && <VmsModal data={vms} onClose={() => setVms(null)} />}
      {enrollFor && <EnrollPicker r={enrollFor} controllers={controllers}
        onClose={() => setEnrollFor(null)} onPick={(cid) => enroll(enrollFor, cid)} />}
      {jumpFor && <JumpHostEditor r={jumpFor} onClose={() => setJumpFor(null)}
        onSaved={() => { setJumpFor(null); load() }} />}
    </>
  )
}

// Project-level VM access settings: the login user the keys are installed on (kept
// consistent across cloud-init, the Terraform output, and the inventory), an
// optional login password sourced from a Vault variable (turns on password SSH so
// a VM is reachable even before its key lands), and the SSH jump host. All apply to
// every inventory the project owns, so you set them once instead of per inventory.
function JumpHostEditor({ r, onClose, onSaved }) {
  const [sshUser, setSshUser] = useState(r.ssh_user || '')
  const [pw, setPw] = useState('')
  const [bastion, setBastion] = useState(r.bastion || '')
  const { wrap, node } = useErr()
  return (
    <Modal title={`VM access — ${r.project_name}`} onClose={onClose}>
      <p className="muted" style={{ marginTop: 0 }}>
        How SLEP logs into this project’s VMs. The login user is baked into the cloud-init, the Terraform output,
        and the inventory together, so they never drift out of step (the mismatch that causes
        <span className="mono"> Permission denied</span>). Re-apply to rebuild existing VMs with these settings.
      </p>
      <Field label="Login user — the account keys are installed on (e.g. admin)">
        <input value={sshUser} autoFocus onChange={(e) => setSshUser(e.target.value)} placeholder="admin" />
      </Field>
      <Field label="Login password — a Vault variable, e.g. vault.admin_pw (turns on password SSH; leave blank to keep unchanged)">
        <input value={pw} onChange={(e) => setPw(e.target.value)} placeholder="vault.admin_pw" />
      </Field>
      <Field label="Jump host (user@host[:port]) — empty for a direct connection">
        <input value={bastion} onChange={(e) => setBastion(e.target.value)} placeholder="admin@192.168.8.212" />
      </Field>
      {node}
      <div className="row" style={{ marginTop: 12, justifyContent: 'flex-end' }}>
        <button className="ghost sm" onClick={onClose}>Cancel</button>
        <button className="primary sm" onClick={() => wrap(async () => {
          const body = { bastion: bastion.trim() }
          if (sshUser.trim() && sshUser.trim() !== (r.ssh_user || '')) body.ssh_user = sshUser.trim()
          if (pw.trim()) body.ssh_password = pw.trim()
          await api(`infra/${r.project_id}`, { method: 'PATCH', json: body })
          onSaved()
        })}>Save</button>
      </div>
    </Modal>
  )
}

// Pick a Controller to enroll a project's VMs into, when the project has none set.
// The choice is remembered by the backend for future enrolls.
function EnrollPicker({ r, controllers, onClose, onPick }) {
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
function VmsModal({ data, onClose }) {
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
    <Modal title="Create infrastructure" onClose={onClose} wide>
      <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="prod-web" autoFocus /></Field>
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
        if (!name.trim()) throw new Error('Give it a name.')
        const d = await api('infra', { method: 'POST', json: {
          name, provider, options: values, controller_id: controllerId ? Number(controllerId) : null,
          deploy_credential_id: deployCredId ? Number(deployCredId) : null,
          inventory_id: (invTarget && invTarget !== '__new') ? Number(invTarget) : null,
          inventory_name: invTarget === '__new' ? invName.trim() : '',
        } })
        onDone(d.project_id, name, d.slug)
      })}>Generate Terraform</button>
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
            onClick={load}>{busy ? 'Loading…' : '↻ Load images'}</button>
        </div>
        {vols && vols.length === 0 && !err && <div className="faint" style={{ fontSize: 11 }}>No images in pool “{values.pool || 'default'}”. Type a name, or leave blank to download the URL below.</div>}
        {err && <div className="faint" style={{ fontSize: 11, color: 'var(--danger)' }}>{err}</div>}
        {!vols && !err && <div className="faint" style={{ fontSize: 11 }}>Leave blank to download the base image URL below, or “Load images” to clone one already on the hypervisor.</div>}
      </Field>
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
            <button className="ghost sm" onClick={() => wrap(async () => setKey(await api('infra/hypervisor-key', { method: 'POST' })))}>
              🔑 {key ? 'Regenerate deploy key' : 'Get deploy key'}
            </button>
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
