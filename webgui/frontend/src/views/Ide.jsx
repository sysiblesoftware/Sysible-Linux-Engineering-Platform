import React, { useEffect, useState, useCallback, useRef } from 'react'
import Editor from '@monaco-editor/react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'
import { SNIPPET_GROUPS as ANSIBLE_SNIPPETS } from '../ansibleSnippets.js'
import { SNIPPET_GROUPS as TERRAFORM_SNIPPETS } from '../terraformSnippets.js'
import { SNIPPET_GROUPS as SALT_SNIPPETS } from '../saltSnippets.js'

const langFor = (path) => {
  if (path.endsWith('.tf') || path.endsWith('.hcl')) return 'hcl'
  if (path.endsWith('.yml') || path.endsWith('.yaml') || path.endsWith('.sls')) return 'yaml'
  if (path.endsWith('.json')) return 'json'
  if (path.endsWith('.sh')) return 'shell'
  if (path.endsWith('.py')) return 'python'
  return 'plaintext'
}

// Pick the snippet library + wording for the open file's engine, so "+ Task"
// becomes "+ Resource" on Terraform files and "+ State" on Salt states. Salt
// SLS wins over the generic YAML→Ansible mapping.
const snippetsFor = (path) => {
  const p = (path || '').toLowerCase()
  if (p.endsWith('.tf') || p.endsWith('.hcl')) return { groups: TERRAFORM_SNIPPETS, verb: 'Resource', engine: 'terraform' }
  if (p.endsWith('.sls')) return { groups: SALT_SNIPPETS, verb: 'State', engine: 'salt' }
  return { groups: ANSIBLE_SNIPPETS, verb: 'Task', engine: 'ansible' }
}

