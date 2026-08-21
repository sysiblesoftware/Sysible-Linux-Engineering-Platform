import React, { useEffect, useState, useCallback, useRef } from 'react'
import Editor from '@monaco-editor/react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'
import { SNIPPET_GROUPS } from '../ansibleSnippets.js'

const langFor = (path) => {
  if (path.endsWith('.tf') || path.endsWith('.hcl')) return 'hcl'
  if (path.endsWith('.yml') || path.endsWith('.yaml') || path.endsWith('.sls')) return 'yaml'
  if (path.endsWith('.json')) return 'json'
  if (path.endsWith('.sh')) return 'shell'
  if (path.endsWith('.py')) return 'python'
  return 'plaintext'
}

export default function Ide({ project, onBack, onRun }) {
  const [tree, setTree] = useState([])
  const [path, setPath] = useState(null)
  const [content, setContent] = useState('')
  const [saved, setSaved] = useState(true)
  const [runOpen, setRunOpen] = useState(false)
  const [newOpen, setNewOpen] = useState(false)
  const [taskOpen, setTaskOpen] = useState(false)
  const editorRef = useRef(null)

  const loadTree = useCallback(() => api(`projects/${project.id}/files`).then((d) => setTree(d.files)), [project.id])
  useEffect(() => { loadTree() }, [loadTree])

  const open = async (p) => { const d = await api(`projects/${project.id}/file?path=${encodeURIComponent(p)}`); setPath(p); setContent(d.content); setSaved(true) }

  const remove = async (p) => {
    if (!confirm('Delete ' + p + '?')) return
    await api(`projects/${project.id}/file?path=${encodeURIComponent(p)}`, { method: 'DELETE' })
    if (p === path) { setPath(null); setContent('') }
    loadTree()
  }

  // Insert a task snippet at the editor cursor (fires onChange → marks unsaved).
  const insertTask = (yaml) => {
    const ed = editorRef.current
    if (!ed) return
    ed.executeEdits('insert-task', [{ range: ed.getSelection(), text: yaml, forceMoveMarkers: true }])
    ed.focus()
    setTaskOpen(false)
  }
  const save = useCallback(async () => {
    if (path == null) return
    await api(`projects/${project.id}/file`, { method: 'PUT', json: { path, content } })
    setSaved(true)
  }, [project.id, path, content])

  useEffect(() => {
    const onKey = (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save() } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [save])

  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <button className="ghost sm" onClick={onBack}>← Projects</button>
        <h2 style={{ margin: 0 }}>{project.name}</h2><span className="muted">{project.slug}</span>
        <div className="spacer" />
        <button className="ghost sm" onClick={() => setNewOpen(true)}>+ File</button>
        <button className="ghost sm" onClick={() => setTaskOpen(true)} disabled={path == null} title="Insert a ready-made Ansible task at the cursor">+ Task</button>
        <button className="ghost sm" onClick={save} disabled={path == null}>{saved ? 'Saved' : 'Save'}</button>
        <button className="primary sm" onClick={() => setRunOpen(true)}>▶ Run</button>
      </div>
      <div className="ide">
        <div className="tree">
          {tree.length === 0 && <div className="muted" style={{ padding: 6 }}>Empty project — “+ File”.</div>}
          {tree.map((f) => (
            <div key={f.path} className={'f ' + (f.type === 'dir' ? 'dir' : '') + (f.path === path ? ' active' : '')}>
              <span className="f-name" onClick={() => f.type === 'file' && open(f.path)}>{f.type === 'dir' ? '📁 ' : '📄 '}{f.path}</span>
              <button className="f-del" title={'Delete ' + f.path} onClick={(e) => { e.stopPropagation(); remove(f.path) }}>✕</button>
            </div>
          ))}
        </div>
        <div className="edwrap">
          <div className="edtool"><span className="muted">{path || 'No file open'}{!saved && ' •'}</span></div>
          <Editor height="100%" theme="vs-dark" path={path || 'untitled'} language={langFor(path || '')}
            value={content} onChange={(v) => { setContent(v ?? ''); setSaved(false) }}
            onMount={(ed) => { editorRef.current = ed }}
            options={{ minimap: { enabled: false }, fontSize: 13, automaticLayout: true }} />
        </div>
      </div>
      {newOpen && <NewFile project={project} onClose={() => setNewOpen(false)} onCreated={(p) => { setNewOpen(false); loadTree(); open(p) }} />}
      {taskOpen && <TaskPalette onClose={() => setTaskOpen(false)} onInsert={insertTask} />}
      {runOpen && <RunModal project={project} currentFile={path} onClose={() => setRunOpen(false)} onLaunched={onRun} />}
    </>
  )
}

