import React, { useEffect, useRef, useState } from 'react'
import { api, tail } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

export default function Inventories() {
  const [invs, setInvs] = useState([])
  const [selected, setSelected] = useState(null)   // inventory id being drilled into
  const [newOpen, setNewOpen] = useState(false)
  const [collapsed, setCollapsed] = useState({})   // {env: true} → group hidden
  const load = () => api('inventories').then((d) => setInvs(d.inventories))
  useEffect(() => { load() }, [])

  const sel = invs.find((i) => i.id === selected)
  if (sel) return <InventoryDetail inv={sel} onBack={() => { setSelected(null); load() }} onChanged={load} />

  // Nest inventories under their environment (dev / staging / prod / …). Untagged
  // ones fall under "Unassigned". Groups sort with the common envs first, then
  // alphabetically; a lone Unassigned group renders flat (no header noise).
  const ORDER = ['dev', 'development', 'test', 'testing', 'stage', 'staging', 'qa', 'uat', 'prod', 'production']
  const rank = (e) => { const i = ORDER.indexOf((e || '').toLowerCase()); return i < 0 ? 500 : i }
  const groups = {}
  for (const inv of invs) { const k = (inv.environment || '').trim() || 'Unassigned'; (groups[k] || (groups[k] = [])).push(inv) }
  const envs = Object.keys(groups).sort((a, b) => (rank(a) - rank(b)) || a.localeCompare(b))
  const flat = envs.length === 1 && envs[0] === 'Unassigned'

  const del = async (inv) => { if (confirm('Delete inventory ' + inv.name + '?')) { await api('inventories/' + inv.id, { method: 'DELETE' }); load() } }
  const Row = ({ inv }) => (
    <tr>
      <td><a onClick={() => setSelected(inv.id)}>{inv.name}</a></td>
      <td><span className="pill">{inv.source}</span></td>
      <td className="mono muted">{inv.bastion || '—'}</td>
      <td className="row">
        <button className="ghost sm" onClick={() => setSelected(inv.id)}>Open →</button>
        <button className="danger ghost sm" onClick={() => del(inv)}>Delete</button>
      </td>
    </tr>
  )

  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>Inventories</h2>
        <div className="spacer" />
        <button className="primary" onClick={() => setNewOpen(true)}>+ New inventory</button>
      </div>
      {invs.length === 0 ? <div className="muted">No inventories. Create one, then add hosts or import from a Sysible Controller.</div> : (
        <table>
          <thead><tr><th>Name</th><th>Source</th><th>Jump host</th><th></th></tr></thead>
          <tbody>
            {flat
              ? groups.Unassigned.map((inv) => <Row key={inv.id} inv={inv} />)
              : envs.map((env) => (
                <React.Fragment key={env}>
                  <tr className="env-head" onClick={() => setCollapsed((c) => ({ ...c, [env]: !c[env] }))}>
                    <td colSpan={4}>
                      <span className="env-caret">{collapsed[env] ? '▶' : '▼'}</span>
                      {env === 'Unassigned' ? <span className="faint">Unassigned</span> : env}
                      <span className="env-count">{groups[env].length}</span>
                    </td>
                  </tr>
                  {!collapsed[env] && groups[env].map((inv) => <Row key={inv.id} inv={inv} />)}
                </React.Fragment>
              ))}
          </tbody>
        </table>
      )}
      {newOpen && <NewInventory onClose={() => setNewOpen(false)} onDone={() => { setNewOpen(false); load() }} />}
    </>
  )
}

