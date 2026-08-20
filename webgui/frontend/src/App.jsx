import React, { useState, useEffect } from 'react'
import { api, setToken, getToken } from './api.js'
import { Field, useErr } from './ui.jsx'
import Logo from './Logo.jsx'
import Projects from './views/Projects.jsx'
import Ide from './views/Ide.jsx'
import Inventories from './views/Inventories.jsx'
import Credentials from './views/Credentials.jsx'
import Runs, { RunLog } from './views/Runs.jsx'

export default function App() {
  const [ready, setReady] = useState(false)
  const [needSetup, setNeedSetup] = useState(false)
  const [authed, setAuthed] = useState(false)
  const [view, setView] = useState('projects')
  const [project, setProject] = useState(null)
  const [runId, setRunId] = useState(null)

  useEffect(() => {
    const onLogout = () => setAuthed(false)
    window.addEventListener('slep-logout', onLogout)
    ;(async () => {
      try {
        const h = await fetch('/api/health').then((r) => r.json())
        if (h.admins === 0) { setNeedSetup(true); setReady(true); return }
      } catch { setReady(true); return }
      if (getToken()) { try { await api('me'); setAuthed(true) } catch { /* stale token */ } }
      setReady(true)
    })()
    return () => window.removeEventListener('slep-logout', onLogout)
  }, [])

  if (!ready) return <div className="center muted">Loading…</div>
  if (!authed) return <Auth needSetup={needSetup} onAuthed={() => { setNeedSetup(false); setAuthed(true) }} />

  const go = (v) => { setView(v); setProject(null); setRunId(null) }
  return (
    <>
      <header className="top">
        <div className="brand"><Logo size={24} /> Sysible Linux Engineering Platform</div>
        <div className="spacer" />
        <button className="ghost" onClick={async () => { try { await api('logout', { method: 'POST' }) } catch {} setToken(''); setAuthed(false) }}>Sign out</button>
      </header>
      <div className="layout">
        <nav className="side">
          {[['projects', 'Projects'], ['inventories', 'Inventories'], ['credentials', 'Credentials'], ['runs', 'Runs']].map(([k, l]) => (
            <button key={k} className={view === k && !project && runId == null ? 'active' : ''} onClick={() => go(k)}>{l}</button>
          ))}
        </nav>
        <main className="view">
          {runId != null ? <RunLog runId={runId} onBack={() => setRunId(null)} />
            : project ? <Ide project={project} onBack={() => setProject(null)} onRun={(id) => setRunId(id)} />
              : view === 'projects' ? <Projects onOpen={setProject} />
                : view === 'inventories' ? <Inventories />
                  : view === 'credentials' ? <Credentials />
                    : <Runs onOpen={setRunId} />}
        </main>
      </div>
    </>
  )
}

function Auth({ needSetup, onAuthed }) {
  const [u, setU] = useState('')
  const [p, setP] = useState('')
  const { err, wrap, node } = useErr()
  const submit = () => wrap(async () => {
    if (needSetup && p.length < 10) throw new Error('Password must be at least 10 characters.')
    const path = needSetup ? 'setup' : 'login'
    const d = await api(path, { method: 'POST', json: { username: u, password: p } })
    setToken(d.token); onAuthed()
  })
  return (
    <div className="center">
      <div className="card col" style={{ width: 'min(380px,92vw)' }}>
        <div className="brand" style={{ gap: 12 }}>
          <Logo size={40} />
          <div className="col" style={{ gap: 2 }}>
            <div style={{ fontSize: 17, lineHeight: 1.15 }}>Sysible Linux<br />Engineering Platform</div>
          </div>
        </div>
        <div className="muted">{needSetup ? 'Create the first administrator' : 'Sign in'}</div>
        <Field label="Username"><input value={u} autoComplete="username" onChange={(e) => setU(e.target.value)} /></Field>
        <Field label="Password"><input type="password" value={p} autoComplete={needSetup ? 'new-password' : 'current-password'}
          onChange={(e) => setP(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && submit()} /></Field>
        {node}
        <button className="primary" onClick={submit}>{needSetup ? 'Create admin' : 'Sign in'}</button>
      </div>
    </div>
  )
}