function NewFile({ project, onClose, onCreated }) {
  const [p, setP] = useState('')
  const { wrap, node } = useErr()
  return (
    <Modal title="New file" onClose={onClose}>
      <Field label="Path"><input value={p} onChange={(e) => setP(e.target.value)} autoFocus placeholder="site.yml, main.tf, states/web.sls" /></Field>
      {node}
      <button className="primary" onClick={() => wrap(async () => { await api(`projects/${project.id}/file`, { method: 'POST', json: { path: p, type: 'file' } }); onCreated(p) })}>Create</button>
    </Modal>
  )
}

// Insert-task palette: searchable, grouped Ansible task snippets. Clicking a task
// drops its YAML at the editor cursor.
function TaskPalette({ onClose, onInsert }) {
  const [q, setQ] = useState('')
  const needle = q.trim().toLowerCase()
  const groups = SNIPPET_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((it) => !needle
      || it.name.toLowerCase().includes(needle) || (it.search || '').includes(needle)),
  })).filter((g) => g.items.length)
  return (
    <Modal title="Insert task" onClose={onClose}>
      <input autoFocus placeholder="Search tasks — shell, package, service, lvm, mount, git…"
        value={q} onChange={(e) => setQ(e.target.value)} />
      <div style={{ maxHeight: '56vh', overflow: 'auto', marginTop: 4 }}>
        {groups.length === 0 && <div className="muted" style={{ padding: 8 }}>No matching tasks.</div>}
        {groups.map((g) => (
          <div key={g.group} style={{ marginBottom: 8 }}>
            <div className="faint" style={{ fontSize: 12, margin: '8px 2px 4px' }}>{g.group}</div>
            {g.items.map((it) => (
              <button key={it.name} className="ghost" style={{ display: 'block', width: '100%', textAlign: 'left', marginBottom: 4 }}
                onClick={() => onInsert(it.yaml)}>{it.name}</button>
            ))}
          </div>
        ))}
      </div>
      <div className="faint" style={{ fontSize: 12 }}>Inserts a task at the cursor — indented for a play’s <span className="mono">tasks:</span> list. Adjust indentation to match your file.</div>
    </Modal>
  )
}

function RunModal({ project, currentFile, onClose, onLaunched }) {
  const [engine, setEngine] = useState('ansible')
  const [target, setTarget] = useState(currentFile || 'site.yml')
  const [invs, setInvs] = useState([]); const [creds, setCreds] = useState([])
  const [inv, setInv] = useState(''); const [cred, setCred] = useState('')
  const { wrap, node } = useErr()

  useEffect(() => { api('inventories').then((d) => { setInvs(d.inventories); if (d.inventories[0]) setInv(String(d.inventories[0].id)) }) }, [])
  useEffect(() => { api('credentials').then((d) => setCreds(d.credentials)) }, [])
  useEffect(() => {
    if (engine === 'terraform') setTarget('plan')
    else if (engine === 'salt') setTarget('highstate')
    else setTarget(currentFile || 'site.yml')
  }, [engine])

  const needsInv = engine !== 'terraform'
  return (
    <Modal title="Run" onClose={onClose}>
      <Field label="Engine">
        <select value={engine} onChange={(e) => setEngine(e.target.value)}>
          <option value="ansible">Ansible — playbook</option>
          <option value="terraform">Terraform — plan / apply / destroy</option>
          <option value="salt">Salt — state.apply (salt-ssh)</option>
        </select>
      </Field>
      <Field label={engine === 'ansible' ? 'Playbook path' : engine === 'terraform' ? 'Action' : 'State (or “highstate”)'}>
        {engine === 'terraform'
          ? <select value={target} onChange={(e) => setTarget(e.target.value)}><option>plan</option><option>apply</option><option>destroy</option></select>
          : <input value={target} onChange={(e) => setTarget(e.target.value)} />}
      </Field>
      {needsInv && (
        <Field label="Inventory">
          <select value={inv} onChange={(e) => setInv(e.target.value)}>
            {invs.length === 0 && <option value="">(no inventories — create one first)</option>}
            {invs.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
          </select>
        </Field>
      )}
      <Field label={engine === 'terraform' ? 'Cloud/env credential' : 'SSH credential'}>
        <select value={cred} onChange={(e) => setCred(e.target.value)}>
          <option value="">(none)</option>
          {creds.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.kind})</option>)}
        </select>
      </Field>
      {node}
      <button className="primary" onClick={() => wrap(async () => {
        if (needsInv && !inv) throw new Error('Create and select an inventory first.')
        const d = await api('runs', { method: 'POST', json: {
          project_id: project.id, kind: engine, target,
          inventory_id: needsInv ? Number(inv) : null, credential_id: cred ? Number(cred) : null,
        } })
        onClose(); onLaunched(d.run_id)
      })}>▶ Launch</button>
    </Modal>
  )
}