export default function Ide({ project, onBack, onRun }) {
  const [tree, setTree] = useState([])
  const [path, setPath] = useState(null)
  const [content, setContent] = useState('')
  const [saved, setSaved] = useState(true)
  const [runOpen, setRunOpen] = useState(false)
  const [newOpen, setNewOpen] = useState(false)
  const [taskOpen, setTaskOpen] = useState(false)
  const [delPath, setDelPath] = useState(null)
  const [menu, setMenu] = useState(null)   // {x, y} custom editor context menu
  const editorRef = useRef(null)
  const snip = snippetsFor(path)

  // A brand-matched Monaco theme (defined once, before the editor mounts) so the
  // editor surface uses the SLEP palette instead of stock vs-dark.
  const defineTheme = (monaco) => {
    monaco.editor.defineTheme('sysible-dark', {
      base: 'vs-dark', inherit: true, rules: [],
      colors: {
        'editor.background': '#0d1117',
        'editor.foreground': '#e9f0f7',
        'editorLineNumber.foreground': '#4a5a72',
        'editorLineNumber.activeForeground': '#9aa7bd',
        'editorCursor.foreground': '#63c869',
        'editor.selectionBackground': '#2a3b55',
        'editor.lineHighlightBackground': '#12161f',
        'editorIndentGuide.background': '#1c2432',
        'editorIndentGuide.activeBackground': '#33405a',
        'editorGutter.background': '#0d1117',
        'editorWidget.background': '#12161f',
        'editorWidget.border': '#232b3a',
      },
    })
  }

  // Read the current selection (or the whole current line when nothing is
  // selected — matching editor norms for copy/cut).
  const selectionText = (ed) => {
    const model = ed.getModel(); const sel = ed.getSelection()
    if (!sel || sel.isEmpty()) { const ln = sel ? sel.startLineNumber : 1; return model.getLineContent(ln) + '\n' }
    return model.getValueInRange(sel)
  }
  const doCopy = async (ed) => { try { await navigator.clipboard.writeText(selectionText(ed)) } catch { ed.trigger('menu', 'editor.action.clipboardCopyAction') } }
  const doCut = async (ed) => {
    const sel = ed.getSelection()
    try {
      await navigator.clipboard.writeText(selectionText(ed))
      if (sel && !sel.isEmpty()) ed.executeEdits('cut', [{ range: sel, text: '', forceMoveMarkers: true }])
      else ed.trigger('menu', 'editor.action.deleteLines')
      setSaved(false)
    } catch { ed.trigger('menu', 'editor.action.clipboardCutAction') }
    ed.focus()
  }
  const doPaste = async (ed) => {
    try {
      const text = await navigator.clipboard.readText()
      ed.executeEdits('paste', [{ range: ed.getSelection(), text, forceMoveMarkers: true }])
      setSaved(false); ed.focus()
    } catch {
      alert('Your browser blocked clipboard read. Use Ctrl+V (⌘V on Mac) to paste — that always works.')
    }
  }
  const menuAction = async (fn) => { const ed = editorRef.current; setMenu(null); if (ed) await fn(ed) }

  const loadTree = useCallback(() => api(`projects/${project.id}/files`).then((d) => setTree(d.files)), [project.id])
  useEffect(() => { loadTree() }, [loadTree])

  const open = async (p) => { const d = await api(`projects/${project.id}/file?path=${encodeURIComponent(p)}`); setPath(p); setContent(d.content); setSaved(true) }

  // Delete via an in-app dialog (native confirm() can be suppressed by the
  // browser, which silently blocked deletes). try/catch surfaces any error.
  const remove = async (p) => {
    try {
      await api(`projects/${project.id}/file?path=${encodeURIComponent(p)}`, { method: 'DELETE' })
      if (p === path) { setPath(null); setContent('') }
      await loadTree()
    } catch (e) { alert('Could not delete ' + p + ': ' + e.message) }
  }

  // Insert a task snippet at the editor cursor (fires onChange → marks unsaved).
  // Space it out: drop a blank line before the task when it follows other content
  // (so successive inserts read as separate tasks, not one wall of YAML), and end
  // on a fresh line. Idempotent about existing blank lines — never stacks them up.
  const insertTask = (yaml) => {
    const ed = editorRef.current
    if (!ed) return
    const model = ed.getModel()
    const sel = ed.getSelection()
    const pos = sel.getStartPosition()
    const before = model.getValueInRange({ startLineNumber: 1, startColumn: 1, endLineNumber: pos.lineNumber, endColumn: pos.column })
    let text = yaml.replace(/\s+$/, '') + '\n'          // exactly one trailing newline
    if (before.trim() !== '') {                          // there's prior content above
      const trailing = (before.match(/\n*$/) || [''])[0].length
      if (trailing < 2) text = '\n'.repeat(2 - trailing) + text   // ensure a blank line separates
    }
    ed.executeEdits('insert-task', [{ range: sel, text, forceMoveMarkers: true }])
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
        <button className="ghost sm" onClick={() => setTaskOpen(true)} disabled={path == null}
          title={`Insert a ready-made ${snip.engine} ${snip.verb.toLowerCase()} at the cursor`}>+ {snip.verb}</button>
        <button className="ghost sm" onClick={save} disabled={path == null}>{saved ? 'Saved' : 'Save'}</button>
        <button className="primary sm" onClick={() => setRunOpen(true)}>▶ Run</button>
      </div>
      <div className="ide">
        <div className="tree">
          {tree.length === 0 && <div className="muted" style={{ padding: 6 }}>Empty project — “+ File”.</div>}
          {tree.map((f) => (
            <div key={f.path} className={'f ' + (f.type === 'dir' ? 'dir' : '') + (f.path === path ? ' active' : '')}>
              <span className="f-name" onClick={() => f.type === 'file' && open(f.path)}>{f.type === 'dir' ? '📁 ' : '📄 '}{f.path}</span>
              <button className="f-del" title={'Delete ' + f.path} onClick={(e) => { e.stopPropagation(); setDelPath(f.path) }}>✕</button>
            </div>
          ))}
        </div>
        <div className="edwrap" onContextMenu={(e) => { if (path != null) { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY }) } }}>
          <div className="edtool"><span className="muted">{path || 'No file open'}{!saved && ' •'}</span></div>
          <Editor height="100%" theme="sysible-dark" path={path || 'untitled'} language={langFor(path || '')}
            value={content} onChange={(v) => { setContent(v ?? ''); setSaved(false) }}
            beforeMount={defineTheme}
            onMount={(ed) => { editorRef.current = ed }}
            options={{ minimap: { enabled: false }, fontSize: 13, automaticLayout: true, contextmenu: false }} />
        </div>
      </div>
      {newOpen && <NewFile project={project} onClose={() => setNewOpen(false)} onCreated={(p) => { setNewOpen(false); loadTree(); open(p) }} />}
      {taskOpen && <TaskPalette groups={snip.groups} verb={snip.verb} onClose={() => setTaskOpen(false)} onInsert={insertTask} />}
      {delPath && (
        <Modal title="Delete file" onClose={() => setDelPath(null)}>
          <div>Delete <b>{delPath}</b>? This can’t be undone.</div>
          <div className="row" style={{ marginTop: 6 }}>
            <div className="spacer" />
            <button className="ghost" onClick={() => setDelPath(null)}>Cancel</button>
            <button className="danger" onClick={() => { const p = delPath; setDelPath(null); remove(p) }}>Delete</button>
          </div>
        </Modal>
      )}
      {runOpen && <RunModal project={project} currentFile={path} onClose={() => setRunOpen(false)} onLaunched={onRun} />}
      {menu && <EditorMenu at={menu} onClose={() => setMenu(null)}
        items={[
          { label: 'Cut', accel: 'Ctrl+X', run: () => menuAction(doCut) },
          { label: 'Copy', accel: 'Ctrl+C', run: () => menuAction(doCopy) },
          { label: 'Paste', accel: 'Ctrl+V', run: () => menuAction(doPaste) },
          { sep: true },
          { label: 'Select all', accel: 'Ctrl+A', run: () => menuAction((ed) => { ed.setSelection(ed.getModel().getFullModelRange()); ed.focus() }) },
          { label: 'Command palette', accel: 'F1', run: () => menuAction((ed) => { ed.focus(); ed.trigger('menu', 'editor.action.quickCommand') }) },
        ]} />}
    </>
  )
}

