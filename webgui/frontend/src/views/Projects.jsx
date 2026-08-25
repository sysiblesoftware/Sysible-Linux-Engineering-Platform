import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'
import { useInfraRowActions } from './InfraActions.jsx'
import { CreateWizard } from './Infrastructure.jsx'

// Projects are the top-level unit; each can nest sub-projects (folders) via
// parent_id, rendered here as an expandable tree. Organizations still exist for
// access control behind the scenes (shown as a column only when you can see
// more than one). The name is the primary action (opens the project); everything
// else lives in a per-row ⋯ menu to keep the list calm.
export default function Projects({ onOpen, onOpenRun }) {
  const [projects, setProjects] = useState([])
  const [orgs, setOrgs] = useState([])
  const [infraByPid, setInfraByPid] = useState({})  // project_id → infra row (for the ⋯ menu actions)
  const [newFor, setNewFor] = useState(undefined)   // undefined=closed; null=top-level; id=sub-project parent
  const [wizOpen, setWizOpen] = useState(false)     // Create-Infrastructure wizard
  const [moveP, setMoveP] = useState(null)          // project being reparented
  const [menuId, setMenuId] = useState(null)        // project whose ⋯ menu is open
  const [collapsed, setCollapsed] = useState({})    // {projectId: true} → children hidden
  const { wrap, node } = useErr()

  const load = () => api('projects').then((d) => setProjects(d.projects))
  const loadInfra = () => api('infra').then((d) => {
    const m = {}; (d.infra || []).forEach((r) => { m[r.project_id] = r }); setInfraByPid(m)
  }).catch(() => {})
  useEffect(() => { load(); loadInfra(); api('organizations').then((d) => setOrgs(d.organizations || [])).catch(() => {}) }, [])
  // Infra lifecycle actions for the row ⋯ menu (shared with the IDE bar).
  const { itemsFor, modals: infraModals } = useInfraRowActions({
    onOpenRun, onOpenProject: onOpen, onReload: () => { load(); loadInfra() },
  })
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
    await api(`projects/${p.id}`, { method: 'PATCH', json: { parent_id } }); load()
  })
  const del = (p) => wrap(async () => {
    const kids = childrenOf(p.id).length
    const msg = kids ? `Delete “${p.name}”? Its ${kids} sub-project(s) move up to its parent.` : `Delete “${p.name}”?`
    if (!confirm(msg)) return
    await api('projects/' + p.id, { method: 'DELETE' }); load()
  })
  const moveTargetsFor = (p) => {
    const banned = descendants(p.id); banned.add(p.id)
    return projects.filter((t) => t.org_id === p.org_id && !banned.has(t.id))
  }

  const Row = ({ p, depth }) => {
    const kids = childrenOf(p.id)
    const isCollapsed = collapsed[p.id]
    return (
      <>
        <tr className="proj-row">
          <td style={{ paddingLeft: 10 + depth * 22 }}>
            <span className="proj-name">
              <button className={'proj-caret' + (kids.length ? '' : ' leaf')}
                title={isCollapsed ? 'Expand' : 'Collapse'}
                onClick={() => setCollapsed((c) => ({ ...c, [p.id]: !c[p.id] }))}>{isCollapsed ? '▶' : '▼'}</button>
              <span className="proj-link" title={`Open ${p.name}`} onClick={() => onOpen(p)}>{p.name}</span>
              {kids.length > 0 && <span className="proj-count" title={`${kids.length} sub-project(s)`}>{kids.length}</span>}
            </span>
          </td>
          {multiOrg && <td className="muted">{orgName(p.org_id)}</td>}
          <td><span className="proj-slug">{p.slug}</span></td>
          <td><div className="proj-desc" title={p.description}>{p.description || <span className="faint">—</span>}</div></td>
          <td>
            <div className="proj-actions">
              <RowMenu
                open={menuId === p.id}
                onToggle={() => setMenuId((id) => (id === p.id ? null : p.id))}
                onClose={() => setMenuId(null)}
                items={[
                  { label: 'Open', accel: '↵', run: () => onOpen(p) },
                  { label: 'Add sub-project', run: () => setNewFor(p.id) },
                  { label: 'Move…', run: () => setMoveP(p) },
                  ...itemsFor(infraByPid[p.id]),
                  { sep: true },
                  { label: 'Delete', danger: true, run: () => del(p) },
                ]} />
            </div>
          </td>
        </tr>
        {!isCollapsed && kids.map((c) => <Row key={c.id} p={c} depth={depth + 1} />)}
      </>
    )
  }

  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        <h2 style={{ margin: 0 }}>Projects</h2>
        <div className="spacer" />
        <button className="ghost" onClick={() => setWizOpen(true)}>+ Create infrastructure</button>
        <button className="primary" onClick={() => setNewFor(null)}>+ New project</button>
      </div>
      {node}
      {projects.length === 0 ? (
        <div className="empty-state">
          <div className="es-title">No projects yet</div>
          <div>Create one to start authoring playbooks, Terraform, or Salt states.</div>
          <div style={{ marginTop: 14 }}><button className="primary" onClick={() => setNewFor(null)}>+ New project</button></div>
        </div>
      ) : (
        <table className="proj-table">
          <thead><tr><th>Name</th>{multiOrg && <th>Organization</th>}<th>Slug</th><th>Description</th><th style={{ width: 44 }} /></tr></thead>
          <tbody>{roots.map((p) => <Row key={p.id} p={p} depth={0} />)}</tbody>
        </table>
      )}
      {newFor !== undefined && (
        <NewProject orgs={orgs} parent={projects.find((p) => p.id === newFor) || null}
          onClose={() => setNewFor(undefined)}
          onCreated={(p) => { setNewFor(undefined); load(); onOpen(p) }} />
      )}
      {moveP && (
        <MoveProject p={moveP} targets={moveTargetsFor(moveP)}
          onClose={() => setMoveP(null)}
          onMove={(parentId) => { setMoveP(null); move(moveP, parentId) }} />
      )}
      {wizOpen && (
        <CreateWizard onClose={() => setWizOpen(false)}
          onDone={(pid, name, slug) => { setWizOpen(false); load(); loadInfra(); onOpen({ id: pid, name, slug }) }} />
      )}
      {infraModals}
    </>
  )
}

