import React, { useState, useEffect } from 'react'
import { api, setToken, getToken, setRole, isSuperuser, getTheme, applyTheme } from './api.js'
import { Field, useErr } from './ui.jsx'
import Logo from './Logo.jsx'
import Projects from './views/Projects.jsx'
import Ide from './views/Ide.jsx'
import Inventories from './views/Inventories.jsx'
import Controllers from './views/Controllers.jsx'
import Credentials from './views/Credentials.jsx'
import Vault from './views/Vault.jsx'
import Users from './views/Users.jsx'
import Infrastructure from './views/Infrastructure.jsx'
import Schedules from './views/Schedules.jsx'
import Activity from './views/Activity.jsx'
import HealthBanner from './components/HealthBanner.jsx'
import Runs, { RunLog } from './views/Runs.jsx'

// Left-rail navigation — same shape as the Sysible Controller console.
// `su: true` items are shown only to superusers.
const NAV = [
  { key: 'projects', label: 'Projects', icon: 'folder' },
  { key: 'inventories', label: 'Inventories', icon: 'server' },
  { key: 'infra', label: 'Infrastructure', icon: 'cloud' },
  { key: 'controllers', label: 'Controllers', icon: 'link' },
  { key: 'credentials', label: 'Credentials', icon: 'key' },
  { key: 'vault', label: 'Vault', icon: 'lock' },
  { key: 'runs', label: 'Runs', icon: 'play' },
  { key: 'schedules', label: 'Schedules', icon: 'clock' },
  { key: 'activity', label: 'Activity', icon: 'activity', su: true },
  { key: 'users', label: 'Users', icon: 'users', su: true },
]

// Feather-style line icons (inline SVG paths), matching the Controller's set.
const ICONS = {
  folder: <path d="M3 7a2 2 0 0 1 2-2h3.5l2 2H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />,
  server: <><rect x="3" y="4" width="18" height="6" rx="1.5" /><rect x="3" y="14" width="18" height="6" rx="1.5" /><line x1="7" y1="7" x2="7.01" y2="7" /><line x1="7" y1="17" x2="7.01" y2="17" /></>,
  link: <><path d="M9 15l6-6" /><path d="M11.5 6.5l1-1a4 4 0 0 1 6 6l-1 1" /><path d="M12.5 17.5l-1 1a4 4 0 0 1-6-6l1-1" /></>,
  key: <><circle cx="8" cy="15" r="4" /><path d="M10.8 12.2 20 3M17 6l2 2M14 9l2 2" /></>,
  lock: <><rect x="4" y="11" width="16" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></>,
  play: <path d="M7 5l12 7-12 7z" />,
  users: <><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  activity: <path d="M3 12h4l3 8 4-16 3 8h4" />,
  cloud: <path d="M6.5 19a4.5 4.5 0 0 1-.5-8.97 6 6 0 0 1 11.64-1.36A4 4 0 0 1 17.5 19z" />,
}

function NavIcon({ name }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{ICONS[name]}</svg>
  )
}

export default function App() {
  const [ready, setReady] = useState(false)
  const [needSetup, setNeedSetup] = useState(false)
  const [authed, setAuthed] = useState(false)
  const [username, setUsername] = useState('')
  const [roleName, setRoleName] = useState('')
  const [view, setView] = useState('projects')
  const [project, setProject] = useState(null)
  const [runId, setRunId] = useState(null)
  const [theme, setTheme] = useState(getTheme())
  useEffect(() => { applyTheme(theme) }, [theme])

  useEffect(() => {
    const onLogout = () => setAuthed(false)
    window.addEventListener('slep-logout', onLogout)
    ;(async () => {
      try {
        const h = await fetch('/api/health').then((r) => r.json())
        if (h.admins === 0) { setNeedSetup(true); setReady(true); return }
      } catch { setReady(true); return }
      if (getToken()) { try { const me = await api('me'); setUsername(me.username); setRole(me.role); setRoleName(me.role); setAuthed(true) } catch { /* stale token */ } }
      setReady(true)
    })()
    return () => window.removeEventListener('slep-logout', onLogout)
  }, [])

  if (!ready) return <div className="center muted">Loading…</div>
  if (!authed) return <Auth needSetup={needSetup} onAuthed={(u, r) => { setNeedSetup(false); setUsername(u); setRoleName(r); setAuthed(true) }} />

  const go = (v) => { setView(v); setProject(null); setRunId(null) }
  const atNav = (k) => view === k && !project && runId == null
  const initials = (username || 'AD').slice(0, 2).toUpperCase()

  return (
    <div className="shell">
      <aside className="rail">
        <div className="rail-brand">
          <Logo size={34} />
          <div className="name">Sysible Linux<br />Engineering Platform</div>
        </div>
        <nav className="rail-nav">
          {NAV.filter((n) => !n.su || roleName === 'superuser').map((n) => (
            <button key={n.key} className={'rail-item' + (atNav(n.key) ? ' active' : '')} onClick={() => go(n.key)}>
              <NavIcon name={n.icon} /> {n.label}
            </button>
          ))}
        </nav>
        <div className="rail-foot">
          <div className="ce-badge">Community Edition</div>
          <div className="account">
            <span className="avatar">{initials}</span>
            <div style={{ lineHeight: 1.2 }}>{username || 'admin'}<br /><span className="faint" style={{ fontSize: 12 }}>{roleName || 'operator'}</span></div>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <button className="ghost sm" style={{ flex: 1 }} title="Toggle light / dark"
              onClick={() => setTheme((t) => (t === 'light' ? 'dark' : 'light'))}>{theme === 'light' ? '☾ Dark' : '☀ Light'}</button>
            <button className="ghost sm" style={{ flex: 1 }} onClick={async () => { try { await api('logout', { method: 'POST' }) } catch {} setToken(''); setRole(''); setAuthed(false) }}>Sign out</button>
          </div>
        </div>
      </aside>
      <main className="view">
        {runId == null && !project && <HealthBanner />}
        {runId != null ? <RunLog runId={runId} onBack={() => setRunId(null)} onOpenRun={setRunId} />
          : project ? <Ide project={project} onBack={() => setProject(null)} onRun={(id) => setRunId(id)} />
            : view === 'projects' ? <Projects onOpen={setProject} />
              : view === 'inventories' ? <Inventories />
                : view === 'infra' ? <Infrastructure onOpenProject={setProject} onOpenRun={setRunId} />
                : view === 'controllers' ? <Controllers />
                  : view === 'credentials' ? <Credentials />
                    : view === 'vault' ? <Vault />
                      : view === 'schedules' ? <Schedules />
                        : view === 'activity' ? <Activity />
                          : view === 'users' ? <Users />
                            : <Runs onOpen={setRunId} />}
      </main>
    </div>
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
    setToken(d.token); setRole(d.role); onAuthed(d.username || u, d.role)
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
