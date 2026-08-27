import React, { useEffect, useState, useCallback, useRef } from 'react'
import Editor from '@monaco-editor/react'
import { api, getTheme } from '../api.js'
import { Field, Modal, useErr } from '../ui.jsx'
import { SNIPPET_GROUPS as ANSIBLE_SNIPPETS, PLAY_GROUPS } from '../ansibleSnippets.js'
import CollectionsInstall from '../components/CollectionsInstall.jsx'
import { SNIPPET_GROUPS as TERRAFORM_SNIPPETS } from '../terraformSnippets.js'
import { SNIPPET_GROUPS as SALT_SNIPPETS } from '../saltSnippets.js'
import { SNIPPET_GROUPS as ANSIBLECFG_SNIPPETS } from '../ansibleCfgSnippets.js'
import { CreateWizard } from './Infrastructure.jsx'

// Mirror of backend/_ansible_group: INI group names allow only letters, digits
// and underscores, and can't start with a digit. Keep in sync so the play-target
// dropdown shows the same group the generated inventory will actually contain.
const ansibleGroup = (name) => {
  let g = (name || '').trim().replace(/[^A-Za-z0-9_]/g, '_')
  if (g && g[0] >= '0' && g[0] <= '9') g = 'g_' + g
  return g || 'ungrouped'
}

// Ansible facts + magic variables offered as editor autocomplete. `d` documents
// the value; entries with dot paths (ansible_default_ipv4.address) insert the
// whole path. Kept practical, not exhaustive — the ones people reach for.
const ANSIBLE_VARS = [
  // Preferred modern form: ansible_facts['name'] (top-level ansible_* fact vars
  // are deprecated). `true` = preferred → sorted first in the completion list.
  ["ansible_facts['hostname']", 'short hostname (preferred over ansible_hostname)', true],
  ["ansible_facts['fqdn']", 'fully-qualified domain name', true],
  ["ansible_facts['default_ipv4']['address']", 'primary IPv4 address', true],
  ["ansible_facts['default_ipv4']['gateway']", 'default gateway', true],
  ["ansible_facts['distribution']", 'e.g. Ubuntu, Rocky, Debian, Archlinux', true],
  ["ansible_facts['distribution_version']", 'e.g. 22.04', true],
  ["ansible_facts['distribution_major_version']", 'e.g. 22', true],
  ["ansible_facts['os_family']", 'e.g. Debian, RedHat, Suse, Archlinux', true],
  ["ansible_facts['architecture']", 'e.g. x86_64, aarch64', true],
  ["ansible_facts['kernel']", 'running kernel version', true],
  ["ansible_facts['processor_vcpus']", 'number of vCPUs', true],
  ["ansible_facts['memtotal_mb']", 'total RAM in MB', true],
  ["ansible_facts['pkg_mgr']", 'package manager, e.g. apt, dnf', true],
  ["ansible_facts['service_mgr']", 'init system, e.g. systemd', true],
  ["ansible_facts['python_version']", 'Python version on the target', true],
  ["ansible_facts['date_time']['iso8601']", 'current time, ISO-8601', true],
  ["ansible_facts['all_ipv4_addresses']", 'list of all IPv4 addresses', true],
  // Legacy top-level fact vars (still work; kept for familiarity).
  ['ansible_hostname', 'short hostname of the target'],
  ['ansible_fqdn', 'fully-qualified domain name'],
  ['ansible_nodename', 'hostname as the OS reports it (uname -n)'],
  ['ansible_domain', 'DNS domain of the host'],
  ['ansible_distribution', 'e.g. Ubuntu, Rocky, Debian, Archlinux'],
  ['ansible_distribution_version', 'e.g. 22.04'],
  ['ansible_distribution_major_version', 'e.g. 22'],
  ['ansible_distribution_release', 'e.g. jammy'],
  ['ansible_os_family', 'e.g. Debian, RedHat, Suse, Archlinux'],
  ['ansible_system', 'e.g. Linux'],
  ['ansible_kernel', 'running kernel version'],
  ['ansible_architecture', 'e.g. x86_64, aarch64'],
  ['ansible_machine', 'hardware machine type'],
  ['ansible_processor_vcpus', 'number of vCPUs'],
  ['ansible_processor_cores', 'CPU cores per socket'],
  ['ansible_memtotal_mb', 'total RAM in MB'],
  ['ansible_memfree_mb', 'free RAM in MB'],
  ['ansible_default_ipv4.address', 'primary IPv4 address'],
  ['ansible_default_ipv4.gateway', 'default gateway'],
  ['ansible_default_ipv4.interface', 'primary interface name'],
  ['ansible_all_ipv4_addresses', 'list of all IPv4 addresses'],
  ['ansible_interfaces', 'list of network interface names'],
  ['ansible_default_ipv6.address', 'primary IPv6 address'],
  ['ansible_dns.nameservers', 'configured DNS servers'],
  ['ansible_env', 'dict of the remote user’s environment'],
  ['ansible_user_id', 'remote user name'],
  ['ansible_user_dir', 'remote user home directory'],
  ['ansible_python_version', 'Python version on the target'],
  ['ansible_python.executable', 'Python interpreter path'],
  ['ansible_date_time.iso8601', 'current time, ISO-8601'],
  ['ansible_date_time.date', 'current date (YYYY-MM-DD)'],
  ['ansible_date_time.epoch', 'current time, epoch seconds'],
  ['ansible_uptime_seconds', 'seconds since boot'],
  ['ansible_mounts', 'list of mounted filesystems'],
  ['ansible_devices', 'dict of block devices'],
  ['ansible_selinux.status', 'SELinux status'],
  ['ansible_service_mgr', 'init system, e.g. systemd'],
  ['ansible_pkg_mgr', 'package manager, e.g. apt, dnf'],
  ['ansible_virtualization_type', 'e.g. kvm, docker, VMware'],
  ['ansible_facts', 'the full facts dict (ansible_facts.hostname, …)'],
  // magic / connection variables
  ['inventory_hostname', 'name of the current host in the inventory'],
  ['inventory_hostname_short', 'inventory hostname up to the first dot'],
  ['group_names', 'list of groups the current host is in'],
  ['groups', 'dict of all groups → their hosts'],
  ['hostvars', 'dict of every host’s variables (hostvars[name])'],
  ['ansible_play_hosts', 'hosts remaining in the current play'],
  ['ansible_host', 'address Ansible connects to'],
  ['ansible_port', 'SSH port'],
  ['ansible_user', 'SSH user'],
  ['ansible_become_user', 'user to become (sudo)'],
  ['inventory_dir', 'directory of the inventory source'],
  ['playbook_dir', 'directory of the running playbook'],
  ['role_name', 'name of the current role'],
  ['role_path', 'path of the current role'],
  ['ansible_version.full', 'Ansible version string'],
  ['item', 'the current item in a loop'],
  ['ansible_loop.index', '1-based loop index'],
  ['omit', 'placeholder to skip a parameter'],
]

