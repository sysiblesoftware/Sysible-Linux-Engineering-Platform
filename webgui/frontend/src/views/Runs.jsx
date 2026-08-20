import React, { useEffect, useRef, useState } from 'react'
import { api, tail } from '../api.js'

// Render ANSI-coloured tool output (ansible-playbook et al. emit SGR codes) as
// coloured spans, so the run log reads like a real terminal instead of showing
// raw escape sequences. Text is placed in React spans (auto-escaped); only
// recognised SGR colour/bold codes affect styling.
const ANSI_COLORS = {
  30: '#7d8ca3', 31: '#e5534b', 32: '#63c869', 33: '#e0a83b',
  34: '#5580ee', 35: '#c678dd', 36: '#56b6c2', 37: '#e9f0f7',
  90: '#7d8ca3', 91: '#ff7b72', 92: '#7ee787', 93: '#f2cc60',
  94: '#79c0ff', 95: '#d2a8ff', 96: '#76e3ea', 97: '#ffffff',
}
function ansiToSpans(text) {
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

export function RunLog({ runId, onBack }) {
  const [text, setText] = useState('')
  const [status, setStatus] = useState('pending')
  const boxRef = useRef(null)
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
  return (
    <>
      <div className="row" style={{ marginBottom: 10 }}>
        <button className="ghost sm" onClick={onBack}>← Runs</button>
        <h2 style={{ margin: 0 }}>Run #{runId}</h2>
        <span className={'pill ' + status}>{status}</span>
      </div>
      <div className="log" ref={boxRef}>{text ? ansiToSpans(text) : 'connecting…'}</div>
    </>
  )
}
