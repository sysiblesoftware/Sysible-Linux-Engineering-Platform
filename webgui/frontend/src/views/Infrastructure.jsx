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
                  {writable && <button className="ghost sm" title={r.bastion ? `Jump host: ${r.bastion}` : 'Set the SSH jump host (hypervisor) used to reach these VMs'} onClick={() => setJumpFor(r)}>{r.bastion ? '⤳ Jump host' : '+ Jump host'}</button>}
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

// Designate the SSH jump host for a project's VMs at the project level — pushed
// down to every inventory the project owns, so runs/tests/key-distribution hop
// through it. For libvirt this is the hypervisor (auto-set on create); this lets
// you view or override it without visiting each inventory.
function JumpHostEditor({ r, onClose, onSaved }) {
  const [bastion, setBastion] = useState(r.bastion || '')
  const { wrap, node } = useErr()
  return (
    <Modal title={`Jump host — ${r.project_name}`} onClose={onClose}>
      <p className="muted" style={{ marginTop: 0 }}>
        The VMs sit on the hypervisor’s private network, which SLEP can’t reach directly — it hops through this
        jump host. For libvirt it’s the hypervisor itself (the <span className="mono">user@host</span> from its
        connection URI). Applies to every inventory in this project.
      </p>
      <Field label="Jump host (user@host[:port]) — empty for a direct connection">
        <input value={bastion} autoFocus onChange={(e) => setBastion(e.target.value)} placeholder="admin@192.168.8.212" />
      </Field>
      {node}
      <div className="row" style={{ marginTop: 12, justifyContent: 'flex-end' }}>
        <button className="ghost sm" onClick={onClose}>Cancel</button>
        <button className="primary sm" onClick={() => wrap(async () => {
          await api(`infra/${r.project_id}`, { method: 'PATCH', json: { bastion: bastion.trim() } })
          onSaved()
        })}>Save jump host</button>
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
  const { wrap, node } = useErr()

  useEffect(() => { api('infra/providers').then((d) => {
    setSchema(d.providers)
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

      <div className="task-palette" style={{ gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        {opts.filter((o) => !(provider === 'libvirt' && o.key === 'uri')).map((o) => (
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
          <div className="row" style={{ gap: 10, alignItems: 'center', marginTop: 2 }}>
            <button className="ghost sm" onClick={() => wrap(async () => setKey(await api('infra/hypervisor-key', { method: 'POST' })))}>
              🔑 {key ? 'Regenerate deploy key' : 'Get deploy key'}
            </button>
            <span className="faint" style={{ fontSize: 12 }}>SLEP’s managed key — install it on the host once, then this and every future hypervisor just works.</span>
          </div>
          {key && (
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
