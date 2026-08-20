import React, { useEffect, useState, useCallback } from 'react'
import Editor from '@monaco-editor/react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'

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

  const loadTree = useCallback(() => api(`projects/${project.id}/files`).then((d) => setTree(d.files)), [project.id])
  useEffect(() => { loadTree() }, [loadTree])

  const open = async (p) => { const d = await api(`projects/${project.id}/file?path=${encodeURIComponent(p)}`); setPath(p); setContent(d.content); setSaved(true) }
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
        <button className="ghost sm" onClick={save} disabled={path == null}>{saved ? 'Saved' : 'Save'}</button>
        <button className="primary sm" onClick={() => setRunOpen(true)}>▶ Run</button>
      </div>
      <div className="ide">
        <div className="tree">
          {tree.length === 0 && <div className="muted" style={{ padding: 6 }}>Empty project — “+ File”.</div>}
          {tree.map((f) => (
            <div key={f.path} className={'f ' + (f.type === 'dir' ? 'dir' : '') + (f.path === path ? ' active' : '')}
              onClick={() => f.type === 'file' && open(f.path)}>{f.type === 'dir' ? '📁 ' : '📄 '}{f.path}</div>
          ))}
        </div>
        <div className="edwrap">
          <div className="edtool"><span className="muted">{path || 'No file open'}{!saved && ' •'}</span></div>
          <Editor height="100%" theme="vs-dark" path={path || 'untitled'} language={langFor(path || '')}
            value={content} onChange={(v) => { setContent(v ?? ''); setSaved(false) }}
            options={{ minimap: { enabled: false }, fontSize: 13, automaticLayout: true }} />
        </div>
      </div>
      {newOpen && <NewFile project={project} onClose={() => setNewOpen(false)} onCreated={(p) => { setNewOpen(false); loadTree(); open(p) }} />}
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
