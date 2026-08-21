import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

// "Connect to Controller" — register a Sysible Controller once (validated + key
// stored server-side), then import its fleet into any inventory with one click.
export default function Controllers() {
  const [controllers, setControllers] = useState([])
  const [connectOpen, setConnectOpen] = useState(false)
  const load = () => api('controllers').then((d) => setControllers(d.controllers))
  useEffect(() => { load() }, [])

  return (
    <>
      <h2>Controllers</h2>
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={() => setConnectOpen(true)}>+ Connect to Controller</button>
      </div>
      {controllers.length === 0
        ? <div className="muted">No Controllers connected. Connect one to import its fleet as inventory — sign in with a Controller <b>superuser</b> username &amp; password (SLEP fetches the key for you). No key-hunting; the raw backend API key still works as a fallback.</div>
        : controllers.map((c) => <ControllerCard key={c.id} ctrl={c} onChanged={load} />)}
      {connectOpen && <ConnectModal onClose={() => setConnectOpen(false)} onDone={() => { setConnectOpen(false); load() }} />}
    </>
  )
}

function ControllerCard({ ctrl, onChanged }) {
  const [status, setStatus] = useState(null)   // {agents, ssh, total} | {error}
  const [busy, setBusy] = useState(false)
  const [importOpen, setImportOpen] = useState(false)

  const test = async () => {
    setBusy(true); setStatus(null)
    try { const d = await api(`controllers/${ctrl.id}/test`, { method: 'POST' }); setStatus(d) }
    catch (e) { setStatus({ error: e.message }) }
    finally { setBusy(false) }
  }
  return (
    <div className="card col" style={{ marginBottom: 12 }}>
      <div className="row">
        <b>{ctrl.name}</b>
        <span className="mono muted">{ctrl.base_url}</span>
        {status && !status.error && <span className="pill ok">reachable · {status.total} host(s)</span>}
        {status && status.error && <span className="pill failed">unreachable</span>}
        <div className="spacer" />
        <button className="ghost sm" disabled={busy} onClick={test}>{busy ? 'Testing…' : 'Test'}</button>
        <button className="ghost sm" onClick={() => setImportOpen(true)}>Import hosts →</button>
        <button className="danger ghost sm" onClick={async () => { if (confirm('Disconnect ' + ctrl.name + '?')) { await api('controllers/' + ctrl.id, { method: 'DELETE' }); onChanged() } }}>Disconnect</button>
      </div>
      {status && !status.error && <div className="muted">{status.agents} agent + {status.ssh} SSH host(s) available to import.</div>}
      {status && status.error && <div className="err">{status.error}</div>}
      {ctrl.last_import ? <div className="faint">Last import: {new Date(ctrl.last_import * 1000).toLocaleString()}</div> : null}
      {importOpen && <ImportModal ctrl={ctrl} onClose={() => setImportOpen(false)} onDone={() => { setImportOpen(false); onChanged() }} />}
    </div>
  )
}

function ConnectModal({ onClose, onDone }) {
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [mode, setMode] = useState('creds')   // 'creds' | 'key'
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [totp, setTotp] = useState('')
  const [mfa, setMfa] = useState(false)        // Controller asked for a second factor
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const { wrap, node, setErr } = useErr()

  const connect = () => wrap(async () => {
    setBusy(true)
    try {
      const json = mode === 'key'
        ? { name, base_url: url, api_key: key }
        : { name, base_url: url, username, password, totp_code: totp }
      const d = await api('controllers', { method: 'POST', json })
      if (d.status === 'mfa_required') { setMfa(true); setErr(d.detail || 'Enter your authentication code.'); return }
      alert(`Connected to ${d.controller.name} — ${d.total} host(s) visible (${d.agents} agent + ${d.ssh} SSH).`)
      onDone()
    } finally { setBusy(false) }
  })

  return (
    <Modal title="Connect to a Sysible Controller" onClose={onClose}>
      <div className="muted">Sign in as a Controller <b>superuser</b> and SLEP fetches the connection key for you — then stores it server-side (never shown again) so imports don’t need it re-entered.</div>
      <div className="row" style={{ gap: 6, marginTop: 4 }}>
        <button className={'ghost sm' + (mode === 'creds' ? ' active' : '')} onClick={() => setMode('creds')}>Username &amp; password</button>
        <button className={'ghost sm' + (mode === 'key' ? ' active' : '')} onClick={() => setMode('key')}>API key</button>
      </div>
      <Field label="Name (optional)"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Production Controller" autoFocus /></Field>
      <Field label="Controller URL"><input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://controller-host:9000" /></Field>
      {mode === 'creds' ? (
        <>
          <Field label="Superuser username"><input value={username} autoComplete="off" onChange={(e) => setUsername(e.target.value)} /></Field>
          <Field label="Password"><input type="password" value={password} autoComplete="off" onChange={(e) => setPassword(e.target.value)} /></Field>
          {mfa && <Field label="Authentication code (MFA)"><input value={totp} autoFocus onChange={(e) => setTotp(e.target.value)} placeholder="6-digit code" /></Field>}
        </>
      ) : (
        <Field label="Backend API key"><input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder="host: /opt/sysible/api_key.txt · docker: /opt/sysible/secrets/api_key.txt" /></Field>
      )}
      {node}
      <button className="primary" disabled={busy} onClick={connect}>{busy ? 'Connecting…' : 'Connect'}</button>
    </Modal>
  )
}

function ImportModal({ ctrl, onClose, onDone }) {
  const [invs, setInvs] = useState([])
  const [inv, setInv] = useState('')
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)
  const { wrap, node } = useErr()
  useEffect(() => { api('inventories').then((d) => { setInvs(d.inventories); if (d.inventories[0]) setInv(String(d.inventories[0].id)) }) }, [])
  return (
    <Modal title={`Import hosts from ${ctrl.name}`} onClose={onClose}>
      <div className="muted">Pulls the Controller’s agent + SSH hosts into the chosen inventory. Idempotent — re-importing refreshes, never duplicates.</div>
      <Field label="Target inventory">
        <select value={inv} onChange={(e) => setInv(e.target.value)}>
          {invs.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
          <option value="__new">+ Create a new inventory…</option>
        </select>
      </Field>
      {inv === '__new' && <Field label="New inventory name"><input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="e.g. imported-fleet" autoFocus /></Field>}
      {node}
      <button className="primary" disabled={busy} onClick={() => wrap(async () => {
        setBusy(true)
        try {
          let iid = inv
          if (inv === '__new') { const created = await api('inventories', { method: 'POST', json: { name: newName || 'imported-fleet' } }); iid = created.id }
          const d = await api(`inventories/${iid}/import-controller`, { method: 'POST', json: { controller_id: ctrl.id } })
          let msg = `Imported ${d.imported} host(s): ${d.agents} agent + ${d.ssh} SSH`
          if (d.skipped) msg += ` (${d.skipped} skipped)`
          if (d.errors && d.errors.length) msg += `\n\nNote: ${d.errors.join('; ')}`
          alert(msg); onDone()
        } finally { setBusy(false) }
      })}>{busy ? 'Importing…' : 'Import'}</button>
    </Modal>
  )
}
