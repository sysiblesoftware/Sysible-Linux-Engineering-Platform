import React, { useEffect, useState } from 'react'
import { api, canWrite } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

// Reusable SSH jump hosts (bastions): define once, prepare once (SLEP installs its
// managed key on them), then pick one from a project's Access instead of retyping
// user@host. Same shape as Credentials — an address book for jump hosts.
export default function JumpHosts() {
  const [rows, setRows] = useState([])
  const [add, setAdd] = useState(false)
  const [prep, setPrep] = useState(null)
  const load = () => api('jump-hosts').then((d) => setRows(d.jump_hosts || [])).catch(() => {})
  useEffect(() => { load() }, [])
  const writable = canWrite()
  return (
    <>
      <h2>Jump Hosts</h2>
      <div className="muted" style={{ marginBottom: 10 }}>
        SSH jump hosts (bastions) SLEP hops through to reach VMs on a private network — usually the hypervisor.
        Define one here, <b>Prepare</b> it once (installs SLEP’s key), then pick it in a project’s <b>Access</b>.
      </div>
      {writable && <div className="row" style={{ marginBottom: 12 }}><button className="primary" onClick={() => setAdd(true)}>+ Add jump host</button></div>}
      {rows.length === 0 ? <div className="muted">No jump hosts yet. Add one, then select it in a project’s Access.</div> : (
        <table>
          <thead><tr><th>Name</th><th>Address</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {rows.map((j) => (
              <tr key={j.id}>
                <td>{j.name}</td>
                <td className="mono muted">{j.bastion}</td>
                <td>{j.prepared
                  ? <span style={{ color: 'var(--green-bright)' }}>✓ key installed</span>
                  : <span className="faint">not prepared</span>}</td>
                <td style={{ textAlign: 'right' }}>
                  {writable && <button className="ghost sm" onClick={() => setPrep(j)}>{j.prepared ? 'Re-prepare' : 'Prepare'}</button>}
                  {writable && <button className="danger ghost sm" style={{ marginLeft: 6 }}
                    onClick={async () => { if (confirm(`Delete jump host “${j.name}”?`)) { await api('jump-hosts/' + j.id, { method: 'DELETE' }); load() } }}>Delete</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {add && <AddJumpHost onClose={() => setAdd(false)} onDone={() => { setAdd(false); load() }} />}
      {prep && <PrepareJump j={prep} onClose={() => setPrep(null)} onDone={() => { setPrep(null); load() }} />}
    </>
  )
}

function AddJumpHost({ onClose, onDone }) {
  const [name, setName] = useState('')
  const [host, setHost] = useState('')
  const [username, setUsername] = useState('admin')
  const [port, setPort] = useState('22')
  const { wrap, node } = useErr()
  return (
    <Modal title="Add jump host" onClose={onClose}>
      <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} autoFocus placeholder="hypervisor-1" /></Field>
      <Field label="Host (address)"><input value={host} onChange={(e) => setHost(e.target.value)} placeholder="192.168.8.212" /></Field>
      <Field label="SSH username"><input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="admin" /></Field>
      <Field label="Port"><input value={port} onChange={(e) => setPort(e.target.value)} placeholder="22" /></Field>
      {node}
      <div className="row" style={{ marginTop: 12, justifyContent: 'flex-end' }}>
        <button className="ghost sm" onClick={onClose}>Cancel</button>
        <button className="primary sm" onClick={() => wrap(async () => {
          await api('jump-hosts', { method: 'POST', json: { name, host, username, port: Number(port) || 22 } }); onDone()
        })}>Save</button>
      </div>
    </Modal>
  )
}

function PrepareJump({ j, onClose, onDone }) {
  const [pw, setPw] = useState('')
  const [busy, setBusy] = useState(false)
  const [res, setRes] = useState(null)
  const { wrap, node } = useErr()
  return (
    <Modal title={`Prepare ${j.name}`} onClose={onClose}>
      <p className="muted" style={{ marginTop: 0 }}>
        Installs SLEP’s key on <span className="mono">{j.bastion}</span> with a one-time password (used once, never saved), so runs hop through it with the key.
      </p>
      <Field label="Host password (used once)"><input type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="off" /></Field>
      {node}
      <div className="row" style={{ marginTop: 12, justifyContent: 'flex-end' }}>
        <button className="ghost sm" onClick={onClose}>Close</button>
        <button className="primary sm" disabled={busy || !pw} onClick={() => wrap(async () => {
          setBusy(true); setRes(null)
          try {
            const d = await api(`jump-hosts/${j.id}/prepare`, { method: 'POST', json: { password: pw } })
            setRes(d); if (d.ok) setTimeout(onDone, 900)
          } finally { setBusy(false) }
        })}>{busy ? 'Preparing…' : 'Prepare'}</button>
      </div>
      {res && <div style={{ marginTop: 12, fontSize: 13, whiteSpace: 'pre-wrap', color: res.ok ? 'var(--green-bright)' : 'var(--danger)' }}>{res.ok ? '✓ ' : '✗ '}{res.detail || res.output || (res.ok ? 'Key installed.' : 'Could not install the key.')}</div>}
    </Modal>
  )
}
