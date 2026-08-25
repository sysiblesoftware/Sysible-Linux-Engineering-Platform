import React, { useEffect, useState } from 'react'
import { api, canWrite } from '../api.js'
import { Modal, Field } from '../ui.jsx'
import { PipelineModal } from './Ide.jsx'

// Saved pipelines — named, re-runnable sequences (create → configure → maintain,
// or any order). Build one with the pipeline editor, run it, and the run
// visualizer shows the whole sequence advancing.
export default function Pipelines({ onOpenRun }) {
  const [rows, setRows] = useState([])
  const [projects, setProjects] = useState([])
  const [newOpen, setNewOpen] = useState(false)
  const [editor, setEditor] = useState(null)   // {project, saveId?, name?, steps?, stop?}
  const load = () => api('pipelines').then((d) => setRows(d.pipelines))
  useEffect(() => { load(); api('projects').then((d) => setProjects(d.projects)) }, [])
  const writable = canWrite()

  const run = async (p) => {
    try { const d = await api(`pipelines/${p.id}/run`, { method: 'POST' }); onOpenRun(d.run_ids[0]) }
    catch (e) { alert(e.message) }
  }
  const del = async (p) => { if (confirm(`Delete pipeline "${p.name}"?`)) { await api(`pipelines/${p.id}`, { method: 'DELETE' }); load() } }
  const edit = (p) => setEditor({
    project: { id: p.project_id, name: p.project_name, slug: p.project_slug },
    saveId: p.id, name: p.name, steps: p.steps, stop: p.stop_on_failure,
  })

  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <h2 style={{ margin: 0 }}>Pipelines</h2>
        <div className="spacer" />
        {writable && <button className="primary" onClick={() => setNewOpen(true)}>+ New pipeline</button>}
      </div>
      <div className="muted" style={{ marginBottom: 12 }}>Saved sequences you can re-run — steps execute one after another (create → configure → maintain, or any order).</div>
      {rows.length === 0 ? <div className="muted">No saved pipelines yet. “New pipeline” to build one, or save a sequence from a project’s “Sequence” builder.</div> : (
        <table>
          <thead><tr><th>Name</th><th>Project</th><th>Sequence</th><th></th></tr></thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.id}>
                <td><b>{p.name}</b></td>
                <td className="muted">{p.project_name}</td>
                <td className="muted" style={{ fontSize: 12.5 }}>{p.steps.map((s) => `${s.kind}:${s.target}`).join('  →  ')}</td>
                <td className="row">
                  {writable && <button className="primary sm" onClick={() => run(p)}>Run</button>}
                  {writable && <button className="ghost sm" onClick={() => edit(p)}>Edit</button>}
                  {writable && <button className="danger ghost sm" onClick={() => del(p)}>Delete</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {newOpen && <PickProject projects={projects} onClose={() => setNewOpen(false)}
        onPick={(project) => { setNewOpen(false); setEditor({ project }) }} />}
      {editor && <PipelineModal project={editor.project} initialSteps={editor.steps} initialName={editor.name}
        saveId={editor.saveId} stopDefault={editor.stop}
        onClose={() => setEditor(null)} onSaved={() => { setEditor(null); load() }}
        onLaunched={(id) => { setEditor(null); load(); onOpenRun(id) }} />}
    </>
  )
}

function PickProject({ projects, onClose, onPick }) {
  const [pid, setPid] = useState(projects[0] ? String(projects[0].id) : '')
  return (
    <Modal title="New pipeline" onClose={onClose}>
      <Field label="Project">
        <select value={pid} onChange={(e) => setPid(e.target.value)}>
          {projects.length === 0 && <option value="">(create a project first)</option>}
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </Field>
      <button className="primary" disabled={!pid} onClick={() => {
        const p = projects.find((x) => String(x.id) === String(pid))
        onPick({ id: p.id, name: p.name, slug: p.slug })
      }}>Build sequence →</button>
    </Modal>
  )
}
