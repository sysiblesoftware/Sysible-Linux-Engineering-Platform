import React, { useEffect, useRef, useState } from 'react'
import { api, getToken } from '../api.js'
import { Modal } from '../ui.jsx'

// One-click engine install: POST the install, then tail its streaming log until
// the backend reports installed/failed. On success the caller re-checks health.
export default function EngineInstall({ engine, label, onClose, onDone }) {
  const [text, setText] = useState('')
  const [status, setStatus] = useState('starting')   // starting|running|installed|failed
  const boxRef = useRef(null)

  useEffect(() => {
    let alive = true, offset = 0, acc = ''
    ;(async () => {
      try { await api(`engines/${engine}/install`, { method: 'POST' }) }
      catch (e) { if (alive) { setText('Could not start install: ' + e.message); setStatus('failed') } return }
      setStatus('running')
      while (alive) {
        const r = await fetch(`/api/engines/${engine}/install-log?offset=${offset}`,
          { headers: getToken() ? { Authorization: 'Bearer ' + getToken() } : {} })
        const chunk = await r.text()
        if (chunk) { acc += chunk; setText(acc); const b = boxRef.current; if (b) b.scrollTop = b.scrollHeight }
        offset = Number(r.headers.get('X-Log-Next') || offset)
        const st = r.headers.get('X-Install-Status') || ''
        if (st === 'installed') { setStatus('installed'); break }
        if (st === 'failed') { setStatus('failed'); break }
        await new Promise((res) => setTimeout(res, 1000))
      }
    })()
    return () => { alive = false }
  }, [engine])

  return (
    <Modal title={`Install ${label}`} onClose={onClose} wide>
      <div className="row" style={{ marginBottom: 6 }}>
        {status === 'installed' ? <span className="pill ok">installed</span>
          : status === 'failed' ? <span className="pill failed">failed</span>
            : <span className="pill running">installing…</span>}
        <span className="muted">Installs into SLEP’s data dir — no root, no system changes.</span>
      </div>
      <div className="log" ref={boxRef} style={{ maxHeight: '48vh' }}>{text || 'starting…'}</div>
      <div className="row" style={{ marginTop: 8 }}>
        <div className="spacer" />
        {status === 'installed'
          ? <button className="primary" onClick={onDone}>Done</button>
          : <button className="ghost" onClick={onClose}>{status === 'failed' ? 'Close' : 'Run in background'}</button>}
      </div>
    </Modal>
  )
}
