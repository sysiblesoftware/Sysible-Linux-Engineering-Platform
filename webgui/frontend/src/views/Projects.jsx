import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

export default function Projects({ onOpen }) {
  const [projects, setProjects] = useState([])
  const [orgs, setOrgs] = useState([])
  const [newOpen, setNewOpen] = useState(false)
  const load = () => api('projects').then((d) => setProjects(d.projects))
  useEffect(() => { load(); api('organizations').then((d) => setOrgs(d.organizations || [])).catch(() => {}) }, [])
  const orgName = (id) => (orgs.find((o) => o.id === id) || {}).name || '—'

  return (
    <>
      <h2>Projects</h2>
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={() => setNewOpen(true)}>+ New project</button>
      </div>
      {projects.length === 0 ? <div className="muted">No projects yet. Create one to start authoring playbooks, Terraform, or Salt states.</div> : (
        <table>
          <thead><tr><th>Name</th><th>Organization</th><th>Slug</th><th>Description</th><th></th></tr></thead>
          <tbody>
            {projects.map((p) => (
              <tr key={p.id}>
                <td><a onClick={() => onOpen(p)}>{p.name}</a></td>
                <td className="muted">{orgName(p.org_id)}</td>
                <td className="muted">{p.slug}</td>
                <td className="muted">{p.description}</td>
                <td className="row">
                  <button className="ghost sm" onClick={() => onOpen(p)}>Open</button>
                  <button className="danger ghost sm" onClick={async () => { if (confirm('Delete project ' + p.name + '?')) { await api('projects/' + p.id, { method: 'DELETE' }); load() } }}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {newOpen && <NewProject orgs={orgs} onClose={() => setNewOpen(false)} onCreated={(p) => { setNewOpen(false); load(); onOpen(p) }} />}
    </>
  )
}

function NewProject({ orgs = [], onClose, onCreated }) {
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [mode, setMode] = useState('blank')   // 'blank' | 'clone'
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  // Only orgs where the user can author (operator or admin) can receive a project.
  const writableOrgs = orgs.filter((o) => o.role === 'operator' || o.role === 'admin')
  const [orgId, setOrgId] = useState(writableOrgs[0] ? String(writableOrgs[0].id) : '')
  const { wrap, node } = useErr()
  const create = () => wrap(async () => {
    setBusy(true)
    try {
      const json = { name: name.trim() || (mode === 'clone' ? repoName(url) : ''), description: desc }
      if (orgId) json.org_id = Number(orgId)
      if (mode === 'clone') { json.clone_url = url.trim(); if (token) json.git_token = token }
      onCreated(await api('projects', { method: 'POST', json }))
    } finally { setBusy(false) }
  })
  return (
    <Modal title="New project" onClose={onClose}>
      {writableOrgs.length > 1 && (
        <Field label="Organization">
          <select value={orgId} onChange={(e) => setOrgId(e.target.value)}>
            {writableOrgs.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
          </select>
        </Field>
      )}
      <div className="row" style={{ gap: 6, marginBottom: 4 }}>
        <button className={'ghost sm' + (mode === 'blank' ? ' active' : '')} onClick={() => setMode('blank')}>Blank</button>
        <button className={'ghost sm' + (mode === 'clone' ? ' active' : '')} onClick={() => setMode('clone')}>Clone a git repo</button>
      </div>
      {mode === 'clone' && (
        <>
          <Field label="Repository URL"><input value={url} autoFocus onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/org/repo.git" /></Field>
          <Field label="Access token (for private repos — optional)"><input type="password" value={token} autoComplete="off" onChange={(e) => setToken(e.target.value)} placeholder="ghp_… / PAT" /></Field>
        </>
      )}
      <Field label={mode === 'clone' ? 'Name (defaults to the repo name)' : 'Name'}><input value={name} autoFocus={mode === 'blank'} onChange={(e) => setName(e.target.value)} placeholder={mode === 'clone' ? repoName(url) : ''} /></Field>
      <Field label="Description"><input value={desc} onChange={(e) => setDesc(e.target.value)} /></Field>
      {node}
      <button className="primary" disabled={busy || (mode === 'clone' && !url.trim())} onClick={create}>{busy ? (mode === 'clone' ? 'Cloning…' : 'Creating…') : (mode === 'clone' ? 'Clone' : 'Create')}</button>
    </Modal>
  )
}

const repoName = (url) => (url || '').trim().replace(/\.git$/, '').replace(/\/$/, '').split('/').pop() || ''
