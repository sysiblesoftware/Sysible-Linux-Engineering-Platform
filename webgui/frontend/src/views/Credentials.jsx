import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

export default function Credentials() {
  const [creds, setCreds] = useState([])
  const [open, setOpen] = useState(false)
  const [sudoFor, setSudoFor] = useState(null)
  const load = () => api('credentials').then((d) => setCreds(d.credentials))
  useEffect(() => { load() }, [])

  return (
    <>
      <h2>Credentials</h2>
      <ManagedKeyPanel onChanged={load} />
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={() => setOpen(true)}>+ New credential</button>
      </div>
      {creds.length === 0 ? <div className="muted">No credentials. Add an SSH key or password for runs to use.</div> : (
        <table>
          <thead><tr><th>Name</th><th>Kind</th><th>Username</th><th>Sudo</th><th></th></tr></thead>
          <tbody>
            {creds.map((c) => (
              <tr key={c.id}>
                <td>{c.name}</td><td className="muted">{c.kind}</td><td className="muted">{c.username}</td>
                <td>{c.has_become ? <span className="pill ok">sudo ✓</span> : <span className="faint">—</span>}</td>
                <td className="row">
                  <button className="ghost sm" onClick={() => setSudoFor(c)}>{c.has_become ? 'Change sudo' : 'Set sudo'}</button>
                  <button className="danger ghost sm" onClick={async () => { await api('credentials/' + c.id, { method: 'DELETE' }); load() }}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && <NewCred onClose={() => setOpen(false)} onDone={() => { setOpen(false); load() }} />}
      {sudoFor && <SetSudo cred={sudoFor} onClose={() => setSudoFor(null)} onDone={() => { setSudoFor(null); load() }} />}
    </>
  )
}

// SLEP's ONE managed key — the key baked into VMs it builds, used for the jump
// hop and the hypervisor connection. Shows its fingerprint (so you can tell the
// current key from a stale one on old VMs), and lets you RESET it (mint a fresh
// key when the deployed one drifted — "none of the SLEP keys work") or REMOVE it.
function ManagedKeyPanel({ onChanged }) {
  const [info, setInfo] = useState(null)
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState('')
  const load = () => api('infra/managed-key').then(setInfo).catch(() => setInfo({ exists: false }))
  useEffect(() => { load() }, [])   // eslint-disable-line
  const regen = async () => {
    if (!window.confirm('Reset SLEP’s managed key?\n\nA brand-new key is generated and the OLD one stops working immediately. You must then re-install it on your hypervisor(s) (Install key with password) and re-apply so VMs bake in the new key.')) return
    setBusy('regen'); setMsg('')
    try { const d = await api('infra/managed-key/regenerate', { method: 'POST' }); setMsg(d.note || 'Key regenerated.'); await load(); onChanged && onChanged() }
    catch (e) { setMsg('Error: ' + (e.message || e)) } finally { setBusy('') }
  }
  const remove = async () => {
    if (!window.confirm('Remove SLEP’s managed key and its credential?\n\nSLEP will mint a fresh one the next time it needs a key. Deployed copies (hypervisor authorized_keys, VM cloud-init) are not touched.')) return
    setBusy('remove'); setMsg('')
    try { await api('infra/managed-key', { method: 'DELETE' }); setMsg('Removed. A fresh key will be created on next use.'); await load(); onChanged && onChanged() }
    catch (e) { setMsg('Error: ' + (e.message || e)) } finally { setBusy('') }
  }
  if (!info) return null
  return (
    <div style={{ border: '1px solid var(--line)', borderRadius: 10, padding: 12, margin: '4px 0 14px' }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <div>
          <b style={{ fontSize: 13 }}>🔑 SLEP managed key</b>
          <div className="faint" style={{ fontSize: 12, marginTop: 2 }}>
            The one key SLEP bakes into the VMs it builds and uses for the jump hop / hypervisor connection.
          </div>
          <div className="mono" style={{ fontSize: 11.5, marginTop: 4 }}>
            {info.exists
              ? (info.fingerprint || info.public_key || '(key present)')
              : <span style={{ color: 'var(--danger)' }}>no key on disk — one will be created on next use</span>}
            {info.exists && info.credential_id == null && <span style={{ color: 'var(--danger)' }}> · no matching credential</span>}
          </div>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <button className="ghost sm" disabled={busy === 'regen'} title="Mint a brand-new key (resets SLEP’s identity)" onClick={regen}>{busy === 'regen' ? 'Resetting…' : '↻ Reset key'}</button>
          <button className="danger ghost sm" disabled={busy === 'remove'} title="Remove the key + its credential" onClick={remove}>{busy === 'remove' ? 'Removing…' : 'Remove'}</button>
        </div>
      </div>
      {msg && <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>{msg}</div>}
    </div>
  )
}

// Set (or clear) a credential's sudo/become password — so a key credential like
// "SLEP managed key" can run `become` tasks against password-sudo accounts.
function SetSudo({ cred, onClose, onDone }) {
  const [pw, setPw] = useState('')
  const { wrap, node } = useErr()
  return (
    <Modal title={`Sudo password — ${cred.name}`} onClose={onClose}>
      <div className="muted">Stored encrypted and used for <span className="mono">become</span> (sudo) during runs. Leave empty and save to clear it. Never shown again.</div>
      <Field label="Sudo (become) password"><input type="password" value={pw} autoComplete="off" autoFocus onChange={(e) => setPw(e.target.value)} /></Field>
      {node}
      <button className="primary" onClick={() => wrap(async () => { await api('credentials/' + cred.id, { method: 'PATCH', json: { become_password: pw } }); onDone() })}>Save</button>
    </Modal>
  )
}

function NewCred({ onClose, onDone }) {
  const [name, setName] = useState('')
  const [kind, setKind] = useState('ssh')
  const [username, setUsername] = useState('')
  const [secret, setSecret] = useState('')
  const [become, setBecome] = useState('')
  const { wrap, node } = useErr()
  const hint = {
    ssh: 'Paste the OpenSSH private key.',
    ssh_password: 'The SSH (and sudo) password.',
    cloud: 'KEY=VALUE lines injected as env for Terraform (e.g. AWS_ACCESS_KEY_ID=…).',
  }[kind]
  return (
    <Modal title="New credential" onClose={onClose}>
      <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} autoFocus /></Field>
      <Field label="Kind">
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="ssh">SSH private key</option>
          <option value="ssh_password">SSH password</option>
          <option value="cloud">Cloud / env (Terraform)</option>
        </select>
      </Field>
      {kind !== 'cloud' && <Field label="Username"><input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="e.g. ansible" /></Field>}
      <Field label="Secret"><textarea rows={6} value={secret} onChange={(e) => setSecret(e.target.value)} placeholder={hint} /></Field>
      <div className="muted">{hint} Stored server-side and injected into runs — never shown again.</div>
      {kind === 'ssh' && (
        <Field label="Sudo (become) password — optional">
          <input type="password" value={become} autoComplete="off" onChange={(e) => setBecome(e.target.value)}
                 placeholder="only if become tasks need a sudo password" />
        </Field>
      )}
      {node}
      <button className="primary" onClick={() => wrap(async () => { await api('credentials', { method: 'POST', json: { name, kind, username, secret, become_password: become } }); onDone() })}>Save</button>
    </Modal>
  )
}