// Register Ansible-variable autocomplete once per Monaco instance. Suggestions
// appear inside a {{ … }} expression, or when the token being typed looks like an
// Ansible variable (ansible_*, inventory_*, groups, hostvars, item, …).
let _ansibleSetup = false
function registerAnsible(monaco) {
  if (_ansibleSetup) return
  _ansibleSetup = true
  monaco.languages.registerCompletionItemProvider('yaml', {
    triggerCharacters: ['_', '.', '{', ' '],
    provideCompletionItems(model, position) {
      const line = model.getValueInRange({ startLineNumber: position.lineNumber, startColumn: 1, endLineNumber: position.lineNumber, endColumn: position.column })
      const token = (line.match(/[\w.]*$/) || [''])[0]
      const inJinja = line.lastIndexOf('{{') > line.lastIndexOf('}}')
      if (!inJinja && !/^(ansible|inventory|group|host|item|play|role|omit)/i.test(token)) return { suggestions: [] }
      // Replace the whole dotted token so "ansible_default_ipv4." completes cleanly.
      const range = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: position.column - token.length, endColumn: position.column }
      return {
        suggestions: ANSIBLE_VARS.map(([label, doc, pref]) => ({
          label, kind: monaco.languages.CompletionItemKind.Variable,
          insertText: label, detail: pref ? 'Ansible fact (preferred)' : 'Ansible variable',
          documentation: doc, range, sortText: (pref ? '0' : '1') + label,
        })),
      }
    },
  })
}

