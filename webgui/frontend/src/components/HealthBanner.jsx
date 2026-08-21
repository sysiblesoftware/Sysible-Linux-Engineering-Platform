import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

// Surfaces operational warnings (missing engine binary, unwritable data dir).
// Renders nothing when healthy; dismissals are per-session (sessionStorage).
export default function HealthBanner() {
  const [warnings, setWarnings] = useState([])
  const [dismissed, setDismissed] = useState(() => {
    try { return new Set(JSON.parse(sessionStorage.getItem('slep_dismissed_warnings') || '[]')) } catch { return new Set() }
  })

  useEffect(() => {
    const load = () => api('health-warnings').then((d) => setWarnings(d.warnings || [])).catch(() => {})
    load(); const t = setInterval(load, 60000); return () => clearInterval(t)
  }, [])

  const dismiss = (id) => {
    const next = new Set(dismissed); next.add(id); setDismissed(next)
    try { sessionStorage.setItem('slep_dismissed_warnings', JSON.stringify([...next])) } catch { /* private mode */ }
  }

  const shown = warnings.filter((w) => !dismissed.has(w.id))
  if (shown.length === 0) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
      {shown.map((w) => (
        <div key={w.id} className={'health-banner ' + (w.severity === 'critical' ? 'crit' : 'warn')}>
          <div style={{ flex: 1 }}>
            <b>{w.title}</b>
            <div className="faint" style={{ fontSize: 12.5 }}>{w.detail}{w.hint ? ` — ${w.hint}` : ''}</div>
          </div>
          <button className="ghost sm" onClick={() => dismiss(w.id)}>Dismiss</button>
        </div>
      ))}
    </div>
  )
}
