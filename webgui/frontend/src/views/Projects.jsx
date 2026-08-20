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
  const { wrap, node } = useErr()
  return (
    <Modal title="New project" onClose={onClose}>
      <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} autoFocus /></Field>
      <Field label="Description"><input value={desc} onChange={(e) => setDesc(e.target.value)} /></Field>
      {node}
      <button className="primary" onClick={() => wrap(async () => onCreated(await api('projects', { method: 'POST', json: { name, description: desc } })))}>Create</button>
    </Modal>
  )
}
