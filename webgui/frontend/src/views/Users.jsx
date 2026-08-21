import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

const ROLE_HELP = {
  viewer: 'Read-only: browse projects, inventories, and run logs.',
  operator: 'Author projects, manage inventories & credentials, launch runs, use the Vault.',
  superuser: 'Everything, incl. managing users and connecting Controllers.',
}

// Superuser-only: manage users and their roles.
export default function Users() {
  const [users, setUsers] = useState([])
  const [roles, setRoles] = useState(['viewer', 'operator', 'superuser'])
  const [open, setOpen] = useState(false)
  const load = () => api('users').then((d) => { setUsers(d.users); if (d.roles) setRoles(d.roles) })
  useEffect(() => { load() }, [])

  const setRoleFor = async (u, role) => {
    try { await api('users/' + encodeURIComponent(u.username), { method: 'PATCH', json: { role } }); load() }
    catch (e) { alert(e.message) }
  }
  return (
    <>
      <h2>Users</h2>
      <div className="muted" style={{ marginBottom: 10 }}>Roles: <b>viewer</b> (read-only) · <b>operator</b> (author + run) · <b>superuser</b> (full + user admin).</div>
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={() => setOpen(true)}>+ Add user</button>
      </div>
      <table>
        <thead><tr><th>Username</th><th>Role</th><th></th></tr></thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.username}>
              <td>{u.username}</td>
              <td>
                <select value={u.role} onChange={(e) => setRoleFor(u, e.target.value)} style={{ maxWidth: 160 }}>
                  {roles.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </td>
              <td className="row">
                <button className="ghost sm" onClick={() => setOpen(u)}>Reset password</button>
                <button className="danger ghost sm" onClick={async () => { if (confirm('Delete user ' + u.username + '?')) { try { await api('users/' + encodeURIComponent(u.username), { method: 'DELETE' }); load() } catch (e) { alert(e.message) } } }}>Delete</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {open && <UserModal reset={open !== true ? open : null} roles={roles} onClose={() => setOpen(false)} onDone={() => { setOpen(false); load() }} />}
    </>
  )
}

function UserModal({ reset, roles, onClose, onDone }) {
  const [username, setUsername] = useState(reset ? reset.username : '')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState(reset ? reset.role : 'operator')
  const { wrap, node } = useErr()
  const editing = !!reset
  return (
    <Modal title={editing ? `Reset password — ${reset.username}` : 'Add user'} onClose={onClose}>
      {!editing && <Field label="Username"><input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus /></Field>}
      <Field label="Password (10+ chars)"><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus={editing} /></Field>
      {!editing && (
        <Field label="Role">
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            {roles.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </Field>
      )}
      {!editing && <div className="muted">{ROLE_HELP[role]}</div>}
      {node}
      <button className="primary" onClick={() => wrap(async () => {
        if (editing) await api('users/' + encodeURIComponent(reset.username), { method: 'PATCH', json: { password } })
        else await api('users', { method: 'POST', json: { username, password, role } })
        onDone()
      })}>{editing ? 'Reset password' : 'Create'}</button>
    </Modal>
  )
}
