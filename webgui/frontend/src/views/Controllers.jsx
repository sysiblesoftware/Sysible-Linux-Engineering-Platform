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

// Host-picker import: browse the Controller's hosts, check a subset, and add just
// those to a chosen (or new) inventory. The modal stays open after each add so you
// can route different hosts to different inventories in one sitting.
function ImportModal({ ctrl, onClose, onDone }) {
  const [hosts, setHosts] = useState(null)         // null = loading
  const [sel, setSel] = useState(() => new Set())
  const [q, setQ] = useState('')
  const [invs, setInvs] = useState([])
  const [inv, setInv] = useState('__new')          // default: create a new inventory
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)
  const [flash, setFlash] = useState('')
  const { wrap, node, setErr } = useErr()

  // Load inventories; when at least one exists, target the first by default — with
  // none, stay on '__new' so the create-new field is live (never leave `inv` empty,
  // which would POST to inventories//import-controller and 404).
  const loadInvs = () => api('inventories').then((d) => {
    setInvs(d.inventories)
    setInv((cur) => (cur === '__new' && d.inventories[0]) ? String(d.inventories[0].id) : cur)
  })
  useEffect(() => { loadInvs() }, [])
  useEffect(() => {
    api(`controllers/${ctrl.id}/hosts`).then((d) => setHosts(d.hosts || []))
      .catch((e) => { setHosts([]); setErr(e.message) })
  }, [ctrl.id])

  const needle = q.trim().toLowerCase()
  const shown = (hosts || []).filter((h) => !needle
    || h.name.toLowerCase().includes(needle) || (h.address || '').includes(needle) || (h.groups || '').toLowerCase().includes(needle))
  const toggle = (name) => setSel((s) => { const n = new Set(s); n.has(name) ? n.delete(name) : n.add(name); return n })
  const allShownSelected = shown.length > 0 && shown.every((h) => sel.has(h.name))
  const toggleAll = () => setSel((s) => {
    const n = new Set(s)
    if (allShownSelected) shown.forEach((h) => n.delete(h.name)); else shown.forEach((h) => n.add(h.name))
    return n
  })

  // Distinct Controller environments across the hosts in play (selection if any,
  // else everything) — drives the "one inventory per environment" quick action.
  const inPlay = () => (hosts || []).filter((h) => sel.size === 0 || sel.has(h.name))
  const envGroups = () => {
    const m = new Map()
    for (const h of inPlay()) {
      const env = (h.groups || '').trim() || 'ungrouped'
      if (!m.has(env)) m.set(env, [])
      m.get(env).push(h.name)
    }
    return m
  }
  const envs = [...new Set((hosts || []).map((h) => (h.groups || '').trim()).filter(Boolean))]

  const importTo = async (iid, names) =>
    (await api(`inventories/${iid}/import-controller`, { method: 'POST', json: { controller_id: ctrl.id, host_names: names } })).imported

  const add = () => wrap(async () => {
    if (sel.size === 0) throw new Error('Check one or more hosts first.')
    setBusy(true)
    try {
      let iid = inv
      let invName = invs.find((i) => String(i.id) === String(inv))?.name
      if (inv === '__new' || !iid) {
        if (!newName.trim()) throw new Error('Name the new inventory.')
        const created = await api('inventories', { method: 'POST', json: { name: newName.trim() } })
        iid = created.id; invName = created.name
        setNewName(''); await loadInvs(); setInv(String(iid))
      }
      const imported = await importTo(iid, [...sel])
      setFlash(`Added ${imported} host(s) to “${invName}”. Pick more for another inventory, or close.`)
      setSel(new Set())
      onDone()
    } finally { setBusy(false) }
  })

  // One inventory per Controller environment: create an inventory named after each
  // env (reusing one that already has that name) and import its hosts into it.
  const importPerEnv = () => wrap(async () => {
    const groups = envGroups()
    if (groups.size === 0) throw new Error('No hosts to import.')
    setBusy(true)
    try {
      const byName = new Map(invs.map((i) => [i.name.toLowerCase(), i]))
      let invMade = 0, hostsN = 0
      for (const [env, names] of groups) {
        let target = byName.get(env.toLowerCase())
        if (!target) { target = await api('inventories', { method: 'POST', json: { name: env } }); invMade++ }
        hostsN += await importTo(target.id, names)
      }
      await loadInvs()
      setFlash(`Built ${groups.size} inventory(ies) from environments (${invMade} new) — ${hostsN} host(s) imported.`)
      setSel(new Set())
      onDone()
    } finally { setBusy(false) }
  })

  return (
    <Modal title={`Import hosts from ${ctrl.name}`} onClose={onClose} wide>
      <div className="muted">Check the hosts you want, choose a target inventory, and add them. Route different hosts to different inventories — the list stays up after each add.</div>
      {flash && <div className="ok-text" style={{ color: 'var(--ok,#63c869)', fontSize: 13 }}>{flash}</div>}

      {envs.length > 0 && (
        <div className="row" style={{ gap: 8, marginTop: 6, alignItems: 'center', flexWrap: 'wrap' }}>
          <button className="ghost sm" disabled={busy} onClick={importPerEnv}>⤵ Create inventories per environment</button>
          <span className="faint" style={{ fontSize: 12 }}>
            {sel.size ? `${sel.size} selected` : 'all hosts'} → one inventory each for: {envs.map((e) => <span key={e} className="pill" style={{ fontSize: 11, marginLeft: 4 }}>{e}</span>)}
          </span>
        </div>
      )}

      <div className="row" style={{ gap: 10, marginTop: 8 }}>
        <input placeholder="Filter hosts — name, IP, group…" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: 1 }} />
        <button className="ghost sm" onClick={toggleAll} disabled={shown.length === 0}>{allShownSelected ? 'Clear' : 'Select all'}</button>
      </div>

      <div style={{ maxHeight: '40vh', overflow: 'auto', border: '1px solid var(--line)', borderRadius: 8, marginTop: 8 }}>
        {hosts === null ? <div className="muted" style={{ padding: 10 }}>Loading hosts…</div>
          : shown.length === 0 ? <div className="muted" style={{ padding: 10 }}>No hosts{needle ? ' match your filter' : ' on this Controller'}.</div>
            : shown.map((h) => (
              <label key={h.name} className="row" style={{ gap: 10, padding: '7px 10px', borderBottom: '1px solid var(--line)', cursor: 'pointer' }}>
                <input type="checkbox" checked={sel.has(h.name)} onChange={() => toggle(h.name)} style={{ width: 'auto' }} />
                <b className="mono" style={{ fontSize: 13, minWidth: 140 }}>{h.name}</b>
                <span className="muted mono" style={{ fontSize: 12 }}>{h.address}</span>
                <div className="spacer" />
                {h.groups && <span className="pill" style={{ fontSize: 11 }}>{h.groups}</span>}
                <span className="faint" style={{ fontSize: 11 }}>{h.source}</span>
              </label>
            ))}
      </div>

      <div className="row" style={{ gap: 10, marginTop: 10, alignItems: 'flex-end' }}>
        <Field label={`Add ${sel.size} selected to inventory`}>
          <select value={inv} onChange={(e) => setInv(e.target.value)}>
            {invs.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
            <option value="__new">+ Create a new inventory…</option>
          </select>
        </Field>
        {inv === '__new' && <Field label="New inventory name"><input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="e.g. web-tier" autoFocus /></Field>}
        <button className="primary" disabled={busy || sel.size === 0} onClick={add}>{busy ? 'Adding…' : `Add ${sel.size} →`}</button>
      </div>
      {node}
    </Modal>
  )
}
