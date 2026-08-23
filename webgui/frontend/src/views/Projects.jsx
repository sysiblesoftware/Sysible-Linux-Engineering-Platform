import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

// Projects are the top-level unit; each can nest sub-projects (folders) via
// parent_id, rendered here as an expandable tree. Organizations still exist for
// access control behind the scenes (shown as a column only when you can see
// more than one).
export default function Projects({ onOpen }) {
  const [projects, setProjects] = useState([])
  const [orgs, setOrgs] = useState([])
  const [newFor, setNewFor] = useState(undefined)   // undefined=closed; null=top-level; id=sub-project parent
  const [collapsed, setCollapsed] = useState({})    // {projectId: true} → children hidden
  const { wrap, node } = useErr()

  const load = () => api('projects').then((d) => setProjects(d.projects))
  useEffect(() => { load(); api('organizations').then((d) => setOrgs(d.organizations || [])).catch(() => {}) }, [])
  const orgName = (id) => (orgs.find((o) => o.id === id) || {}).name || '—'
  const multiOrg = orgs.length > 1

  const childrenOf = (pid) => projects.filter((p) => (p.parent_id ?? null) === pid)
  const roots = projects.filter((p) => !p.parent_id)

  // Descendant ids of p (to keep Move targets from creating a cycle).
  const descendants = (pid, acc = new Set()) => {
    for (const c of childrenOf(pid)) { acc.add(c.id); descendants(c.id, acc) }
    return acc
  }
  const move = (p, parent_id) => wrap(async () => {
    await api(`projects/${p.id}`, { method: 'PATCH', json: { parent_id: parent_id } }); load()
  })
  const del = (p) => wrap(async () => {
    const kids = childrenOf(p.id).length
    const msg = kids ? `Delete “${p.name}”? Its ${kids} sub-project(s) move up to its parent.` : `Delete “${p.name}”?`
    if (!confirm(msg)) return
    await api('projects/' + p.id, { method: 'DELETE' }); load()
  })

  const Row = ({ p, depth }) => {
    const kids = childrenOf(p.id)
    const isCollapsed = collapsed[p.id]
    const banned = descendants(p.id); banned.add(p.id)
    const moveTargets = projects.filter((t) => t.org_id === p.org_id && !banned.has(t.id))
    return (
      <>
        <tr>
          <td style={{ paddingLeft: 8 + depth * 22 }}>
            {kids.length > 0
              ? <a onClick={() => setCollapsed((c) => ({ ...c, [p.id]: !c[p.id] }))} style={{ marginRight: 6 }}>{isCollapsed ? '▸' : '▾'}</a>
              : <span style={{ display: 'inline-block', width: 14 }} />}
            <a onClick={() => onOpen(p)}>{p.name}</a>
          </td>
          {multiOrg && <td className="muted">{orgName(p.org_id)}</td>}
          <td className="muted">{p.slug}</td>
          <td className="muted">{p.description}</td>
          <td className="row">
            <button className="ghost sm" onClick={() => onOpen(p)}>Open</button>
            <button className="ghost sm" title="Add a sub-project under this one" onClick={() => setNewFor(p.id)}>+ Sub</button>
            <select className="sm" value="" title="Move under another project"
              onChange={(e) => { const v = e.target.value; move(p, v === '__top' ? null : Number(v)) }}>
              <option value="" disabled>Move…</option>
              {p.parent_id && <option value="__top">↑ Top level</option>}
              {moveTargets.map((t) => <option key={t.id} value={t.id}>→ {t.name}</option>)}
            </select>
            <button className="danger ghost sm" onClick={() => del(p)}>Delete</button>
          </td>
        </tr>
        {!isCollapsed && kids.map((c) => <Row key={c.id} p={c} depth={depth + 1} />)}
      </>
    )
  }

  return (
    <>
      <h2>Projects</h2>
      <div className="row" style={{ marginBottom: 12 }}>
        <button className="primary" onClick={() => setNewFor(null)}>+ New project</button>
      </div>
      {node}
      {projects.length === 0 ? <div className="muted">No projects yet. Create one to start authoring playbooks, Terraform, or Salt states.</div> : (
        <table>
          <thead><tr><th>Name</th>{multiOrg && <th>Organization</th>}<th>Slug</th><th>Description</th><th></th></tr></thead>
          <tbody>{roots.map((p) => <Row key={p.id} p={p} depth={0} />)}</tbody>
        </table>
      )}
      {newFor !== undefined && (
        <NewProject orgs={orgs} parent={projects.find((p) => p.id === newFor) || null}
          onClose={() => setNewFor(undefined)}
          onCreated={(p) => { setNewFor(undefined); load(); onOpen(p) }} />
      )}
    </>
  )
}

function NewProject({ orgs = [], parent, onClose, onCreated }) {
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [mode, setMode] = useState('blank')   // 'blank' | 'clone'
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const writableOrgs = orgs.filter((o) => o.role === 'operator' || o.role === 'admin')
  const [orgId, setOrgId] = useState(writableOrgs[0] ? String(writableOrgs[0].id) : '')
  const { wrap, node } = useErr()
  const create = () => wrap(async () => {
    setBusy(true)
    try {
      const json = { name: name.trim() || (mode === 'clone' ? repoName(url) : ''), description: desc }
      if (parent) json.parent_id = parent.id            // sub-project → inherits parent's org
      else if (orgId) json.org_id = Number(orgId)
      if (mode === 'clone') { json.clone_url = url.trim(); if (token) json.git_token = token }
      onCreated(await api('projects', { method: 'POST', json }))
    } finally { setBusy(false) }
  })
  return (
    <Modal title={parent ? `New sub-project under “${parent.name}”` : 'New project'} onClose={onClose}>
      {!parent && writableOrgs.length > 1 && (
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
