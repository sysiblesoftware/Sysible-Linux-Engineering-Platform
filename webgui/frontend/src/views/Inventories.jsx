import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

export default function Inventories() {
  const [invs, setInvs] = useState([])
  const [selected, setSelected] = useState(null)   // inventory id being drilled into
  const [newOpen, setNewOpen] = useState(false)
  const load = () => api('inventories').then((d) => setInvs(d.inventories))
  useEffect(() => { load() }, [])

  const sel = invs.find((i) => i.id === selected)
  if (sel) return <InventoryDetail inv={sel} onBack={() => { setSelected(null); load() }} onChanged={load} />

  return (
    <>
      <h2>Inventories</h2>
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={() => setNewOpen(true)}>+ New inventory</button>
      </div>
      {invs.length === 0 ? <div className="muted">No inventories. Create one, then add hosts or import from a Sysible Controller.</div> : (
        <table>
          <thead><tr><th>Name</th><th>Source</th><th>Jump host</th><th></th></tr></thead>
          <tbody>
            {invs.map((inv) => (
              <tr key={inv.id}>
                <td><a onClick={() => setSelected(inv.id)}>{inv.name}</a></td>
                <td><span className="pill">{inv.source}</span></td>
                <td className="mono muted">{inv.bastion || '—'}</td>
                <td className="row">
                  <button className="ghost sm" onClick={() => setSelected(inv.id)}>Open →</button>
                  <button className="danger ghost sm" onClick={async () => { if (confirm('Delete inventory ' + inv.name + '?')) { await api('inventories/' + inv.id, { method: 'DELETE' }); load() } }}>Delete</button>
                </td>
              </tr>
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
        {inv.bastion && <span className="pill" title="Runs tunnel through this SSH jump host">⤳ {inv.bastion}</span>}
        <div className="spacer" />
        <button className="ghost sm" onClick={() => setModal('bastion')}>{inv.bastion ? 'Jump host' : '+ Jump host'}</button>
        <button className="ghost sm" onClick={() => setModal('import')}>Import from Controller</button>
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
      </div>
    </div>
  )
}

function NewInventory({ onClose, onDone }) {
  const [name, setName] = useState('')
  const [bastion, setBastion] = useState('')
  const { wrap, node } = useErr()
  return (
    <Modal title="New inventory" onClose={onClose}>
      <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} autoFocus placeholder="e.g. production" /></Field>
      <Field label="SSH jump host / bastion (optional)"><input value={bastion} onChange={(e) => setBastion(e.target.value)} placeholder="user@192.168.8.212  — reach hosts through this box" /></Field>
      <div className="muted">Set this when the hosts aren’t directly reachable (e.g. VMs on a hypervisor’s internal network). Runs tunnel SSH through it (ProxyJump).</div>
      {node}
      <button className="primary" onClick={() => wrap(async () => { await api('inventories', { method: 'POST', json: { name, bastion } }); onDone() })}>Create</button>
    </Modal>
  )
}

function SetBastion({ inv, onClose, onDone }) {
  const [bastion, setBastion] = useState(inv.bastion || '')
  const { wrap, node } = useErr()
  return (
    <Modal title={`Jump host for ${inv.name}`} onClose={onClose}>
      <div className="muted">SSH bastion to reach this inventory’s hosts through (ProxyJump). Leave empty for direct connections.</div>
      <Field label="Jump host"><input value={bastion} onChange={(e) => setBastion(e.target.value)} autoFocus placeholder="user@192.168.8.212" /></Field>
      {node}
      <button className="primary" onClick={() => wrap(async () => { await api('inventories/' + inv.id, { method: 'PATCH', json: { bastion } }); onDone() })}>Save</button>
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