// A per-row ⋯ overflow menu, reusing the app's context-menu styling. Closes on
// outside click / Escape. Positioned under the trigger button.
function RowMenu({ open, onToggle, onClose, items }) {
  const btnRef = useRef(null)
  const [pos, setPos] = useState({ x: 0, y: 0 })
  useEffect(() => {
    if (!open) return undefined
    const r = btnRef.current?.getBoundingClientRect()
    if (r) setPos({ x: Math.max(8, Math.min(r.right - 200, window.innerWidth - 210)), y: r.bottom + 4 })
    const close = (e) => { if (!btnRef.current || !btnRef.current.contains(e.target)) onClose() }
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('mousedown', close)
    window.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onClose, true)
    return () => { window.removeEventListener('mousedown', close); window.removeEventListener('keydown', onKey); window.removeEventListener('scroll', onClose, true) }
  }, [open, onClose])
  return (
    <>
      <button ref={btnRef} className={'kebab' + (open ? ' open' : '')} title="Actions"
        aria-label="Project actions" onClick={onToggle}>⋯</button>
      {open && (
        <div className="ctx-menu" style={{ left: pos.x, top: pos.y }} onMouseDown={(e) => e.stopPropagation()}>
          {items.map((it, i) => it.sep
            ? <div key={i} className="ctx-sep" />
            : <button key={i} className="ctx-item" style={it.danger ? { color: 'var(--danger)' } : undefined}
                onClick={() => { onClose(); it.run() }}>
                <span>{it.label}</span>{it.accel && <span className="ctx-accel">{it.accel}</span>}
              </button>)}
        </div>
      )}
    </>
  )
}

// Reparent a project (or send it to the top level), cycle-guarded by the caller's
// target list.
function MoveProject({ p, targets, onClose, onMove }) {
  return (
    <Modal title={`Move “${p.name}”`} onClose={onClose}>
      <div className="muted" style={{ marginBottom: 4 }}>Choose where this project should nest. Its own sub-projects move with it.</div>
      <div className="col" style={{ gap: 6, maxHeight: '50vh', overflow: 'auto' }}>
        {p.parent_id != null && (
          <button className="ghost" style={{ justifyContent: 'flex-start' }} onClick={() => onMove(null)}>↑ Top level</button>
        )}
        {targets.length === 0 && p.parent_id == null
          ? <div className="faint" style={{ fontSize: 13 }}>No other project can hold this one.</div>
          : targets.map((t) => (
            <button key={t.id} className="ghost" style={{ justifyContent: 'flex-start' }} onClick={() => onMove(t.id)}>→ {t.name}</button>
          ))}
      </div>
    </Modal>
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