function InventoryDetail({ inv, onBack, onChanged }) {
  const [hosts, setHosts] = useState([])
  const [modal, setModal] = useState(null)
  const load = () => api(`inventories/${inv.id}/hosts`).then((d) => setHosts(d.hosts))
  useEffect(() => { load() }, [inv.id])
  return (
    <div className="col">
      <div className="row" style={{ marginBottom: 6 }}>
        <button className="ghost sm" onClick={onBack}>← Inventories</button>
      </div>
      <div className="card col">
      <div className="row">
        <b>{inv.name}</b><span className="pill">{inv.source}</span><span className="muted">{hosts.length} host(s)</span>
        {inv.environment && <span className="pill" title="Environment">🖿 {inv.environment}</span>}
        {inv.bastion && <span className="pill" title="Runs tunnel through this SSH jump host">⤳ {inv.bastion}</span>}
        <div className="spacer" />
        <button className="ghost sm" title="Environment & jump host" onClick={() => setModal('bastion')}>⚙ Settings</button>
        {inv.bastion && <button className="ghost sm" title="Install SLEP's key on the jump host so runs hop through it with the key" onClick={() => setModal('bastionprep')}>Prepare jump host</button>}
        <button className="ghost sm" onClick={() => setModal('import')}>Import from Controller</button>
        <button className="ghost sm" disabled={hosts.length === 0}
          title="Check SSH reachability of these hosts" onClick={() => setModal('test')}>◉ Test connection</button>
        <button className="ghost sm" disabled={hosts.length === 0}
          title="Install SLEP's SSH key on these hosts so runs are key-based" onClick={() => setModal('keydist')}>🔑 Distribute SSH key</button>
        <button className="ghost sm" onClick={() => setModal('host')}>+ Host</button>
        <button className="danger ghost sm" onClick={async () => { if (confirm('Delete inventory ' + inv.name + '?')) { await api('inventories/' + inv.id, { method: 'DELETE' }); onChanged() } }}>Delete</button>
      </div>
      {hosts.length > 0 && (
        <table>
          <thead><tr><th>Name</th><th>Address</th><th>Groups</th><th>Source</th><th></th></tr></thead>
          <tbody>
            {hosts.map((h) => (
              <tr key={h.id}>
                <td>{h.name}</td><td className="muted">{h.address}</td><td className="muted">{h.groups}</td><td className="muted">{h.source}</td>
                <td><button className="danger ghost sm" onClick={async () => { await api('hosts/' + h.id, { method: 'DELETE' }); load() }}>✕</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {modal === 'host' && <AddHost inv={inv} onClose={() => setModal(null)} onDone={() => { setModal(null); load(); onChanged() }} />}
      {modal === 'import' && <ImportController inv={inv} onClose={() => setModal(null)} onDone={() => { setModal(null); load(); onChanged() }} />}
      {modal === 'bastion' && <SetBastion inv={inv} onClose={() => setModal(null)} onDone={() => { setModal(null); onChanged() }} />}
      {modal === 'keydist' && <DistributeKey inv={inv} hosts={hosts} onClose={() => setModal(null)} />}
      {modal === 'test' && <TestConnection inv={inv} hosts={hosts} onClose={() => setModal(null)} />}
      {modal === 'bastionprep' && <PrepareBastion inv={inv} onClose={() => setModal(null)} />}
      </div>
    </div>
  )
}

// Common environments offered as quick picks (still free-text so any label works).
const ENV_SUGGESTIONS = ['dev', 'staging', 'qa', 'prod']

function NewInventory({ onClose, onDone }) {
  const [name, setName] = useState('')
  const [bastion, setBastion] = useState('')
  const [environment, setEnvironment] = useState('')
  const { wrap, node } = useErr()
  return (
    <Modal title="New inventory" onClose={onClose}>
      <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} autoFocus placeholder="e.g. web-tier" /></Field>
      <Field label="Environment (groups it in the list — dev / staging / prod / …)">
        <input value={environment} list="env-suggestions" onChange={(e) => setEnvironment(e.target.value)} placeholder="dev" />
        <datalist id="env-suggestions">{ENV_SUGGESTIONS.map((e) => <option key={e} value={e} />)}</datalist>
      </Field>
      <Field label="SSH jump host / bastion (optional)"><input value={bastion} onChange={(e) => setBastion(e.target.value)} placeholder="user@192.168.8.212  — reach hosts through this box" /></Field>
      <div className="muted">Set the jump host when hosts aren’t directly reachable (e.g. VMs on a hypervisor’s internal network). Runs tunnel SSH through it (ProxyJump).</div>
      {node}
      <button className="primary" onClick={() => wrap(async () => { await api('inventories', { method: 'POST', json: { name, bastion, environment } }); onDone() })}>Create</button>
    </Modal>
  )
}

function SetBastion({ inv, onClose, onDone }) {
  const [bastion, setBastion] = useState(inv.bastion || '')
  const [environment, setEnvironment] = useState(inv.environment || '')
  const { wrap, node } = useErr()
  return (
    <Modal title={`Settings — ${inv.name}`} onClose={onClose}>
      <Field label="Environment (groups it in the list)">
        <input value={environment} list="env-suggestions" onChange={(e) => setEnvironment(e.target.value)} autoFocus placeholder="dev / staging / prod" />
        <datalist id="env-suggestions">{ENV_SUGGESTIONS.map((e) => <option key={e} value={e} />)}</datalist>
      </Field>
      <div className="muted" style={{ margin: '4px 0' }}>SSH bastion to reach this inventory’s hosts through (ProxyJump). Leave empty for direct connections.</div>
      <Field label="Jump host"><input value={bastion} onChange={(e) => setBastion(e.target.value)} placeholder="user@192.168.8.212" /></Field>
      {node}
      <button className="primary" onClick={() => wrap(async () => { await api('inventories/' + inv.id, { method: 'PATCH', json: { bastion, environment } }); onDone() })}>Save</button>
    </Modal>
  )
}

function AddHost({ inv, onClose, onDone }) {
  const [name, setName] = useState(''); const [address, setAddress] = useState(''); const [groups, setGroups] = useState('')
  const { wrap, node } = useErr()
  return (
    <Modal title="Add host" onClose={onClose}>
      <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} autoFocus /></Field>
      <Field label="Address / IP"><input value={address} onChange={(e) => setAddress(e.target.value)} /></Field>
      <Field label="Groups (comma-separated)"><input value={groups} onChange={(e) => setGroups(e.target.value)} /></Field>
      {node}
      <button className="primary" onClick={() => wrap(async () => { await api(`inventories/${inv.id}/hosts`, { method: 'POST', json: { name, address, groups } }); onDone() })}>Add</button>
    </Modal>
  )
}

// Polls a job's log endpoint and accumulates it, stopping when the server reports
// the job is no longer running (X-Run-Status). Shared by the SSH action modals.
function useLogStream() {
  const [log, setLog] = useState('')
  const [running, setRunning] = useState(false)
  const timer = useRef(null)
  useEffect(() => () => { if (timer.current) clearInterval(timer.current) }, [])
  const run = async (startFn, logPath) => {
    setLog(''); setRunning(true)
    await startFn()
    let off = 0
    const tick = async () => {
      const { text, next, status } = await tail(`${logPath}?offset=${off}`)
      if (text) { off = next; setLog((l) => l + text) }
      if (status !== 'running') { clearInterval(timer.current); timer.current = null; setRunning(false) }
    }
    timer.current = setInterval(tick, 900); tick()
  }
  return { log, running, run }
}

// Render a streamed SSH-action log as a readable report: bold section headers,
// green ✓ / red ✗ per-host rows (host in strong text, reason muted), dim
// progress and notes. Falls back to a placeholder when empty.
function JobLog({ text, empty }) {
  if (!text) return <div className="job-log empty">{empty}</div>
  const rows = text.split('\n').map((raw, i) => {
    const t = raw.replace(/\s+$/, '')
    if (!t.trim()) return <div key={i} style={{ height: 6 }} />
    const bare = t.trim()
    if (/^==.*==$/.test(bare)) return <div key={i} className="jl-head">{bare.replace(/^=+\s*|\s*=+$/g, '')}</div>
    if (bare.startsWith('→')) return <div key={i} className="jl-prog">{bare.replace(/^→\s*/, '')}</div>
    if (bare.startsWith('--')) return <div key={i} className="jl-note">{bare.replace(/^--\s*/, '')}</div>
    if (bare.startsWith('Tip:') || bare.startsWith('!!')) return <div key={i} className="jl-tip">{bare.replace(/^!!\s*/, '')}</div>
    const ok = bare.includes('✓'), bad = bare.includes('✗')
    if (ok || bad) {
      const body = bare.replace(/^.*?[✓✗]\s*/, '')
      const m = body.match(/^(.*?)(?:\s+—\s+|:\s+)(.*)$/)
      const host = m ? m[1] : body, reason = m ? m[2] : ''
      return (
        <div key={i} className={ok ? 'jl-ok' : 'jl-bad'}>
          <span className="jl-ico">{ok ? '✓' : '✗'}</span>
          <b>{host}</b>{reason && <span className="jl-reason">— {reason}</span>}
        </div>
      )
    }
    return <div key={i} className="jl-line">{bare}</div>
  })
  return <div className="job-log">{rows}</div>
}

// Test SSH reachability of the inventory's hosts with a chosen credential (or
// SLEP's managed key), through the jump host. Read-only — just a per-host verdict.
function TestConnection({ inv, hosts, onClose }) {
  const [creds, setCreds] = useState([])
  const [cid, setCid] = useState('')     // '' = SLEP managed key
  const [sel, setSel] = useState(() => new Set(hosts.map((h) => h.name)))
  const { log, running, run } = useLogStream()
  const { wrap, node } = useErr()

  useEffect(() => {
    api('credentials').then((d) => {
      setCreds(d.credentials)
      const managed = d.credentials.find((c) => c.name === 'SLEP managed key')
      if (managed) setCid(String(managed.id))
    }).catch(() => {})
  }, [])
  const toggle = (name) => setSel((s) => { const n = new Set(s); n.has(name) ? n.delete(name) : n.add(name); return n })

  const start = () => wrap(async () => {
    if (sel.size === 0) throw new Error('Select at least one host.')
    await run(() => api(`inventories/${inv.id}/test-connection`, { method: 'POST', json: {
      credential_id: cid ? Number(cid) : null, host_names: [...sel] } }), `inventories/${inv.id}/test-connection/log`)
  })

  return (
    <Modal title={`Test connection — ${inv.name}`} onClose={onClose} wide>
      <div className="muted">Checks whether SLEP can SSH to each host with the selected credential{inv.bastion ? ' (through the jump host)' : ''}. Nothing is changed on the hosts.</div>
      <div className="job-split">
        <div className="job-col">
          <Field label="Authenticate with">
            <select value={cid} onChange={(e) => setCid(e.target.value)}>
              <option value="">SLEP managed key</option>
              {creds.map((c) => <option key={c.id} value={c.id}>{c.name}{c.username ? ` (${c.username})` : ''}</option>)}
            </select>
          </Field>
          <div className="faint" style={{ fontSize: 12, margin: '4px 2px' }}>{sel.size} of {hosts.length} host(s) selected</div>
          <div style={{ maxHeight: '38vh', overflow: 'auto', border: '1px solid var(--line)', borderRadius: 8 }}>
            {hosts.map((h) => (
              <label key={h.id} className="row" style={{ gap: 10, padding: '5px 10px', borderBottom: '1px solid var(--line)', cursor: 'pointer' }}>
                <input type="checkbox" checked={sel.has(h.name)} onChange={() => toggle(h.name)} style={{ width: 'auto' }} />
                <b className="mono" style={{ fontSize: 12, minWidth: 130 }}>{h.name}</b>
                <span className="muted mono" style={{ fontSize: 12 }}>{h.address}</span>
              </label>
            ))}
          </div>
          {node}
          <div className="row" style={{ marginTop: 8 }}>
            <div className="spacer" />
            <button className="ghost" onClick={onClose}>Close</button>
            <button className="primary" disabled={running} onClick={start}>{running ? 'Testing…' : `Test ${sel.size} host(s)`}</button>
          </div>
        </div>
        <div className="job-col">
          <div className="pane-title">Result</div>
          <JobLog text={log} empty="The per-host result appears here when you test." />
        </div>
      </div>
    </Modal>
  )
}

// Install SLEP's key on the inventory's jump host itself, so the ProxyJump hop is
// key-based like the targets. Authenticates once with the bastion password.
function PrepareBastion({ inv, onClose }) {
  const [bastion, setBastion] = useState(inv.bastion || '')
  const [password, setPassword] = useState('')
  const { log, running, run } = useLogStream()
  const { wrap, node } = useErr()

  const start = () => wrap(async () => {
    if (!bastion.includes('@')) throw new Error('Jump host must be user@host.')
    if (!password) throw new Error('Enter the jump host password (used once).')
    await run(() => api(`inventories/${inv.id}/prepare-bastion`, { method: 'POST', json: {
      bastion: bastion.trim(), password } }), `inventories/${inv.id}/prepare-bastion/log`)
  })

  return (
    <Modal title={`Prepare jump host — ${inv.name}`} onClose={onClose} wide>
      <div className="muted">Installs SLEP’s key on the jump host so runs and key distribution hop through it with the key — not a password. The password is used once and never saved.</div>
      <div className="job-split">
        <div className="job-col">
          <Field label="Jump host (user@host)"><input value={bastion} onChange={(e) => setBastion(e.target.value)} placeholder="admin@192.168.8.212" autoFocus /></Field>
          <Field label="Jump host password (used once)"><input type="password" value={password} autoComplete="off" onChange={(e) => setPassword(e.target.value)} /></Field>
          {node}
          <div className="row" style={{ marginTop: 8 }}>
            <div className="spacer" />
            <button className="ghost" onClick={onClose}>Close</button>
            <button className="primary" disabled={running} onClick={start}>{running ? 'Preparing…' : 'Prepare jump host'}</button>
          </div>
        </div>
        <div className="job-col">
          <div className="pane-title">Progress</div>
          <JobLog text={log} empty="Progress appears here when you start." />
        </div>
      </div>
    </Modal>
  )
}

// Push SLEP's managed public key onto the inventory's hosts, authenticating once
// with the host password (through the jump host if set). On success SLEP creates
// a "SLEP managed key" credential so future runs are key-based — no stored
// password. Streams the per-host progress log live.
function DistributeKey({ inv, hosts, onClose }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [bastion, setBastion] = useState(inv.bastion || '')
  const [sel, setSel] = useState(() => new Set(hosts.map((h) => h.name)))
  const [pubkey, setPubkey] = useState('')
  const [running, setRunning] = useState(false)
  const [log, setLog] = useState('')
  const timer = useRef(null)
  const { wrap, node } = useErr()

  useEffect(() => { api('keydist/public-key').then((d) => setPubkey(d.public_key || '')).catch(() => {}) }, [])
  useEffect(() => () => { if (timer.current) clearInterval(timer.current) }, [])

  const toggle = (name) => setSel((s) => { const n = new Set(s); n.has(name) ? n.delete(name) : n.add(name); return n })

  const poll = () => {
    let off = 0
    const tick = async () => {
      const { text, next, status } = await tail(`inventories/${inv.id}/distribute-key/log?offset=${off}`)
      if (text) { off = next; setLog((l) => l + text) }
      if (status !== 'running') { clearInterval(timer.current); timer.current = null; setRunning(false) }
    }
    timer.current = setInterval(tick, 900); tick()
  }

  const start = () => wrap(async () => {
    if (!username.trim()) throw new Error('Enter the SSH username for these hosts.')
    if (!password) throw new Error('Enter the host password (used once to install the key).')
    if (sel.size === 0) throw new Error('Select at least one host.')
    setLog(''); setRunning(true)
    await api(`inventories/${inv.id}/distribute-key`, { method: 'POST', json: {
      username: username.trim(), password, bastion: bastion.trim(), host_names: [...sel] } })
    poll()
  })

  return (
    <Modal title={`Distribute SSH key — ${inv.name}`} onClose={onClose} wide>
      <div className="muted">SLEP installs its own key on the selected hosts, authenticating once with the password below (through the jump host if set). After that, runs use the key — no stored password. The password is used for this action only; it is never saved.</div>
      <div className="job-split">
        <div className="job-col">
          {pubkey && <div className="mono faint" style={{ fontSize: 11, wordBreak: 'break-all', marginBottom: 6 }}>{pubkey}</div>}
          <div className="row" style={{ gap: 10 }}>
            <Field label="SSH username"><input value={username} autoComplete="off" onChange={(e) => setUsername(e.target.value)} placeholder="e.g. admin" autoFocus /></Field>
            <Field label="Host password (used once)"><input type="password" value={password} autoComplete="off" onChange={(e) => setPassword(e.target.value)} /></Field>
          </div>
          <Field label="Jump host (optional)"><input value={bastion} onChange={(e) => setBastion(e.target.value)} placeholder="user@192.168.8.212" /></Field>
          <div className="faint" style={{ fontSize: 12, margin: '4px 2px' }}>{sel.size} of {hosts.length} host(s) selected</div>
          <div style={{ maxHeight: '34vh', overflow: 'auto', border: '1px solid var(--line)', borderRadius: 8 }}>
            {hosts.map((h) => (
              <label key={h.id} className="row" style={{ gap: 10, padding: '5px 10px', borderBottom: '1px solid var(--line)', cursor: 'pointer' }}>
                <input type="checkbox" checked={sel.has(h.name)} onChange={() => toggle(h.name)} style={{ width: 'auto' }} />
                <b className="mono" style={{ fontSize: 12, minWidth: 130 }}>{h.name}</b>
                <span className="muted mono" style={{ fontSize: 12 }}>{h.address}</span>
              </label>
            ))}
          </div>
          {node}
          <div className="row" style={{ marginTop: 8 }}>
            <div className="spacer" />
            <button className="ghost" onClick={onClose}>{running ? 'Close (keeps running)' : 'Close'}</button>
            <button className="primary" disabled={running} onClick={start}>{running ? 'Distributing…' : `Install key on ${sel.size} host(s)`}</button>
          </div>
        </div>
        <div className="job-col">
          <div className="pane-title">Progress</div>
          <JobLog text={log} empty="The per-host log appears here when you start." />
        </div>
      </div>
    </Modal>
  )
}

function ImportController({ inv, onClose, onDone }) {
  const [controllers, setControllers] = useState(null)   // null = loading
  const [cid, setCid] = useState('')
  const [busy, setBusy] = useState(false)
  const { wrap, node } = useErr()
  useEffect(() => { api('controllers').then((d) => { setControllers(d.controllers); if (d.controllers[0]) setCid(String(d.controllers[0].id)) }) }, [])

  return (
    <Modal title="Import hosts from a Sysible Controller" onClose={onClose}>
      <div className="muted">Pulls the Controller’s agent + SSH hosts into this inventory. Re-importing refreshes, never duplicates.</div>
      {controllers === null ? <div className="muted">Loading connected Controllers…</div>
        : controllers.length === 0
          ? <div className="muted">No Controllers connected yet. Go to the <b>Controllers</b> tab and <b>Connect to Controller</b> first — then import from here.</div>
          : (
            <>
              <Field label="Controller">
                <select value={cid} onChange={(e) => setCid(e.target.value)}>
                  {controllers.map((c) => <option key={c.id} value={c.id}>{c.name} — {c.base_url}</option>)}
                </select>
              </Field>
              {node}
              <button className="primary" disabled={busy} onClick={() => wrap(async () => {
                setBusy(true)
                try {
                  const d = await api(`inventories/${inv.id}/import-controller`, { method: 'POST', json: { controller_id: Number(cid) } })
                  let msg = `Imported ${d.imported} host(s): ${d.agents} agent + ${d.ssh} SSH`
                  if (d.skipped) msg += ` (${d.skipped} skipped)`
                  if (d.errors && d.errors.length) msg += `\n\nNote: ${d.errors.join('; ')}`
                  alert(msg); onDone()
                } finally { setBusy(false) }
              })}>{busy ? 'Importing…' : 'Import'}</button>
            </>
          )}
    </Modal>
  )
}
