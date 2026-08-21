import React, { useEffect, useState, useCallback, useRef } from 'react'
import Editor from '@monaco-editor/react'
import { api } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'
import { SNIPPET_GROUPS as ANSIBLE_SNIPPETS, PLAY_GROUPS } from '../ansibleSnippets.js'
import CollectionsInstall from '../components/CollectionsInstall.jsx'
import { SNIPPET_GROUPS as TERRAFORM_SNIPPETS } from '../terraformSnippets.js'
import { SNIPPET_GROUPS as SALT_SNIPPETS } from '../saltSnippets.js'

// Mirror of backend/_ansible_group: INI group names allow only letters, digits
// and underscores, and can't start with a digit. Keep in sync so the play-target
// dropdown shows the same group the generated inventory will actually contain.
const ansibleGroup = (name) => {
  let g = (name || '').trim().replace(/[^A-Za-z0-9_]/g, '_')
  if (g && g[0] >= '0' && g[0] <= '9') g = 'g_' + g
  return g || 'ungrouped'
}

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
  // Ansible palette leads with playbook-structure blocks, then the task library.
  return { groups: [...PLAY_GROUPS, ...ANSIBLE_SNIPPETS], verb: 'Task', engine: 'ansible' }
}

export default function Ide({ project, onBack, onRun }) {
  const [tree, setTree] = useState([])
  const [path, setPath] = useState(null)
  const [content, setContent] = useState('')
  const [saved, setSaved] = useState(true)
  const [runOpen, setRunOpen] = useState(false)
  const [newOpen, setNewOpen] = useState(false)
  const [taskOpen, setTaskOpen] = useState(false)
  const [playOpen, setPlayOpen] = useState(false)
  const [collOpen, setCollOpen] = useState(false)
  const [targets, setTargets] = useState(['all', 'localhost'])   // host patterns for `hosts:`
  const [delPath, setDelPath] = useState(null)
  const [menu, setMenu] = useState(null)   // {x, y} custom editor context menu
  const editorRef = useRef(null)
  const snip = snippetsFor(path)

  // Gather `hosts:` targets from the operator's inventories — the group names
  // (Controller environments) their hosts carry — so play targeting can be picked,
  // not memorized. Groups are stored COMMA-separated, and a single group may
  // contain spaces ("Sysible Labs"); we split on commas only and sanitize each to
  // the exact group Ansible will see at run time (spaces → underscores), so the
  // dropdown offers "Sysible_Labs" (one entry), not "Sysible" + "Labs".
  useEffect(() => {
    let live = true
    ;(async () => {
      try {
        const d = await api('inventories')
        const groups = new Set()
        for (const inv of d.inventories || []) {
          try {
            const h = await api(`inventories/${inv.id}/hosts`)
            for (const host of h.hosts || []) (host.groups || '').split(',').forEach((g) => {
              const s = ansibleGroup(g); if (g.trim()) groups.add(s)
            })
          } catch { /* skip an unreadable inventory */ }
        }
        if (live) setTargets(['all', 'localhost', ...[...groups].sort()])
      } catch { /* no inventories yet — defaults stand */ }
    })()
    return () => { live = false }
  }, [project.id])

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

  // Build a play header from the dialog's choices.
  const buildPlayHeader = ({ name, hosts, become, gather }) => {
    let h = `- name: ${name || 'Configure hosts'}\n  hosts: ${hosts || 'all'}\n`
    if (become) h += '  become: true\n'
    h += `  gather_facts: ${gather ? 'true' : 'false'}\n  tasks:\n`
    return h
  }

  // Wrap the whole file's bare task list in a play: prepend a header ending in
  // `tasks:`, and re-indent the existing content so its list markers sit at 4
  // spaces (nested under tasks:). This is the fix for "not a valid attribute for
  // a Play" — a task list run as a playbook with no play around it.
  const wrapInPlay = (opts) => {
    const header = buildPlayHeader(opts)
    const lines = (content || '').replace(/\s+$/, '').split('\n')
    const firstDash = lines.find((l) => /^\s*-\s/.test(l))
    const curIndent = firstDash ? (firstDash.match(/^\s*/)[0].length) : 0
    const delta = 4 - curIndent
    const shift = (l) => {
      if (l.trim() === '') return ''
      if (delta > 0) return ' '.repeat(delta) + l
      if (delta < 0) { const lead = l.match(/^\s*/)[0].length; return l.slice(Math.min(-delta, lead)) }
      return l
    }
    const body = lines.map(shift).join('\n')
    setContent(header + body + '\n'); setSaved(false); setPlayOpen(false)
  }

  // Insert just a play header at the cursor (for starting a second play, etc.).
  const insertPlayHeader = (opts) => {
    const ed = editorRef.current
    if (ed) {
      const sel = ed.getSelection()
      ed.executeEdits('insert-play', [{ range: sel, text: buildPlayHeader(opts), forceMoveMarkers: true }])
      ed.focus(); setSaved(false)
    } else { setContent((c) => buildPlayHeader(opts) + (c || '')); setSaved(false) }
    setPlayOpen(false)
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
        <button className="ghost sm" onClick={() => setNewOpen(true)}>Add File</button>
        {snip.engine === 'ansible' && (
          <button className="ghost sm" onClick={() => setPlayOpen(true)} disabled={path == null}
            title="Wrap this file in a play, or insert a play header (hosts, become, tasks)">Add Play</button>
        )}
        {snip.engine === 'ansible' && (
          <button className="ghost sm" onClick={() => setCollOpen(true)}
            title="Install the Ansible Galaxy collections the modules need (community.general, ansible.posix, …)">Collections</button>
        )}
        <button className="ghost sm" onClick={() => setTaskOpen(true)} disabled={path == null}
          title={`Insert a ready-made ${snip.engine} ${snip.verb.toLowerCase()} at the cursor`}>Add {snip.verb}</button>
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
      {playOpen && <PlayModal targets={targets} hasContent={(content || '').trim().length > 0}
        onClose={() => setPlayOpen(false)} onWrap={wrapInPlay} onInsert={insertPlayHeader} />}
      {collOpen && <CollectionsInstall onClose={() => setCollOpen(false)} />}
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
          ...(snip.engine === 'ansible' ? [
            { label: 'Play header / wrap in play…', run: () => { setMenu(null); setPlayOpen(true) } },
            { label: `Insert building block…`, run: () => { setMenu(null); setTaskOpen(true) } },
            { sep: true },
          ] : [
            { label: `Insert ${snip.verb.toLowerCase()}…`, run: () => { setMenu(null); setTaskOpen(true) } },
            { sep: true },
          ]),
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

// Play scaffolding: turn a bare task list into a runnable playbook (a play needs
// hosts + tasks). Targets are picked from the operator's inventories (the group
// names their hosts carry) so you don't have to remember them. "Wrap this file"
// nests the whole file's tasks under the header; "Insert header" drops one at the
// cursor for a second play.
function PlayModal({ targets, hasContent, onClose, onWrap, onInsert }) {
  const [name, setName] = useState('Configure hosts')
  const [hosts, setHosts] = useState(targets[0] || 'all')
  const [custom, setCustom] = useState('')
  const [become, setBecome] = useState(true)
  const [gather, setGather] = useState(true)
  const opts = () => ({ name, hosts: hosts === '__custom' ? (custom.trim() || 'all') : hosts, become, gather })

  return (
    <Modal title="Play — target hosts & structure" onClose={onClose}>
      <div className="muted">A playbook is one or more <b>plays</b>; each play points <span className="mono">hosts:</span> at part of your inventory and runs <span className="mono">tasks:</span>. Pick a target and SLEP writes the header.</div>
      <Field label="Play name"><input value={name} onChange={(e) => setName(e.target.value)} autoFocus /></Field>
      <Field label="Target hosts (from your inventories)">
        <select value={hosts} onChange={(e) => setHosts(e.target.value)}>
          {targets.map((t) => <option key={t} value={t}>{t}</option>)}
          <option value="__custom">Custom pattern…</option>
        </select>
      </Field>
      {hosts === '__custom' && <Field label="Pattern (group, host, or wildcard)"><input value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="web:&prod, db-*, 10.0.0.5" /></Field>}
      <div className="row" style={{ gap: 16, marginTop: 4 }}>
        <label className="row" style={{ gap: 7 }}><input type="checkbox" checked={become} onChange={(e) => setBecome(e.target.checked)} style={{ width: 'auto' }} /> become (sudo)</label>
        <label className="row" style={{ gap: 7 }}><input type="checkbox" checked={gather} onChange={(e) => setGather(e.target.checked)} style={{ width: 'auto' }} /> gather_facts</label>
      </div>
      <div className="row" style={{ gap: 8, marginTop: 10 }}>
        <div className="spacer" />
        <button className="ghost" onClick={() => onInsert(opts())}>Insert header at cursor</button>
        <button className="primary" disabled={!hasContent} title={hasContent ? '' : 'Nothing to wrap yet'} onClick={() => onWrap(opts())}>Wrap this file in a play</button>
      </div>
      {!hasContent && <div className="faint" style={{ fontSize: 12 }}>Add tasks first, then “Wrap this file” to make them runnable — or just insert a header to start from the top.</div>}
    </Modal>
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
  const [becomePw, setBecomePw] = useState('')   // per-run sudo override
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
      {engine === 'ansible' && (
        <Field label="Sudo (become) password — optional">
          <input type="password" value={becomePw} autoComplete="off" onChange={(e) => setBecomePw(e.target.value)}
                 placeholder={(creds.find((c) => String(c.id) === String(cred))?.has_become)
                   ? 'credential already has one — leave blank to use it'
                   : 'for become tasks on password-sudo hosts'} />
        </Field>
      )}
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
          extra_vars: extra, become_password: engine === 'ansible' ? becomePw : '',
        } })
        onClose(); onLaunched(d.run_id)
      })}>▶ Launch</button>
    </Modal>
  )
}