// A brand-styled right-click menu for the editor. Replaces Monaco's stock menu
// (which, in a browser, omits Paste and can't be themed) with Cut/Copy/Paste
// driven by the async Clipboard API. Closes on outside click, Escape, or scroll.
function EditorMenu({ at, items, onClose }) {
  useEffect(() => {
    const close = () => onClose()
    window.addEventListener('mousedown', close)
    window.addEventListener('scroll', close, true)
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => { window.removeEventListener('mousedown', close); window.removeEventListener('scroll', close, true); window.removeEventListener('keydown', onKey) }
  }, [onClose])
  const x = Math.min(at.x, window.innerWidth - 210)
  const y = Math.min(at.y, window.innerHeight - 220)
  return (
    <div className="ctx-menu" style={{ left: x, top: y }} onMouseDown={(e) => e.stopPropagation()} onContextMenu={(e) => e.preventDefault()}>
      {items.map((it, i) => it.sep
        ? <div key={i} className="ctx-sep" />
        : <button key={i} className="ctx-item" onClick={it.run}>
            <span>{it.label}</span>{it.accel && <span className="ctx-accel">{it.accel}</span>}
          </button>)}
    </div>
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

// Insert-task palette. To keep ~13 categories from crowding the dialog, it's
// category-first: a left rail of groups, and only the selected group's tasks on
// the right. Typing in the search box switches to a flat, cross-category result
// list (grouped by category) so nothing is buried behind a click.
function TaskPalette({ groups: SNIPPET_GROUPS, verb = 'Task', onClose, onInsert }) {
  const [q, setQ] = useState('')
  const [cat, setCat] = useState(SNIPPET_GROUPS[0].group)
  const needle = q.trim().toLowerCase()
  const searching = needle.length > 0

  // Search mode: flatten matches across every group, keep group labels.
  const matches = SNIPPET_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((it) => it.name.toLowerCase().includes(needle) || (it.search || '').includes(needle)),
  })).filter((g) => g.items.length)

  const active = SNIPPET_GROUPS.find((g) => g.group === cat) || SNIPPET_GROUPS[0]
  const Task = (it) => (
    <button key={it.name} className="ghost" style={{ display: 'block', width: '100%', textAlign: 'left', marginBottom: 4 }}
      onClick={() => onInsert(it.yaml)}>{it.name}</button>
  )

  return (
    <Modal title={`Insert ${verb.toLowerCase()}`} onClose={onClose} wide>
      <input autoFocus placeholder={`Search ${verb.toLowerCase()}s…`}
        value={q} onChange={(e) => setQ(e.target.value)} />
      {searching ? (
        <div style={{ maxHeight: '56vh', overflow: 'auto', marginTop: 8 }}>
          {matches.length === 0 && <div className="muted" style={{ padding: 8 }}>No matching tasks.</div>}
          {matches.map((g) => (
            <div key={g.group} style={{ marginBottom: 8 }}>
              <div className="faint" style={{ fontSize: 12, margin: '8px 2px 4px' }}>{g.group}</div>
              {g.items.map(Task)}
            </div>
          ))}
        </div>
      ) : (
        <div className="task-palette" style={{ marginTop: 8 }}>
          <div className="tp-cats">
            {SNIPPET_GROUPS.map((g) => (
              <button key={g.group} className={'tp-cat' + (g.group === cat ? ' active' : '')}
                onClick={() => setCat(g.group)}>
                {g.group}<span className="faint" style={{ fontSize: 11 }}> {g.items.length}</span>
              </button>
            ))}
          </div>
          <div className="tp-items">
            <div className="faint" style={{ fontSize: 12, margin: '2px 2px 6px' }}>{active.group}</div>
            {active.items.map(Task)}
          </div>
        </div>
      )}
      <div className="faint" style={{ fontSize: 12, marginTop: 6 }}>Inserts at the cursor. Adjust indentation to match your file.</div>
    </Modal>
  )
}

