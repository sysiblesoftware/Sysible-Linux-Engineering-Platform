import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

export default function Credentials() {
  const [creds, setCreds] = useState([])
  const [open, setOpen] = useState(false)
  const load = () => api('credentials').then((d) => setCreds(d.credentials))
  useEffect(() => { load() }, [])

  return (
    <>
      <h2>Credentials</h2>
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={() => setOpen(true)}>+ New credential</button>
      </div>
      {creds.length === 0 ? <div className="muted">No credentials. Add an SSH key or password for runs to use.</div> : (
        <table>
          <thead><tr><th>Name</th><th>Kind</th><th>Username</th><th></th></tr></thead>
          <tbody>
            {creds.map((c) => (
              <tr key={c.id}>
                <td>{c.name}</td><td className="muted">{c.kind}</td><td className="muted">{c.username}</td>
                <td><button className="danger ghost sm" onClick={async () => { await api('credentials/' + c.id, { method: 'DELETE' }); load() }}>Delete</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && <NewCred onClose={() => setOpen(false)} onDone={() => { setOpen(false); load() }} />}
    </>
  )
}

function NewCred({ onClose, onDone }) {
  const [name, setName] = useState('')
  const [kind, setKind] = useState('ssh')
  const [username, setUsername] = useState('')
  const [secret, setSecret] = useState('')
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
      {node}
      <button className="primary" onClick={() => wrap(async () => { await api('credentials', { method: 'POST', json: { name, kind, username, secret } }); onDone() })}>Save</button>
    </Modal>
  )
}
