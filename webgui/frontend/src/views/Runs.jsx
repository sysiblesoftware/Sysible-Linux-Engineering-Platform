import React, { useEffect, useRef, useState } from 'react'
import { api, tail, getTheme } from '../api.js'
import { parseRun } from '../runParse.js'
import RunViz from './RunViz.jsx'
import { RunModal } from './Ide.jsx'

// Render ANSI-coloured tool output (ansible-playbook et al. emit SGR codes) as
// coloured spans, so the run log reads like a real terminal instead of showing
// raw escape sequences. Text is placed in React spans (auto-escaped); only
// recognised SGR colour/bold codes affect styling.
const ANSI_DARK = {
  30: '#7d8ca3', 31: '#e5534b', 32: '#63c869', 33: '#e0a83b',
  34: '#5580ee', 35: '#c678dd', 36: '#56b6c2', 37: '#e9f0f7',
  90: '#7d8ca3', 91: '#ff7b72', 92: '#7ee787', 93: '#f2cc60',
  94: '#79c0ff', 95: '#d2a8ff', 96: '#76e3ea', 97: '#ffffff',
}
// Darker, higher-contrast variants for a light log background.
const ANSI_LIGHT = {
  30: '#5b6472', 31: '#c0392b', 32: '#2e7d32', 33: '#a15c00',
  34: '#2f57c4', 35: '#8e44ad', 36: '#0e7490', 37: '#1c2430',
  90: '#7a8496', 91: '#c0392b', 92: '#2e7d32', 93: '#a15c00',
  94: '#2f57c4', 95: '#8e44ad', 96: '#0e7490', 97: '#111827',
}
function ansiToSpans(text) {
  const ANSI_COLORS = getTheme() === 'light' ? ANSI_LIGHT : ANSI_DARK
  const out = []
  let cur = { color: null, bold: false }
  const re = /\u001b\[([0-9;]*)m/g   // ESC [ ... m  (SGR colour sequences)
  let last = 0, m, k = 0
  const emit = (t) => {
    if (!t) return
    out.push(<span key={k++} style={{ color: cur.color || undefined, fontWeight: cur.bold ? 600 : undefined }}>{t}</span>)
  }
  while ((m = re.exec(text)) !== null) {
    emit(text.slice(last, m.index))
    for (const c of (m[1] || '0').split(';').map(Number)) {
      if (c === 0) cur = { color: null, bold: false }
      else if (c === 1) cur.bold = true
      else if (c === 22) cur.bold = false
      else if (c === 39) cur.color = null
      else if (ANSI_COLORS[c] !== undefined) cur.color = ANSI_COLORS[c]
    }
    last = re.lastIndex
  }
  emit(text.slice(last))
  return out
}

export default function Runs({ onOpen }) {
  const [runs, setRuns] = useState([])
  useEffect(() => { api('runs').then((d) => setRuns(d.runs)) }, [])
  return (
    <>
      <h2>Runs</h2>
      {runs.length === 0 ? <div className="muted">No runs yet.</div> : (
        <table>
          <thead><tr><th>#</th><th>Engine</th><th>Target</th><th>Status</th><th>When</th></tr></thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td><a onClick={() => onOpen(r.id)}>#{r.id}</a></td>
                <td>{r.kind}</td><td className="muted">{r.target}</td>
                <td><span className={'pill ' + r.status}>{r.status}</span></td>
                <td className="muted">{r.created ? new Date(r.created * 1000).toLocaleString() : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

export function RunLog({ runId, onBack, onOpenRun }) {
  const [rerun, setRerun] = useState(null)   // {run, failed, tasks} → opens the Run dialog
  const [text, setText] = useState('')
  const [status, setStatus] = useState('pending')
  const [engine, setEngine] = useState('ansible')
  const [seedHosts, setSeedHosts] = useState([])
  const [leftPct, setLeftPct] = useState(50)   // Visualize/Log split, % of width
  const [groupId, setGroupId] = useState('')   // pipeline group this run belongs to
  const [seq, setSeq] = useState([])           // the sibling runs of that pipeline
  const boxRef = useRef(null)
  const splitRef = useRef(null)

  // Drag the divider: translate the pointer x into a left-pane percentage.
  const startDrag = (e) => {
    e.preventDefault()
    const move = (ev) => {
      const el = splitRef.current; if (!el) return
      const r = el.getBoundingClientRect()
      const pct = ((ev.clientX - r.left) / r.width) * 100
      setLeftPct(Math.min(80, Math.max(20, pct)))
    }
    const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); document.body.style.cursor = '' }
    document.body.style.cursor = 'col-resize'
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  // Run metadata drives the viz: which engine, and (for Ansible) the inventory's
  // hosts so the "reaching hosts" grid shows every target before it reports in.
  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const r = await api(`runs/${runId}`)
        if (!alive) return
        setEngine(r.kind || 'ansible')
        setGroupId(r.group_id || '')
        if (r.inventory_id) {
          try {
            const h = await api(`inventories/${r.inventory_id}/hosts`)
            if (alive) setSeedHosts((h.hosts || []).map((x) => x.name))
          } catch { /* inventory may be gone */ }
        }
      } catch { /* run detail optional */ }
    })()
    return () => { alive = false }
  }, [runId])

  // Part of a launched sequence? Poll the group's runs so the step strip shows the
  // whole pipeline advancing (queued → running → success/failed/canceled).
  useEffect(() => {
    if (!groupId) { setSeq([]); return }
    let alive = true
    const tick = async () => {
      try { const d = await api(`pipelines/runs/${groupId}`); if (alive) setSeq(d.runs || []) } catch { /* ignore */ }
    }
    tick()
    const iv = setInterval(() => {
      const done = seq.length && seq.every((s) => ['success', 'failed', 'canceled'].includes(s.status))
      if (done) { clearInterval(iv); return }
      tick()
    }, 1500)
    return () => { alive = false; clearInterval(iv) }
  }, [groupId])

  // Auto-advance a sequence: when this step finishes successfully, follow the
  // pipeline to the next step's run (e.g. Terraform apply → the Ansible configure
  // screen) so the whole cadence plays out without clicking each pill. Reset the
  // one-shot guard whenever we land on a new run.
  const advancedRef = useRef(false)
  useEffect(() => { advancedRef.current = false }, [runId])
  useEffect(() => {
    if (advancedRef.current || status !== 'success' || !groupId || seq.length < 2 || !onOpenRun) return
    const idx = seq.findIndex((s) => s.id === runId)
    const next = idx >= 0 ? seq[idx + 1] : null
    if (next && ['running', 'queued'].includes(next.status)) {
      advancedRef.current = true
      onOpenRun(next.id)
    }
  }, [status, seq, runId, groupId, onOpenRun])

  useEffect(() => {
    let alive = true, offset = 0, acc = ''
    ;(async () => {
      while (alive) {
        const r = await tail(`runs/${runId}/log?offset=${offset}`)
        if (!alive) break
        if (r.text) { acc += r.text; setText(acc); const b = boxRef.current; if (b) b.scrollTop = b.scrollHeight }
        offset = r.next; setStatus(r.status)
        if (['success', 'failed', 'canceled'].includes(r.status)) break
        await new Promise((res) => setTimeout(res, 1000))
      }
    })()
    return () => { alive = false }
  }, [runId])

  const model = parseRun(engine, text)
  // Hosts that failed/were unreachable, and the earliest task that broke — for a
  // targeted "re-run failed hosts" (--limit + --start-at-task).
  const failedHosts = engine === 'ansible'
    ? Object.entries(model.hosts || {}).filter(([, h]) => (h.failed || 0) > 0 || (h.unreachable || 0) > 0 || h.last === 'failed' || h.last === 'unreachable').map(([n]) => n)
    : []
  let firstFail = Infinity
  for (const h of failedHosts) {
    const i = ((model.hostTasks || {})[h] || []).findIndex((s) => s === 'failed' || s === 'unreachable')
    if (i >= 0) firstFail = Math.min(firstFail, i)
  }
  const startAt = firstFail < Infinity ? (model.taskList?.[firstFail]?.name || '') : ''
  const done = ['success', 'failed', 'canceled'].includes(status)

  const openRerun = async () => {
    const run = await api(`runs/${runId}`)
    setRerun({ run, limit: failedHosts.join(','), startAt, tasks: (model.taskList || []).map((t) => t.name) })
  }

  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <button className="ghost sm" onClick={onBack}>← Runs</button>
        <h2 style={{ margin: 0 }}>Run #{runId}</h2>
        <span className="muted">{engine}</span>
        <span className={'pill ' + status}>{status}</span>
        <div className="spacer" />
        {done && failedHosts.length > 0 && (
          <button className="ghost sm" title={`Re-run the playbook against just: ${failedHosts.join(', ')}`}
            onClick={openRerun}>↻ Re-run failed ({failedHosts.length})</button>
        )}
      </div>
      {seq.length > 1 && (
        <div className="seq-strip">
          {seq.map((s, i) => (
            <React.Fragment key={s.id}>
              <button className={'seq-step pill ' + s.status + (s.id === runId ? ' current' : '')}
                title={`${s.kind} · ${s.target} · ${s.status}`}
                onClick={() => s.id !== runId && onOpenRun && onOpenRun(s.id)}>
                <span className="seq-n">{i + 1}</span>{s.kind}<span className="muted"> · {s.target}</span>
              </button>
              {i < seq.length - 1 && <span className="seq-arrow" aria-hidden="true">→</span>}
            </React.Fragment>
          ))}
        </div>
      )}
      {/* Visualize and Log together, with a draggable divider to size them (they
          stack on narrow screens). Each pane scrolls on its own. */}
      <div className="run-split" ref={splitRef} style={{ gridTemplateColumns: `minmax(0,${leftPct}fr) 8px minmax(0,${100 - leftPct}fr)` }}>
        <div className="run-pane">
          <div className="pane-title">Visualize</div>
          <div className="run-scroll">
            {text ? <RunViz engine={engine} model={model} seedHosts={seedHosts} />
              : <div className="muted" style={{ padding: 8 }}>connecting…</div>}
          </div>
        </div>
        <div className="run-gutter" onMouseDown={startDrag} title="Drag to resize" />
        <div className="run-pane">
          <div className="pane-title">Log</div>
          <div className="log run-scroll" ref={boxRef}>{text ? ansiToSpans(text) : 'connecting…'}</div>
        </div>
      </div>
      {rerun && (
        <RunModal project={{ id: rerun.run.project_id }} onClose={() => setRerun(null)}
          onLaunched={(id) => { setRerun(null); onOpenRun ? onOpenRun(id) : onBack() }}
          initial={{ engine: rerun.run.kind, target: rerun.run.target, inventory_id: rerun.run.inventory_id,
            credential_id: rerun.run.credential_id, limit: rerun.limit, start_at_task: rerun.startAt, tasks: rerun.tasks }} />
      )}
    </>
  )
}
