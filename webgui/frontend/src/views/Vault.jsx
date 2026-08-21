import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

// The secrets vault: encrypted at rest, referenced from playbooks as
// {{ vault.NAME }}. Values are never returned to the browser once saved.
export default function Vault() {
  const [secrets, setSecrets] = useState([])
  const [open, setOpen] = useState(false)
  const load = () => api('vault').then((d) => setSecrets(d.secrets))
  useEffect(() => { load() }, [])

  return (
    <>
      <h2>Vault</h2>
      <div className="muted" style={{ marginBottom: 10 }}>
        Secrets are encrypted at rest and injected into runs as <span className="mono">{'{{ vault.NAME }}'}</span> — never shown again after you save them.
      </div>
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={() => setOpen(true)}>+ Add secret</button>
      </div>
      {secrets.length === 0 ? <div className="muted">No secrets yet. Add one, then reference it in a playbook as <span className="mono">{'{{ vault.NAME }}'}</span>.</div> : (
        <table>
          <thead><tr><th>Name</th><th>Reference</th><th>Added</th><th></th></tr></thead>
          <tbody>
            {secrets.map((s) => (
              <tr key={s.id}>
                <td>{s.name}</td>
                <td className="mono muted">{'{{ vault.' + s.name + ' }}'}</td>
                <td className="muted">{s.created ? new Date(s.created * 1000).toLocaleString() : ''}</td>
                <td><button className="danger ghost sm" onClick={async () => { if (confirm('Delete secret ' + s.name + '?')) { await api('vault/' + s.id, { method: 'DELETE' }); load() } }}>Delete</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && <AddSecret onClose={() => setOpen(false)} onDone={() => { setOpen(false); load() }} />}
    </>
  )
}

function AddSecret({ onClose, onDone }) {
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const { wrap, node } = useErr()
  return (
    <Modal title="Add secret" onClose={onClose}>
      <Field label="Name (variable)"><input value={name} onChange={(e) => setName(e.target.value)} autoFocus placeholder="e.g. db_password" /></Field>
      <Field label="Value"><textarea rows={4} value={value} onChange={(e) => setValue(e.target.value)} placeholder="the secret value" /></Field>
      <div className="muted">Referenced in playbooks as <span className="mono">{'{{ vault.' + (name || 'NAME') + ' }}'}</span>. Encrypted at rest; you won’t see it again.</div>
      {node}
      <button className="primary" onClick={() => wrap(async () => { await api('vault', { method: 'POST', json: { name, value } }); onDone() })}>Save</button>
    </Modal>
  )
}