function RunModal({ project, currentFile, onClose, onLaunched }) {
  const [engine, setEngine] = useState('ansible')
  const [target, setTarget] = useState(currentFile || 'site.yml')
  const [invs, setInvs] = useState([]); const [creds, setCreds] = useState([])
  const [inv, setInv] = useState(''); const [cred, setCred] = useState('')
  const [vars, setVars] = useState('')       // KEY=value per line
  const [saltTest, setSaltTest] = useState(false)
  const { wrap, node } = useErr()

  // Parse the KEY=value textarea into an object (blank lines / #comments ignored).
  const parseVars = () => {
    const out = {}
    for (const raw of vars.split('\n')) {
      const line = raw.trim()
      if (!line || line.startsWith('#')) continue
      const i = line.indexOf('=')
      if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim()
    }
    return out
  }

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
      <Field label={engine === 'terraform' ? 'Variables — KEY=value per line (→ -var)'
        : engine === 'salt' ? 'Pillar / kwargs — key=value per line'
          : 'Extra vars — KEY=value per line (→ -e)'}>
        <textarea rows={3} value={vars} onChange={(e) => setVars(e.target.value)}
          placeholder={engine === 'terraform' ? 'instance_type=t3.micro' : 'env=staging'}
          style={{ fontFamily: 'ui-monospace,Menlo,monospace', fontSize: 12.5, resize: 'vertical' }} />
      </Field>
      {engine === 'salt' && (
        <label className="row" style={{ gap: 8, fontSize: 13, cursor: 'pointer' }}>
          <input type="checkbox" checked={saltTest} onChange={(e) => setSaltTest(e.target.checked)} style={{ width: 'auto' }} />
          Test mode (dry-run — <span className="mono">test=True</span>, report changes without applying)
        </label>
      )}
      {node}
      <button className="primary" onClick={() => wrap(async () => {
        if (needsInv && !inv) throw new Error('Create and select an inventory first.')
        const extra = parseVars()
        if (engine === 'salt' && saltTest) extra.test = 'True'
        const d = await api('runs', { method: 'POST', json: {
          project_id: project.id, kind: engine, target,
          inventory_id: needsInv ? Number(inv) : null, credential_id: cred ? Number(cred) : null,
          extra_vars: extra,
        } })
        onClose(); onLaunched(d.run_id)
      })}>▶ Launch</button>
    </Modal>
  )
}