// A lightweight YAML syntax checker — no full parser (airgap-friendly), just the
// gotchas that actually bite: tab indentation, and unbalanced Jinja/flow brackets.
// Returns markers {line, col, endCol, message, severity}.
function lintYaml(text) {
  const out = []
  const M = (line, col, endCol, message, severity) => out.push({ line, col, endCol, message, severity })
  text.split('\n').forEach((raw, i) => {
    const line = i + 1
    const indent = (raw.match(/^[ \t]*/) || [''])[0]
    const tab = indent.indexOf('\t')
    if (tab >= 0) M(line, tab + 1, indent.length + 1, 'YAML forbids tabs for indentation — use spaces.', 'error')
    const s = raw.replace(/#.*/, '')                       // drop trailing comment (naive)
    const count = (str, re) => (str.match(re) || []).length
    if (count(s, /\{\{/g) !== count(s, /\}\}/g)) M(line, 1, raw.length + 1, "Unbalanced '{{ … }}' on this line.", 'warning')
    if (count(s, /\{%/g) !== count(s, /%\}/g)) M(line, 1, raw.length + 1, "Unbalanced '{% … %}' on this line.", 'warning')
    // Flow collections: strip Jinja + quoted strings, then balance [] and {}.
    const c = s.replace(/\{\{[\s\S]*?\}\}|\{%[\s\S]*?%\}|\{#[\s\S]*?#\}/g, '')
      .replace(/"(?:\\.|[^"\\])*"|'[^']*'/g, '')
    if (count(c, /\[/g) !== count(c, /\]/g)) M(line, 1, raw.length + 1, "Unbalanced '[ ]' on this line.", 'warning')
    if (count(c, /\{/g) !== count(c, /\}/g)) M(line, 1, raw.length + 1, "Unbalanced '{ }' on this line.", 'warning')
    // Exact apt/deb version pin (e.g. nginx=1.24.0-2ubuntu7.11). Ubuntu keeps only the
    // current build in its pool, so a pinned one 404s once a security update replaces it
    // (a common, confusing 'Failed to fetch … 404' at apt time). Require a Debian-style
    // revision (…-N / …~ / …+) after '=' so plain key=value / '==' comparisons don't trip it.
    const pin = /(^|[\s,\[-])([A-Za-z][A-Za-z0-9.+-]*)=([0-9][A-Za-z0-9.+~:]*[-~+][A-Za-z0-9.+~:]*)/.exec(s)
    if (pin) {
      const col = pin.index + pin[0].indexOf(pin[2]) + 1
      M(line, col, col + pin[2].length + 1 + pin[3].length,
        `Exact version pin "${pin[2]}=${pin[3]}" — Ubuntu drops superseded builds from the pool, so this 404s after a security update. Install "${pin[2]}" unpinned for the current version (or expect to bump this pin).`,
        'warning')
    }
  })
  return out
}

const langFor = (path) => {
  if (path.endsWith('ansible.cfg') || path.endsWith('.ini') || path.endsWith('.cfg')) return 'ini'
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
  // ansible.cfg gets a settings palette (sections + options at their defaults) so
  // the config can be assembled from the menu rather than remembered/hand-typed.
  if (p === 'ansible.cfg' || p.endsWith('/ansible.cfg')) return { groups: ANSIBLECFG_SNIPPETS, verb: 'Setting', engine: 'ansiblecfg' }
  if (p.endsWith('.tf') || p.endsWith('.hcl')) return { groups: TERRAFORM_SNIPPETS, verb: 'Resource', engine: 'terraform' }
  if (p.endsWith('.sls')) return { groups: SALT_SNIPPETS, verb: 'State', engine: 'salt' }
  // Ansible palette leads with playbook-structure blocks, then the task library.
  return { groups: [...PLAY_GROUPS, ...ANSIBLE_SNIPPETS], verb: 'Task', engine: 'ansible' }
}

export default function Ide({ project, onBack, onRun, onInfraChanged, theme }) {
  const [tree, setTree] = useState([])
  const [path, setPath] = useState(null)
  const [content, setContent] = useState('')
  const [saved, setSaved] = useState(true)
  const [autosave, setAutosave] = useState(() => { try { return localStorage.getItem('slep.autosave') === '1' } catch { return false } })
  const [runOpen, setRunOpen] = useState(false)
  const [pipeOpen, setPipeOpen] = useState(false)
  const [newOpen, setNewOpen] = useState(false)
  const [infraWizOpen, setInfraWizOpen] = useState(false)   // Build-infrastructure wizard for THIS project
  const [isInfra, setIsInfra] = useState(false)             // already an infra project? (hides "Build infra")
  useEffect(() => { api('infra').then((d) => setIsInfra((d.infra || []).some((x) => x.project_id === project.id))).catch(() => {}) }, [project.id])
  const [taskOpen, setTaskOpen] = useState(false)
  const [playOpen, setPlayOpen] = useState(false)
  const [collOpen, setCollOpen] = useState(false)
  const [targets, setTargets] = useState(['all', 'localhost'])   // host patterns for `hosts:`
  const [delPath, setDelPath] = useState(null)
  const [renPath, setRenPath] = useState(null)
  const [gitOpen, setGitOpen] = useState(false)
  const [menu, setMenu] = useState(null)   // {x, y} custom editor context menu
  const [treeW, setTreeW] = useState(() => { try { return Number(localStorage.getItem('slep.treeW')) || 224 } catch { return 224 } })
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
    monaco.editor.defineTheme('sysible-light', {
      base: 'vs', inherit: true, rules: [],
      colors: {
        'editor.background': '#ffffff',
        'editor.foreground': '#131a26',
        'editorLineNumber.foreground': '#9aa7bd',
        'editorLineNumber.activeForeground': '#556072',
        'editorCursor.foreground': '#2e7d32',
        'editor.selectionBackground': '#cfe3d3',
        'editor.lineHighlightBackground': '#f1f4f9',
        'editorIndentGuide.background': '#e5e9f0',
        'editorGutter.background': '#ffffff',
      },
    })
  }
  const monacoRef = useRef(null)

  // Before mount: brand theme + Ansible-variable autocomplete.
  const beforeMount = (monaco) => { defineTheme(monaco); registerAnsible(monaco) }

  // Lightweight YAML syntax check → editor squiggles/markers. YAML files only.
  const validate = () => {
    const ed = editorRef.current, monaco = monacoRef.current
    if (!ed || !monaco) return
    const model = ed.getModel(); if (!model) return
    if (langFor(path || '') !== 'yaml') { monaco.editor.setModelMarkers(model, 'slep-yaml', []); return }
    const markers = lintYaml(model.getValue()).map((m) => ({
      startLineNumber: m.line, startColumn: m.col, endLineNumber: m.line, endColumn: m.endCol,
      message: m.message, severity: m.severity === 'error' ? monaco.MarkerSeverity.Error : monaco.MarkerSeverity.Warning,
    }))
    monaco.editor.setModelMarkers(model, 'slep-yaml', markers)
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
  // Delete the selection (or the current line when nothing is selected), no clipboard.
  const doDelete = (ed) => {
    const sel = ed.getSelection()
    if (sel && !sel.isEmpty()) ed.executeEdits('delete', [{ range: sel, text: '', forceMoveMarkers: true }])
    else ed.trigger('menu', 'editor.action.deleteLines')
    setSaved(false); ed.focus()
  }
  const menuAction = async (fn) => { const ed = editorRef.current; setMenu(null); if (ed) await fn(ed) }

  const loadTree = useCallback(() => api(`projects/${project.id}/files`).then((d) => setTree(d.files)), [project.id])
  useEffect(() => { loadTree() }, [loadTree])
  // Opened with a target file (e.g. the cadence "Configure"/"Maintain" scaffold)?
  // Open it once on mount so the operator lands directly in that file.
  useEffect(() => { if (project.openFile) open(project.openFile) }, [project.id])

  const open = async (p) => { const d = await api(`projects/${project.id}/file?path=${encodeURIComponent(p)}`); setPath(p); setContent(d.content); setSaved(true) }
  // Open the project's ansible.cfg — the equivalent-of-ansible.cfg configuration
  // file. Seed it from SLEP's starter template on first use, then edit it like any
  // file (it's real ansible.cfg on disk that the runner reads at run time).
  const openConfig = async () => {
    await api(`projects/${project.id}/config/default`, { method: 'POST' })
    await loadTree()
    await open('ansible.cfg')
  }

  // Delete via an in-app dialog (native confirm() can be suppressed by the
  // browser, which silently blocked deletes). try/catch surfaces any error.
  const remove = async (p) => {
    try {
      await api(`projects/${project.id}/file?path=${encodeURIComponent(p)}`, { method: 'DELETE' })
      if (p === path) { setPath(null); setContent('') }
      await loadTree()
    } catch (e) { alert('Could not delete ' + p + ': ' + e.message) }
  }

  // Rename/move a file; keep it open under its new name if it was the open file.
  const rename = async (from, to) => {
    await api(`projects/${project.id}/file/rename`, { method: 'POST', json: { from, to } })
    if (from === path) setPath(to)
    await loadTree()
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

  // Autosave: when enabled, persist the open file a short beat after edits stop
  // (each keystroke resets the timer, so it fires once you pause). The preference
  // is remembered across sessions. Manual Save / Ctrl+S still work alongside it.
  const toggleAutosave = (on) => { setAutosave(on); try { localStorage.setItem('slep.autosave', on ? '1' : '0') } catch { /* ignore */ } }
  useEffect(() => {
    if (!autosave || saved || path == null) return
    const t = setTimeout(() => { save() }, 800)
    return () => clearTimeout(t)
  }, [autosave, saved, path, content, save])

  // Re-lint when the open file changes (switching files doesn't fire onChange).
  useEffect(() => { validate() }, [path])

  // Resizable file Explorer: drag the divider to widen it so long paths aren't
  // clipped; the width is remembered across sessions.
  useEffect(() => { try { localStorage.setItem('slep.treeW', String(treeW)) } catch { /* ignore */ } }, [treeW])
  const startTreeDrag = (e) => {
    e.preventDefault()
    const startX = e.clientX, startW = treeW
    const move = (ev) => setTreeW(Math.max(150, Math.min(560, startW + (ev.clientX - startX))))
    const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); document.body.style.cursor = '' }
    document.body.style.cursor = 'col-resize'
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', up)
  }

  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <button className="ghost sm" onClick={onBack}>← Projects</button>
        <h2 style={{ margin: 0 }}>{project.name}</h2><span className="muted">{project.slug}</span>
      </div>
      <div className="ide ide-3col" style={{ gridTemplateColumns: `150px ${treeW}px 6px 1fr` }}>
        <div className="ide-actions">
          <div className="ide-actions-h">Actions</div>
          <button className="ghost sm" onClick={() => setNewOpen(true)}>＋ New file</button>
          {!isInfra && (
            <button className="ghost sm" title="Generate a full, working Terraform project here with the infrastructure wizard"
              onClick={() => setInfraWizOpen(true)}>Build infra</button>
          )}
          {snip.engine === 'ansible' && (
            <button className="ghost sm" onClick={() => setPlayOpen(true)} disabled={path == null}
              title="Wrap this file in a play, or insert a play header (hosts, become, tasks)">Add Play</button>
          )}
          {snip.engine === 'ansible' && (
            <button className="ghost sm" onClick={() => setCollOpen(true)}
              title="Install the Ansible Galaxy collections the modules need">Collections</button>
          )}
          {snip.engine === 'ansible' && (
            <button className="ghost sm" onClick={openConfig}
              title="Edit this project's ansible.cfg — forks, host key checking, become, SSH, roles/collections paths">Config</button>
          )}
          <button className="ghost sm" onClick={() => setTaskOpen(true)} disabled={path == null}
            title={`Insert a ready-made ${snip.engine} ${snip.verb.toLowerCase()} at the cursor`}>Add {snip.verb}</button>
          <button className="ghost sm" onClick={save} disabled={path == null || (autosave && saved)}
            title={autosave ? 'Autosave is on — edits save automatically' : 'Save (Ctrl+S)'}>
            {saved ? 'Saved' : (autosave ? 'Saving…' : 'Save')}</button>
          <label className="row autosave-toggle" style={{ gap: 5, fontSize: 12, cursor: 'pointer', padding: '0 2px' }}
            title="Automatically save edits a moment after you stop typing">
            <input type="checkbox" checked={autosave} onChange={(e) => toggleAutosave(e.target.checked)} style={{ width: 'auto' }} /> Autosave
          </label>
          <button className="ghost sm" title="Version control — commit, push, pull, branches" onClick={() => setGitOpen(true)}>⎇ Git</button>
          <div className="spacer" />
          <button className="ghost sm" title="Run several steps as one pipeline (build → configure → maintain)" onClick={() => setPipeOpen(true)}>Pipeline</button>
          <button className="primary sm" onClick={() => setRunOpen(true)}>Run</button>
        </div>
        <div className="tree">
          <div className="tree-h"><span>Explorer</span><button className="tree-h-btn" title="New file" onClick={() => setNewOpen(true)}>＋</button></div>
          {tree.length === 0 && <div className="muted" style={{ padding: 6, fontSize: 12 }}>Empty project. Click “＋ New file” to start from a template — Ansible playbook, Salt state, or Terraform.</div>}
          {tree.map((f) => {
            const depth = f.path.split('/').length - 1
            const base = f.path.split('/').pop()
            return (
              <div key={f.path} className={'f ' + (f.type === 'dir' ? 'dir' : '') + (f.path === path ? ' active' : '')} style={{ paddingLeft: 8 + depth * 13 }}>
                <span className="f-name" title={f.path} onClick={() => f.type === 'file' && open(f.path)}>{f.type === 'dir' ? '📁 ' : '📄 '}{base}</span>
                {f.type === 'file' && <button className="f-del" title={'Rename ' + f.path} onClick={(e) => { e.stopPropagation(); setRenPath(f.path) }}>✎</button>}
                <button className="f-del" title={'Delete ' + f.path} onClick={(e) => { e.stopPropagation(); setDelPath(f.path) }}>✕</button>
              </div>
            )
          })}
        </div>
        <div className="ide-gutter" onMouseDown={startTreeDrag} title="Drag to resize the Explorer" />
        <div className="edwrap" onContextMenu={(e) => { if (path != null) { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY }) } }}>
          <div className="edtool"><span className="muted">{path || 'No file open'}{!saved && ' •'}</span></div>
          <Editor height="100%" theme={(theme || getTheme()) === 'light' ? 'sysible-light' : 'sysible-dark'} path={path || 'untitled'} language={langFor(path || '')}
            value={content} onChange={(v) => { setContent(v ?? ''); setSaved(false); validate() }}
            beforeMount={beforeMount}
            onMount={(ed, monaco) => { editorRef.current = ed; monacoRef.current = monaco; validate() }}
            options={{ minimap: { enabled: false }, fontSize: 13, automaticLayout: true, contextmenu: false }} />
        </div>
      </div>
      {newOpen && <NewFile project={project} onClose={() => setNewOpen(false)} onCreated={(p) => { setNewOpen(false); loadTree(); open(p) }} />}
      {infraWizOpen && <CreateWizard project={project} onClose={() => setInfraWizOpen(false)}
        onDone={() => { setInfraWizOpen(false); setIsInfra(true); loadTree(); open('main.tf'); onInfraChanged && onInfraChanged() }} />}
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
      {renPath && <RenameFile from={renPath} onClose={() => setRenPath(null)}
        onRename={async (to) => { await rename(renPath, to); setRenPath(null) }} />}
      {gitOpen && <GitPanel project={project} onClose={() => setGitOpen(false)} onChanged={() => loadTree()} />}
      {runOpen && <RunModal project={project} currentFile={path} onClose={() => setRunOpen(false)} onLaunched={onRun} />}
      {pipeOpen && <PipelineModal project={project} currentFile={path} onClose={() => setPipeOpen(false)} onLaunched={onRun} />}
      {menu && <EditorMenu at={menu} onClose={() => setMenu(null)}
        items={[
          { label: 'Cut', accel: 'Ctrl+X', run: () => menuAction(doCut) },
          { label: 'Copy', accel: 'Ctrl+C', run: () => menuAction(doCopy) },
          { label: 'Paste', accel: 'Ctrl+V', run: () => menuAction(doPaste) },
          { label: 'Delete', accel: 'Del', run: () => menuAction(doDelete) },
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

// Starter templates so a fresh project can bootstrap any engine — you pick the
// kind and get a runnable skeleton, instead of a blank file you have to know how to
// fill. Each is [label, default path, content].
const FILE_TEMPLATES = [
  ['Ansible playbook', 'site.yml',
    '---\n- name: Configure hosts\n  hosts: all\n  become: true\n  tasks:\n' +
    '    - name: Ping the host\n      ansible.builtin.ping:\n'],
  ['Salt state', 'states/web.sls',
    '# Salt state — apply with the Salt engine (state.apply / highstate).\n' +
    'install_nginx:\n  pkg.installed:\n    - name: nginx\n\nnginx_running:\n' +
    '  service.running:\n    - name: nginx\n    - enable: true\n'],
  ['Terraform', 'main.tf',
    '# Terraform — run with the Terraform/OpenTofu engine (plan / apply / destroy).\n' +
    '# For libvirt/cloud VMs, “Create infrastructure” in Projects scaffolds a full,\n' +
    '# working project; this is a blank starting point.\n\n' +
    'terraform {\n  required_providers {\n    # e.g. libvirt = { source = "dmacvicar/libvirt", version = "~> 0.7.0" }\n  }\n}\n'],
  ['Empty file', '', ''],
]

function NewFile({ project, onClose, onCreated }) {
  const [p, setP] = useState('')
  const [tpl, setTpl] = useState(null)   // index into FILE_TEMPLATES, or null
  const { wrap, node } = useErr()
  const create = (path, content) => wrap(async () => {
    if (!path.trim()) throw new Error('Enter a file path.')
    await api(`projects/${project.id}/file`, { method: 'POST', json: { path: path.trim(), type: 'file', content: content || '' } })
    onCreated(path.trim())
  })
  return (
    <Modal title="New file" onClose={onClose}>
      <div className="muted" style={{ marginBottom: 8, fontSize: 13 }}>Start from a template, or make an empty file.</div>
      <div className="col" style={{ gap: 6, marginBottom: 12 }}>
        {FILE_TEMPLATES.map(([label, path, content], i) => (
          <button key={label} className={'ghost' + (tpl === i ? ' active' : '')} style={{ justifyContent: 'flex-start' }}
            onClick={() => { setTpl(i); setP(path) }}>{label}{path ? <span className="faint" style={{ marginLeft: 8 }}>{path}</span> : null}</button>
        ))}
      </div>
      <Field label="Path"><input value={p} onChange={(e) => setP(e.target.value)} autoFocus placeholder="site.yml, main.tf, states/web.sls" /></Field>
      {node}
      <button className="primary" onClick={() => create(p, tpl != null ? FILE_TEMPLATES[tpl][2] : '')}>Create</button>
    </Modal>
  )
}

function RenameFile({ from, onClose, onRename }) {
  const [to, setTo] = useState(from)
  const { wrap, node } = useErr()
  const submit = () => wrap(async () => {
    const t = to.trim()
    if (!t) throw new Error('Enter a new name.')
    if (t === from) { onClose(); return }
    await onRename(t)
  })
  return (
    <Modal title={`Rename ${from}`} onClose={onClose}>
      <Field label="New name / path"><input value={to} autoFocus onChange={(e) => setTo(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') submit() }} placeholder="new-name.yml or path/to/file.yml" /></Field>
      <div className="muted">Include a path to move it (e.g. <span className="mono">roles/web/tasks.yml</span>).</div>
      {node}
      <button className="primary" onClick={submit}>Rename</button>
    </Modal>
  )
}

// Full git-ops for a project: status, stage-all commit, push/pull to a remote
// (with an encrypted token), branch create/checkout, and a recent-commits log.
function GitPanel({ project, onClose, onChanged }) {
  const [st, setSt] = useState(null)        // null = loading
  const [log, setLog] = useState([])
  const [branches, setBranches] = useState([])
  const [msg, setMsg] = useState('')
  const [newBranch, setNewBranch] = useState('')
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState('')
  const [flash, setFlash] = useState('')
  const { wrap, node, setErr } = useErr()
  const base = `projects/${project.id}/git`

  const refresh = async () => {
    const s = await api(`${base}/status`)
    setSt(s); setUrl(s.remote || '')
    if (s.repo) {
      api(`${base}/log`).then((d) => setLog(d.commits)).catch(() => {})
      api(`${base}/branches`).then((d) => setBranches(d.branches)).catch(() => {})
    }
  }
  useEffect(() => { refresh().catch((e) => setErr(e.message)) }, [])

  const act = (label, fn) => wrap(async () => {
    setBusy(label); setFlash('')
    try { const r = await fn(); if (r && r.output) setFlash(r.output); await refresh(); onChanged && onChanged() }
    finally { setBusy('') }
  })

  if (st === null) return <Modal title="Git" onClose={onClose}><div className="muted">Loading…</div>{node}</Modal>

  if (!st.repo) return (
    <Modal title={`Git — ${project.name}`} onClose={onClose}>
      <div className="muted">This project isn’t under version control yet.</div>
      {node}
      <button className="primary" disabled={busy} onClick={() => act('init', () => api(`${base}/init`, { method: 'POST' }))}>Initialize git repository</button>
    </Modal>
  )

  const changed = st.files || []
  return (
    <Modal title={`Git — ${project.name}`} onClose={onClose} wide>
      <div className="row" style={{ gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <span className="pill">⎇ {st.branch}</span>
        {st.ahead > 0 && <span className="pill" title="commits to push">↑ {st.ahead}</span>}
        {st.behind > 0 && <span className="pill" title="commits to pull">↓ {st.behind}</span>}
        <select value="" onChange={(e) => e.target.value && act('checkout', () => api(`${base}/checkout`, { method: 'POST', json: { branch: e.target.value } }))}>
          <option value="">Switch branch…</option>
          {branches.filter((b) => b !== st.branch).map((b) => <option key={b} value={b}>{b}</option>)}
        </select>
        <input style={{ width: 150 }} placeholder="new branch…" value={newBranch} onChange={(e) => setNewBranch(e.target.value)} />
        <button className="ghost sm" disabled={busy || !newBranch.trim()} onClick={() => act('checkout', async () => { const r = await api(`${base}/checkout`, { method: 'POST', json: { branch: newBranch.trim(), create: true } }); setNewBranch(''); return r })}>Create</button>
        <div className="spacer" />
        <button className="ghost sm" disabled={busy || !st.remote} onClick={() => act('pull', () => api(`${base}/pull`, { method: 'POST' }))}>{busy === 'pull' ? 'Pulling…' : `↓ Pull`}</button>
        <button className="ghost sm" disabled={busy || !st.remote} onClick={() => act('push', () => api(`${base}/push`, { method: 'POST' }))}>{busy === 'push' ? 'Pushing…' : `↑ Push`}</button>
      </div>

      <div className="git-cols">
        <div className="job-col">
          <div className="pane-title">Changes ({changed.length})</div>
          <div style={{ maxHeight: '34vh', overflow: 'auto', border: '1px solid var(--line)', borderRadius: 8 }}>
            {changed.length === 0 ? <div className="muted" style={{ padding: 10 }}>Working tree clean.</div>
              : changed.map((f) => (
                <div key={f.path} className="row" style={{ gap: 8, padding: '4px 10px', borderBottom: '1px solid var(--line)' }}>
                  <span className="mono" style={{ width: 26, color: f.untracked ? 'var(--warn)' : 'var(--accent2)' }}>{(f.x + f.y).trim() || '•'}</span>
                  <span className="mono" style={{ fontSize: 12 }}>{f.path}</span>
                </div>
              ))}
          </div>
          <Field label="Commit message"><input value={msg} onChange={(e) => setMsg(e.target.value)} placeholder="what changed" /></Field>
          <button className="primary" disabled={busy || changed.length === 0 || !msg.trim()}
            onClick={() => act('commit', async () => { const r = await api(`${base}/commit`, { method: 'POST', json: { message: msg.trim() } }); setMsg(''); return r })}>{busy === 'commit' ? 'Committing…' : `Commit all (${changed.length})`}</button>
        </div>

        <div className="job-col">
          <div className="pane-title">Remote</div>
          <Field label="Remote URL (origin)"><input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/org/repo.git" /></Field>
          <Field label={st.has_token ? 'Access token (set — leave blank to keep)' : 'Access token (for private push/pull)'}>
            <input type="password" value={token} autoComplete="off" onChange={(e) => setToken(e.target.value)} placeholder="ghp_… / gitlab PAT" />
          </Field>
          <button className="ghost sm" disabled={busy} onClick={() => act('remote', () => api(`${base}/remote`, { method: 'POST', json: { url: url.trim(), ...(token ? { token } : {}) } }).then((r) => { setToken(''); return r }))}>Save remote</button>
          <div className="pane-title" style={{ marginTop: 12 }}>Recent commits</div>
          <div style={{ maxHeight: '24vh', overflow: 'auto' }}>
            {log.length === 0 ? <div className="muted">No commits yet.</div>
              : log.map((c) => (
                <div key={c.hash} className="row" style={{ gap: 8, fontSize: 12, padding: '2px 0' }}>
                  <span className="mono" style={{ color: 'var(--accent)' }}>{c.hash}</span>
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.subject}</span>
                  <span className="faint">{c.when}</span>
                </div>
              ))}
          </div>
        </div>
      </div>

      {flash && <pre className="log" style={{ marginTop: 8, maxHeight: '18vh' }}>{flash}</pre>}
      {node}
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

// Small name-only dialog — used for the inline "＋ New inventory…" in the Run and
// pipeline inventory pickers, so you can make one without leaving the flow.
function NameModal({ title, label, placeholder, onClose, onSubmit }) {
  const [v, setV] = useState('')
  const { wrap, node } = useErr()
  const go = () => wrap(async () => { await onSubmit(v) })
  return (
    <Modal title={title} onClose={onClose}>
      <Field label={label}><input autoFocus value={v} placeholder={placeholder}
        onChange={(e) => setV(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && v.trim()) go() }} /></Field>
      {node}
      <button className="primary" disabled={!v.trim()} onClick={go}>Create</button>
    </Modal>
  )
}

// Target picker for Ansible/Salt: a real dropdown that always lists every
// matching file in the project (a bare <datalist> filters its options by the
// text already in the box, so a pre-filled "main.yml" hides the rest). The
// dropdown shows the full list whenever the project has files — even when the
// current value isn't one of them (it appears as the selected option, with every
// project file listed below it). "✎ Custom path…" swaps to a free text input for
// an arbitrary path; the ▾ button switches back to the list.
function FileTarget({ value, onChange, files, placeholder }) {
  const [custom, setCustom] = useState(false)
  const inList = files.includes(value)
  if (custom || files.length === 0) {
    return (
      <span className="row" style={{ gap: 4, flex: 1 }}>
        <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} style={{ flex: 1 }} autoFocus />
        {files.length > 0 && <button type="button" className="ghost sm" title="Pick from the project's files" onClick={() => setCustom(false)}>▾ list</button>}
      </span>
    )
  }
  return (
    <select value={value} title="Playbook / state to run"
      onChange={(e) => { if (e.target.value === '__custom') setCustom(true); else onChange(e.target.value) }}>
      {!value && <option value="">Select a file…</option>}
      {!inList && value && <option value={value}>{value}</option>}
      {files.map((p) => <option key={p} value={p}>{p}</option>)}
      <option value="__custom">✎ Custom path…</option>
    </select>
  )
}

export function RunModal({ project, currentFile, onClose, onLaunched, initial }) {
  const ini = initial || {}
  const [engine, setEngine] = useState(ini.engine || 'ansible')
  const [target, setTarget] = useState(ini.target || currentFile || 'site.yml')
  const [invs, setInvs] = useState([]); const [creds, setCreds] = useState([])
  const [inv, setInv] = useState(ini.inventory_id ? String(ini.inventory_id) : '')
  const [newInvOpen, setNewInvOpen] = useState(false)
  const [cred, setCred] = useState(ini.credential_id ? String(ini.credential_id) : '')
  const [vars, setVars] = useState('')       // KEY=value per line
  const [saltTest, setSaltTest] = useState(false)
  const [tfTool, setTfTool] = useState(ini.tool || 'terraform')   // terraform | tofu
  const [becomePw, setBecomePw] = useState('')   // per-run sudo override
  const [limit, setLimit] = useState(ini.limit || '')          // --limit
  const [startAt, setStartAt] = useState(ini.start_at_task || '')  // --start-at-task
  const startTasks = ini.tasks || []           // task names for the start-at dropdown
  const [files, setFiles] = useState([])       // project files → target autocomplete
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

  // Load inventories; also called on dropdown focus so an inventory created since
  // the modal opened (e.g. in Infrastructure) shows up without reopening.
  const loadInvs = () => api('inventories').then((d) => { setInvs(d.inventories); setInv((cur) => cur || (d.inventories[0] ? String(d.inventories[0].id) : '')) })
  useEffect(() => { loadInvs() }, [])
  // Default the SSH credential to the ACCOUNT this project's VMs were built with —
  // the deploy credential set in ⚙ Access (or SLEP's managed key) — so Ansible and
  // Salt authenticate as the same account cloud-init created, with no re-picking.
  useEffect(() => {
    api('credentials').then((d) => {
      setCreds(d.credentials)
      if (ini.credential_id || !project?.id) return
      api('infra').then((f) => {
        const row = (f.infra || []).find((x) => x.project_id === project.id)
        if (!row) return
        const managed = (d.credentials || []).find((c) => c.name === 'SLEP managed key')
        const def = row.login_credential_id || row.deploy_credential_id || (managed && managed.id)
        if (def) setCred((cur) => cur || String(def))
      }).catch(() => {})
    })
  }, [])   // eslint-disable-line
  useEffect(() => { if (project?.id) api(`projects/${project.id}/files`)
    .then((d) => setFiles((d.files || []).filter((f) => f.type === 'file').map((f) => f.path))).catch(() => {}) }, [project?.id])
  const targetFiles = files.filter((p) => (engine === 'salt' ? p.endsWith('.sls') : (p.endsWith('.yml') || p.endsWith('.yaml'))))
  const firstEngine = useRef(true)
  useEffect(() => {
    if (firstEngine.current) { firstEngine.current = false; return }   // keep a pre-filled target
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
      {engine === 'terraform' && (
        <Field label="Tool">
          <select value={tfTool} onChange={(e) => setTfTool(e.target.value)}>
            <option value="terraform">Terraform</option>
            <option value="tofu">OpenTofu (tofu)</option>
          </select>
        </Field>
      )}
      <Field label={engine === 'ansible' ? 'Playbook path' : engine === 'terraform' ? 'Action' : 'State (or “highstate”)'}>
        {engine === 'terraform'
          ? <select value={target} onChange={(e) => setTarget(e.target.value)}><option>plan</option><option>apply</option><option>destroy</option></select>
          : <FileTarget value={target} onChange={setTarget} files={targetFiles}
                        placeholder={engine === 'salt' ? 'state.sls / highstate' : 'playbook.yml'} />}
      </Field>
      {needsInv && (
        <Field label="Inventory">
          <select value={inv} onFocus={loadInvs} onChange={(e) => { if (e.target.value === '__new') setNewInvOpen(true); else setInv(e.target.value) }}>
            {invs.length === 0 && <option value="">(no inventories — create one first)</option>}
            {invs.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
            <option value="__new">＋ New inventory…</option>
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
      {engine === 'ansible' && (
        <div className="row" style={{ gap: 10 }}>
          <Field label="Limit to hosts (optional → --limit)"><input value={limit} onChange={(e) => setLimit(e.target.value)} placeholder="rocky-01, web-tier" /></Field>
          <Field label="Start at task (optional → --start-at-task)">
            {startTasks.length
              ? <select value={startAt} onChange={(e) => setStartAt(e.target.value)}>
                  <option value="">(from the beginning)</option>
                  {startTasks.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              : <input value={startAt} onChange={(e) => setStartAt(e.target.value)} placeholder="exact task name" />}
          </Field>
        </div>
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
          limit: engine === 'ansible' ? limit.trim() : '',
          start_at_task: engine === 'ansible' ? startAt.trim() : '',
          tool: engine === 'terraform' ? tfTool : '',
        } })
        onClose(); onLaunched(d.run_id)
      })}>Launch</button>
      {newInvOpen && <NameModal title="New inventory" label="Inventory name" placeholder="prod-web"
        onClose={() => setNewInvOpen(false)}
        onSubmit={async (nm) => {
          const d = await api('inventories', { method: 'POST', json: { name: nm.trim(), project_id: project.id } })
          setInvs((s) => [...s, { id: d.id, name: d.name }]); setInv(String(d.id)); setNewInvOpen(false)
        }} />}
    </Modal>
  )
}

// Run several steps in succession — the "create → configure → maintain" pipeline
// (or any order). Each step becomes a normal run; the sequence stops on the first
// failure by default. Steps run one after another on the server.
export function PipelineModal({ project, currentFile, initialSteps, initialName, saveId, stopDefault, onClose, onLaunched, onSaved }) {
  const [invs, setInvs] = useState([]); const [creds, setCreds] = useState([])
  const [stopOnFail, setStopOnFail] = useState(stopDefault !== undefined ? stopDefault : true)
  const [name, setName] = useState(initialName || '')
  const blank = (over) => ({ kind: 'ansible', target: currentFile || 'site.yml', inventory_id: '', credential_id: '', tool: 'terraform',
    vars: '', becomePw: '', limit: '', startAt: '', saltTest: false, ...over })
  const [steps, setSteps] = useState(initialSteps && initialSteps.length ? initialSteps.map((s) => blank(s)) : [blank()])
  const [openSteps, setOpenSteps] = useState(new Set())   // steps whose ⚙ options panel is expanded
  const toggleOpts = (i) => setOpenSteps((s) => { const n = new Set(s); n.has(i) ? n.delete(i) : n.add(i); return n })
  // Parse a step's "KEY=value per line" box → object (→ -var / -e / pillar); salt
  // test mode rides in as test=True.
  const stepVars = (st) => {
    const out = {}
    for (const raw of (st.vars || '').split('\n')) {
      const l = raw.trim(); if (!l || l.startsWith('#')) continue
      const i = l.indexOf('='); if (i > 0) out[l.slice(0, i).trim()] = l.slice(i + 1).trim()
    }
    if (st.kind === 'salt' && st.saltTest) out.test = 'True'
    return out
  }
  const isPseudo = (k) => k === 'inventory' || k === 'enroll'
  const payloadOf = () => steps.map((st) => ({
    kind: st.kind, target: st.target,
    inventory_id: (st.kind === 'terraform' || isPseudo(st.kind)) ? null : (st.inventory_id ? Number(st.inventory_id) : null),
    credential_id: isPseudo(st.kind) ? null : (st.credential_id ? Number(st.credential_id) : null),
    tool: st.kind === 'terraform' ? st.tool : '',
    extra_vars: stepVars(st),
    become_password: st.kind === 'ansible' ? (st.becomePw || '') : '',
    limit: st.kind === 'ansible' ? (st.limit || '').trim() : '',
    start_at_task: st.kind === 'ansible' ? (st.startAt || '').trim() : '',
  }))
  const { wrap, node } = useErr()

  const [files, setFiles] = useState([])   // project files → target autocomplete
  // Load inventories; re-called on dropdown focus so one created since the modal
  // opened (e.g. in Infrastructure, or built by a prior step) appears without reopening.
  const loadInvs = () => api('inventories').then((d) => { setInvs(d.inventories)
    setSteps((s) => s.map((st) => ({ ...st, inventory_id: st.inventory_id || (d.inventories[0] ? String(d.inventories[0].id) : '') }))) })
  useEffect(() => { loadInvs() }, [])
  // Default each Ansible/Salt step's credential to the account this project's VMs
  // were built with (⚙ Access deploy credential, or SLEP's managed key), so the
  // whole cadence authenticates as one account with no per-step re-picking.
  useEffect(() => {
    api('credentials').then((d) => {
      setCreds(d.credentials)
      if (!project?.id) return
      api('infra').then((f) => {
        const row = (f.infra || []).find((x) => x.project_id === project.id)
        if (!row) return
        const managed = (d.credentials || []).find((c) => c.name === 'SLEP managed key')
        const def = row.login_credential_id || row.deploy_credential_id || (managed && managed.id)
        if (def) setSteps((s) => s.map((st) => (st.kind === 'ansible' || st.kind === 'salt')
          ? { ...st, credential_id: st.credential_id || String(def) } : st))
      }).catch(() => {})
    })
  }, [])   // eslint-disable-line
  useEffect(() => { if (project?.id) api(`projects/${project.id}/files`)
    .then((d) => setFiles((d.files || []).filter((f) => f.type === 'file').map((f) => f.path))).catch(() => {}) }, [project?.id])
  // Files that make sense as a run target for each engine — playbooks for Ansible,
  // state files for Salt — offered as a datalist so the target is picked, not typed.
  const filesFor = (kind) => files.filter((p) => (kind === 'salt' ? p.endsWith('.sls') : (p.endsWith('.yml') || p.endsWith('.yaml'))))

  const [newInvStep, setNewInvStep] = useState(null)   // step index awaiting a new inventory
  const invChange = (i, val) => { if (val === '__new') setNewInvStep(i); else upd(i, { inventory_id: val }) }
  const createInv = async (name) => {
    const d = await api('inventories', { method: 'POST', json: { name: name.trim(), project_id: project.id } })
    setInvs((s) => [...s, { id: d.id, name: d.name }])
    if (newInvStep != null) upd(newInvStep, { inventory_id: String(d.id) })
    setNewInvStep(null)
  }
  const upd = (i, patch) => setSteps((s) => s.map((st, j) => (j === i ? { ...st, ...patch } : st)))
  const add = () => setSteps((s) => [...s, blank({ inventory_id: invs[0] ? String(invs[0].id) : '' })])
  const del = (i) => setSteps((s) => (s.length > 1 ? s.filter((_, j) => j !== i) : s))
  const move = (i, d) => setSteps((s) => { const a = [...s]; const j = i + d; if (j < 0 || j >= a.length) return a;[a[i], a[j]] = [a[j], a[i]]; return a })
  const defTarget = (k) => (k === 'terraform' ? 'apply' : k === 'salt' ? 'highstate' : k === 'inventory' ? 'from VMs' : k === 'enroll' ? '→ Controller' : 'site.yml')

  return (
    <Modal title="Run pipeline" onClose={onClose} wide>
      <div className="faint" style={{ fontSize: 12, marginBottom: 8 }}>Steps run one after another — a common pipeline is build (Terraform/OpenTofu) → configure (Ansible) → maintain (Salt).</div>
      <div className="pipe-head">
        <span className="pipe-n-spacer" />
        <span style={{ width: 130 }}>Engine</span>
        <span style={{ flex: 1 }}>Target</span>
        <span style={{ width: 150 }}>Inventory / tool</span>
        <span style={{ width: 130 }}>Credential</span>
        <span className="pipe-actions-spacer" />
      </div>
      {steps.map((st, i) => (
        <React.Fragment key={i}>
        <div className="pipe-step">
          <span className="pipe-n">{i + 1}</span>
          <select value={st.kind} onChange={(e) => upd(i, { kind: e.target.value, target: defTarget(e.target.value) })} style={{ width: 130 }}>
            <option value="ansible">Ansible</option>
            <option value="terraform">Terraform</option>
            <option value="salt">Salt</option>
            <option value="inventory">Inventory (from VMs)</option>
            <option value="enroll">Enroll → Controller</option>
          </select>
          {st.kind === 'inventory'
            ? <span className="muted" style={{ flex: 1, fontSize: 12.5 }}>Reads the applied VMs into this project’s inventory, then points the Ansible/Salt steps below at it.</span>
            : st.kind === 'enroll'
            ? <span className="muted" style={{ flex: 1, fontSize: 12.5 }}>Registers the applied VMs into this project’s Controller as SSH hosts. Set the Controller via Access; place this after Apply.</span>
            : (<>
                {st.kind === 'terraform'
                  ? <select value={st.target} onChange={(e) => upd(i, { target: e.target.value })} title="Action" style={{ flex: '1 1 auto', minWidth: 0 }}><option>plan</option><option>apply</option><option>destroy</option></select>
                  : <FileTarget value={st.target} onChange={(v) => upd(i, { target: v })} files={filesFor(st.kind)}
                                placeholder={st.kind === 'salt' ? 'state / highstate' : 'playbook.yml'} />}
                {st.kind === 'terraform'
                  ? <select value={st.tool} onChange={(e) => upd(i, { tool: e.target.value })} title="Tool" style={{ width: 150 }}><option value="terraform">Terraform</option><option value="tofu">OpenTofu</option></select>
                  : <select value={st.inventory_id} onFocus={loadInvs} onChange={(e) => invChange(i, e.target.value)} title="Inventory (hosts to target)" style={{ width: 150 }}>
                    {invs.length === 0 && <option value="">(no inventory)</option>}
                    {invs.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
                    <option value="__new">＋ New inventory…</option>
                  </select>}
                <select value={st.credential_id} onChange={(e) => upd(i, { credential_id: e.target.value })} title="SSH / cloud credential" style={{ width: 130 }}>
                  <option value="">(no cred)</option>
                  {creds.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              </>)}
          <div className="row" style={{ gap: 2 }}>
            {!isPseudo(st.kind) && <button className={'ghost sm' + (openSteps.has(i) ? ' active' : '')} title="Variables & options" onClick={() => toggleOpts(i)}>Options</button>}
            <button className="ghost sm" title="Move up" onClick={() => move(i, -1)} disabled={i === 0}>↑</button>
            <button className="ghost sm" title="Move down" onClick={() => move(i, 1)} disabled={i === steps.length - 1}>↓</button>
            <button className="danger ghost sm" title="Remove step" onClick={() => del(i)} disabled={steps.length === 1}>✕</button>
          </div>
        </div>
        {openSteps.has(i) && !isPseudo(st.kind) && (
          <div className="pipe-step-opts">
            <Field label={st.kind === 'terraform' ? 'Variables — KEY=value per line (→ -var)'
              : st.kind === 'salt' ? 'Pillar / kwargs — key=value per line' : 'Extra vars — KEY=value per line (→ -e)'}>
              <textarea rows={2} value={st.vars} onChange={(e) => upd(i, { vars: e.target.value })}
                placeholder={st.kind === 'terraform' ? 'network=homelab' : 'env=staging'}
                style={{ fontFamily: 'ui-monospace,monospace', fontSize: 12, resize: 'vertical' }} />
            </Field>
            {st.kind === 'ansible' && (
              <div className="row" style={{ gap: 8 }}>
                <Field label="Limit (--limit)"><input value={st.limit} onChange={(e) => upd(i, { limit: e.target.value })} placeholder="web-01, db-*" /></Field>
                <Field label="Start at task"><input value={st.startAt} onChange={(e) => upd(i, { startAt: e.target.value })} placeholder="exact task name" /></Field>
                <Field label="Become (sudo) password"><input type="password" value={st.becomePw} autoComplete="off" onChange={(e) => upd(i, { becomePw: e.target.value })} /></Field>
              </div>
            )}
            {st.kind === 'salt' && (
              <label className="row" style={{ gap: 7, fontSize: 13, cursor: 'pointer' }}>
                <input type="checkbox" checked={!!st.saltTest} onChange={(e) => upd(i, { saltTest: e.target.checked })} style={{ width: 'auto' }} />
                Test mode (dry-run — <span className="mono">test=True</span>)
              </label>
            )}
          </div>
        )}
        </React.Fragment>
      ))}
      {newInvStep != null && <NameModal title="New inventory" label="Inventory name" placeholder="prod-web"
        onClose={() => setNewInvStep(null)} onSubmit={createInv} />}
      <div className="row" style={{ margin: '8px 0' }}>
        <button className="ghost sm" onClick={add}>＋ Add step</button>
        <div className="spacer" />
        <label className="row" style={{ gap: 6, fontSize: 13, cursor: 'pointer' }}>
          <input type="checkbox" checked={stopOnFail} onChange={(e) => setStopOnFail(e.target.checked)} style={{ width: 'auto' }} />
          Stop on first failure
        </label>
      </div>
      {onSaved && (
        <Field label="Save this sequence as (optional)">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. web-tier full cadence" />
        </Field>
      )}
      {node}
      <div className="row">
        {onSaved && (
          <button className="ghost" disabled={!name.trim()} onClick={() => wrap(async () => {
            const body = { project_id: project.id, name: name.trim(), steps: payloadOf(), stop_on_failure: stopOnFail }
            const d = saveId
              ? await api(`pipelines/${saveId}`, { method: 'PUT', json: body })
              : await api('pipelines', { method: 'POST', json: body })
            onClose(); onSaved(d.pipeline)
          })}>💾 {saveId ? 'Update' : 'Save'}</button>
        )}
        <div className="spacer" />
        <button className="primary" onClick={() => wrap(async () => {
          const d = await api('pipelines/run', { method: 'POST', json: { project_id: project.id, steps: payloadOf(), stop_on_failure: stopOnFail } })
          onClose(); onLaunched(d.run_ids[0])
        })}>Run {steps.length} step{steps.length > 1 ? 's' : ''} in sequence</button>
      </div>
    </Modal>
  )
}
