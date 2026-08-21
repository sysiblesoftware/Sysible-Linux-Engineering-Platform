import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

export default function Projects({ onOpen }) {
  const [projects, setProjects] = useState([])
  const [newOpen, setNewOpen] = useState(false)
  const load = () => api('projects').then((d) => setProjects(d.projects))
  useEffect(() => { load() }, [])

  return (
    <>
      <h2>Projects</h2>
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={() => setNewOpen(true)}>+ New project</button>
      </div>
      {projects.length === 0 ? <div className="muted">No projects yet. Create one to start authoring playbooks, Terraform, or Salt states.</div> : (
        <table>
          <thead><tr><th>Name</th><th>Slug</th><th>Description</th><th></th></tr></thead>
          <tbody>
            {projects.map((p) => (
              <tr key={p.id}>
                <td><a onClick={() => onOpen(p)}>{p.name}</a></td>
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
      {newOpen && <NewProject onClose={() => setNewOpen(false)} onCreated={(p) => { setNewOpen(false); onOpen(p) }} />}
    </>
  )
}

function NewProject({ onClose, onCreated }) {
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [mode, setMode] = useState('blank')   // 'blank' | 'clone'
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const { wrap, node } = useErr()
  const create = () => wrap(async () => {
    setBusy(true)
    try {
      const json = { name: name.trim() || (mode === 'clone' ? repoName(url) : ''), description: desc }
      if (mode === 'clone') { json.clone_url = url.trim(); if (token) json.git_token = token }
      onCreated(await api('projects', { method: 'POST', json }))
    } finally { setBusy(false) }
  })
  return (
    <Modal title="New project" onClose={onClose}>
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
