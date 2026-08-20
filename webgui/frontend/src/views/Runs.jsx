import React, { useEffect, useRef, useState } from 'react'
import { api, tail } from '../api.js'

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
      <div className="log" ref={boxRef}>{text || 'connecting…'}</div>
    </>
  )
}
