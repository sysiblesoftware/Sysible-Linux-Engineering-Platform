import React, { useEffect, useState } from 'react'
import { api, isSuperuser } from '../api.js'
import EngineInstall from './EngineInstall.jsx'

// Surfaces operational warnings (missing engine binary, unwritable data dir) and
// offers a one-click install for a missing engine. Renders nothing when healthy;
// dismissals are per-session (sessionStorage).
export default function HealthBanner() {
  const [warnings, setWarnings] = useState([])
  const [install, setInstall] = useState(null)   // {engine,label} being installed
  const [dismissed, setDismissed] = useState(() => {
    try { return new Set(JSON.parse(sessionStorage.getItem('slep_dismissed_warnings') || '[]')) } catch { return new Set() }
  })

  const load = () => api('health-warnings').then((d) => setWarnings(d.warnings || [])).catch(() => {})
  useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t) }, [])

  const dismiss = (id) => {
    const next = new Set(dismissed); next.add(id); setDismissed(next)
    try { sessionStorage.setItem('slep_dismissed_warnings', JSON.stringify([...next])) } catch { /* private mode */ }
  }

  // A warning id like "engine-terraform" → an installable engine.
  const engineOf = (w) => w.id.startsWith('engine-') ? w.id.slice('engine-'.length) : null

  const shown = warnings.filter((w) => !dismissed.has(w.id))
  if (shown.length === 0) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
      {shown.map((w) => {
        const engine = engineOf(w)
        return (
          <div key={w.id} className={'health-banner ' + (w.severity === 'critical' ? 'crit' : 'warn')}>
            <div style={{ flex: 1 }}>
              <b>{w.title}</b>
              <div className="faint" style={{ fontSize: 12.5 }}>{w.detail}{w.hint ? ` — ${w.hint}` : ''}</div>
            </div>
            {engine && isSuperuser() && (
              <button className="primary sm" onClick={() => setInstall({ engine, label: w.title.replace(' is not installed', '') })}>
                Install {w.title.replace(' is not installed', '')}
              </button>
            )}
            <button className="ghost sm" onClick={() => dismiss(w.id)}>Dismiss</button>
          </div>
        )
      })}
      {install && <EngineInstall engine={install.engine} label={install.label}
        onClose={() => setInstall(null)} onDone={() => { setInstall(null); load() }} />}
    </div>
  )
}
