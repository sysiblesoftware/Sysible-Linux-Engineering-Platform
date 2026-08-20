import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

export default function Inventories() {
  const [invs, setInvs] = useState([])
  const [newOpen, setNewOpen] = useState(false)
  const load = () => api('inventories').then((d) => setInvs(d.inventories))
  useEffect(() => { load() }, [])

  return (
    <>
      <h2>Inventories</h2>
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={() => setNewOpen(true)}>+ New inventory</button>
      </div>
      {invs.length === 0 ? <div className="muted">No inventories. Create one, then add hosts or import from a Sysible Controller.</div>
        : invs.map((inv) => <InventoryCard key={inv.id} inv={inv} onChanged={load} />)}
      {newOpen && <NewInventory onClose={() => setNewOpen(false)} onDone={() => { setNewOpen(false); load() }} />}
    </>
  )
}

function InventoryCard({ inv, onChanged }) {
  const [hosts, setHosts] = useState([])
  const [modal, setModal] = useState(null)
  const load = () => api(`inventories/${inv.id}/hosts`).then((d) => setHosts(d.hosts))
  useEffect(() => { load() }, [inv.id])
  return (
    <div className="card col" style={{ marginBottom: 12 }}>
      <div className="row">
        <b>{inv.name}</b><span className="pill">{inv.source}</span><span className="muted">{hosts.length} host(s)</span>
        <div className="spacer" />
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
    </div>
  )
}

function NewInventory({ onClose, onDone }) {
  const [name, setName] = useState('')
  const { wrap, node } = useErr()
  return (
    <Modal title="New inventory" onClose={onClose}>
      <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} autoFocus placeholder="e.g. production" /></Field>
      {node}
      <button className="primary" onClick={() => wrap(async () => { await api('inventories', { method: 'POST', json: { name } }); onDone() })}>Create</button>
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
  const [url, setUrl] = useState(''); const [key, setKey] = useState(''); const [busy, setBusy] = useState(false)
  const { wrap, node } = useErr()
  return (
    <Modal title="Import hosts from a Sysible Controller" onClose={onClose}>
      <div className="muted">Pulls the Controller’s /agents fleet into this inventory. Re-importing refreshes, never duplicates.</div>
      <Field label="Controller URL"><input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://controller-host:9000" autoFocus /></Field>
      <Field label="Backend API key"><input type="password" value={key} onChange={(e) => setKey(e.target.value)} /></Field>
      {node}
      <button className="primary" disabled={busy} onClick={() => wrap(async () => {
        setBusy(true)
        try { const d = await api(`inventories/${inv.id}/import-controller`, { method: 'POST', json: { controller_url: url, api_key: key } }); alert(`Imported ${d.imported} host(s) (${d.skipped} skipped of ${d.total}).`); onDone() }
        finally { setBusy(false) }
      })}>{busy ? 'Importing…' : 'Import'}</button>
    </Modal>
  )
}
