import React, { useEffect, useState } from 'react'
import { api, isSuperuser } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

// Organizations — the AAP-style tenant that owns projects, inventories,
// credentials and controllers. System admins (global superusers) can create and
// delete orgs; an org's own admins manage its members and teams. A user's role
// in an org is the highest of their direct grant and any team they belong to.
const ROLES = ['viewer', 'operator', 'admin']

export default function Organizations() {
  const [orgs, setOrgs] = useState([])
  const [sel, setSel] = useState(null)      // selected org id → detail panel
  const [newOpen, setNewOpen] = useState(false)
  const { wrap, node } = useErr()
  const sysadmin = isSuperuser()

  const load = () => api('organizations').then((d) => setOrgs(d.organizations || []))
  useEffect(() => { load() }, [])

  const create = (name, description) => wrap(async () => {
    await api('organizations', { method: 'POST', json: { name, description } })
    setNewOpen(false); load()
  })
  const remove = (o) => wrap(async () => {
    if (!confirm(`Delete organization “${o.name}”? Its projects, inventories, credentials and controllers go with it.`)) return
    await api(`organizations/${o.id}`, { method: 'DELETE' }); if (sel === o.id) setSel(null); load()
  })

  const current = orgs.find((o) => o.id === sel)

  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>Organizations</h2>
        <div className="spacer" />
        {sysadmin && <button className="primary" onClick={() => setNewOpen(true)}>+ New organization</button>}
      </div>
      <div className="muted" style={{ marginBottom: 10 }}>
        An organization owns its projects, inventories, credentials and controllers. Members get a role
        in the org (admin / operator / viewer); teams grant a role to everyone in them.
      </div>
      {node}
      <table>
        <thead><tr><th>Name</th><th>Slug</th><th>Your role</th><th>Members</th><th>Teams</th><th></th></tr></thead>
        <tbody>
          {orgs.map((o) => (
            <tr key={o.id} className={o.id === sel ? 'active-row' : ''}>
              <td><a onClick={() => setSel(o.id)}>{o.name}</a></td>
              <td className="muted">{o.slug}</td>
              <td><span className={'pill role-' + o.role}>{o.role}</span></td>
              <td className="muted">{o.members}</td>
              <td className="muted">{o.teams}</td>
              <td className="row">
                <button className="ghost sm" onClick={() => setSel(o.id)}>Manage</button>
                {sysadmin && o.slug !== 'default' && <button className="danger ghost sm" onClick={() => remove(o)}>Delete</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {current && <OrgDetail org={current} onClose={() => setSel(null)} onChanged={load} />}
      {newOpen && <NewOrg onClose={() => setNewOpen(false)} onCreate={create} />}
    </>
  )
}

function NewOrg({ onClose, onCreate }) {
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  return (
    <Modal title="New organization" onClose={onClose}>
      <Field label="Name"><input value={name} autoFocus onChange={(e) => setName(e.target.value)} /></Field>
      <Field label="Description (optional)"><input value={desc} onChange={(e) => setDesc(e.target.value)} /></Field>
      <div className="row" style={{ marginTop: 12, justifyContent: 'flex-end' }}>
        <button className="ghost" onClick={onClose}>Cancel</button>
        <button className="primary" disabled={!name.trim()} onClick={() => onCreate(name.trim(), desc.trim())}>Create</button>
      </div>
    </Modal>
  )
}

function OrgDetail({ org, onClose, onChanged }) {
  const [members, setMembers] = useState([])
  const [teams, setTeams] = useState([])
  const [users, setUsers] = useState([])          // for the add-member picker (system admin only)
  const [addUser, setAddUser] = useState('')
  const [addRole, setAddRole] = useState('viewer')
  const [teamName, setTeamName] = useState('')
  const [teamRole, setTeamRole] = useState('viewer')
  const { wrap, node } = useErr()
  const canManage = org.role === 'admin'

  const load = () => {
    api(`organizations/${org.id}/members`).then((d) => setMembers(d.members || []))
    api(`organizations/${org.id}/teams`).then((d) => setTeams(d.teams || []))
    if (isSuperuser()) api('users').then((d) => setUsers((d.users || []).map((u) => u.username))).catch(() => {})
  }
  useEffect(() => { load() }, [org.id])

  const setMember = (username, role) => wrap(async () => {
    await api(`organizations/${org.id}/members`, { method: 'POST', json: { username, role } }); load(); onChanged()
  })
  const removeMember = (username) => wrap(async () => {
    await api(`organizations/${org.id}/members/${username}`, { method: 'DELETE' }); load(); onChanged()
  })
  const createTeam = () => wrap(async () => {
    await api(`organizations/${org.id}/teams`, { method: 'POST', json: { name: teamName.trim(), org_role: teamRole } })
    setTeamName(''); load(); onChanged()
  })

  return (
    <Modal title={`Organization — ${org.name}`} onClose={onClose} wide>
      {node}
      <h3 style={{ marginTop: 0 }}>Members</h3>
      {!canManage && <div className="muted" style={{ marginBottom: 8 }}>You need the admin role in this organization to change membership.</div>}
      <table>
        <thead><tr><th>User</th><th>Role</th><th></th></tr></thead>
        <tbody>
          {members.map((m) => (
            <tr key={m.username}>
              <td>{m.username}</td>
              <td>
                {canManage ? (
                  <select value={m.role} onChange={(e) => setMember(m.username, e.target.value)}>
                    {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                ) : <span className={'pill role-' + m.role}>{m.role}</span>}
              </td>
              <td>{canManage && <button className="danger ghost sm" onClick={() => removeMember(m.username)}>Remove</button>}</td>
            </tr>
          ))}
          {members.length === 0 && <tr><td colSpan="3" className="muted">No members yet.</td></tr>}
        </tbody>
      </table>
      {canManage && (
        <div className="row" style={{ gap: 8, marginTop: 8, alignItems: 'flex-end' }}>
          <Field label="Add member">
            {isSuperuser()
              ? <select value={addUser} onChange={(e) => setAddUser(e.target.value)}>
                  <option value="">— pick a user —</option>
                  {users.map((u) => <option key={u} value={u}>{u}</option>)}
                </select>
              : <input value={addUser} placeholder="username" onChange={(e) => setAddUser(e.target.value)} />}
          </Field>
          <Field label="Role">
            <select value={addRole} onChange={(e) => setAddRole(e.target.value)}>
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </Field>
          <button className="primary sm" disabled={!addUser} onClick={() => { setMember(addUser, addRole); setAddUser('') }}>Add</button>
        </div>
      )}

      <h3>Teams</h3>
      <table>
        <thead><tr><th>Team</th><th>Confers role</th><th>Members</th><th></th></tr></thead>
        <tbody>
          {teams.map((t) => <TeamRow key={t.id} team={t} orgRole={org.role} onChanged={() => { load(); onChanged() }} />)}
          {teams.length === 0 && <tr><td colSpan="4" className="muted">No teams yet.</td></tr>}
        </tbody>
      </table>
      {canManage && (
        <div className="row" style={{ gap: 8, marginTop: 8, alignItems: 'flex-end' }}>
          <Field label="New team"><input value={teamName} placeholder="e.g. Platform" onChange={(e) => setTeamName(e.target.value)} /></Field>
          <Field label="Confers role">
            <select value={teamRole} onChange={(e) => setTeamRole(e.target.value)}>
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </Field>
          <button className="primary sm" disabled={!teamName.trim()} onClick={createTeam}>Create team</button>
        </div>
      )}
    </Modal>
  )
}

function TeamRow({ team, orgRole, onChanged }) {
  const [open, setOpen] = useState(false)
  const [mem, setMem] = useState([])
  const [add, setAdd] = useState('')
  const { wrap, node } = useErr()
  const canManage = orgRole === 'admin'
  const loadMem = () => api(`teams/${team.id}/members`).then((d) => setMem(d.members || []))
  useEffect(() => { if (open) loadMem() }, [open])

  const del = () => wrap(async () => {
    if (!confirm(`Delete team “${team.name}”?`)) return
    await api(`teams/${team.id}`, { method: 'DELETE' }); onChanged()
  })
  const addMem = () => wrap(async () => { await api(`teams/${team.id}/members`, { method: 'POST', json: { username: add.trim() } }); setAdd(''); loadMem(); onChanged() })
  const rmMem = (u) => wrap(async () => { await api(`teams/${team.id}/members/${u}`, { method: 'DELETE' }); loadMem(); onChanged() })

  return (
    <>
      <tr>
        <td><a onClick={() => setOpen((o) => !o)}>{open ? '▾ ' : '▸ '}{team.name}</a></td>
        <td><span className={'pill role-' + team.org_role}>{team.org_role}</span></td>
        <td className="muted">{team.members}</td>
        <td>{canManage && <button className="danger ghost sm" onClick={del}>Delete</button>}</td>
      </tr>
      {open && (
        <tr><td colSpan="4">
          {node}
          <div className="muted" style={{ marginBottom: 6 }}>Members inherit the “{team.org_role}” role in this org.</div>
          {mem.map((u) => (
            <span key={u} className="chip" style={{ marginRight: 6 }}>{u}
              {canManage && <button className="chip-x" onClick={() => rmMem(u)}>×</button>}</span>
          ))}
          {mem.length === 0 && <span className="muted">No members.</span>}
          {canManage && (
            <div className="row" style={{ gap: 8, marginTop: 8 }}>
              <input value={add} placeholder="username" onChange={(e) => setAdd(e.target.value)} style={{ maxWidth: 220 }} />
              <button className="primary sm" disabled={!add.trim()} onClick={addMem}>Add member</button>
            </div>
          )}
        </td></tr>
      )}
    </>
  )
}
