// Roles, collections and molecule scenarios for one project.
//
// The APIs for all of this already existed and were tested — GET /projects/:id/content,
// POST /projects/:id/content/sync, POST /projects/:id/content/galaxy and
// GET /projects/:id/molecule — with nothing on screen calling them. A project whose
// playbook depends on a role had no way to install it except a shell on the host,
// which is the gap this closes.
//
// Both halves answer the same question — "will this project actually run?" — so they
// are two tabs of one dialog rather than two places to look.
import { useEffect, useState } from 'react'
import { api, canWrite } from '../api.js'
import { Modal, Field, useErr } from '../ui.jsx'

export default function ProjectContent({ project, tab: initialTab = 'content', onClose, onOpenRun }) {
  const [tab, setTab] = useState(initialTab)
  return (
    <Modal title={`${project.name} — content & tests`} onClose={onClose} wide>
      <div className="row" style={{ gap: 6, marginBottom: 10 }}>
        <button className={'ghost sm' + (tab === 'content' ? ' active' : '')}
          onClick={() => setTab('content')}>Roles &amp; collections</button>
        <button className={'ghost sm' + (tab === 'molecule' ? ' active' : '')}
          onClick={() => setTab('molecule')}>Molecule</button>
      </div>
      {tab === 'content'
        ? <ContentTab project={project} onOpenRun={onOpenRun} onClose={onClose} />
        : <MoleculeTab project={project} onOpenRun={onOpenRun} onClose={onClose} />}
    </Modal>
  )
}

const declared = (req) => (req && req.entries) || []
const undeclared = (req, installed) =>
  (installed || []).filter((n) => !declared(req).some((e) => (e.name || e.src) === n))

// --------------------------------------------------------------- roles + collections
function ContentTab({ project, onOpenRun, onClose }) {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState('')
  const [servers, setServers] = useState((project.galaxy_servers || '').replace(/,/g, '\n'))
  const [token, setToken] = useState('')
  const err = useErr()
  const writable = canWrite()

  useEffect(() => {
    api(`projects/${project.id}/content`).then(setData).catch((e) => err.setErr(e.message))
  }, [project.id])

  const sync = (force) => err.wrap(async () => {
    setBusy(force ? 'force' : 'sync')
    try {
      const r = await api(`projects/${project.id}/content/sync`, { method: 'POST', json: { force } })
      onClose(); onOpenRun(r.run_id)      // it is a normal run — follow it in the log
    } finally { setBusy('') }
  })

  const saveGalaxy = () => err.wrap(async () => {
    setBusy('galaxy')
    try {
      // An untouched token field must not clear the stored one, so it is only sent
      // when something was typed — same rule as the notification secrets.
      await api(`projects/${project.id}/content/galaxy`, {
        method: 'POST', json: { servers, ...(token ? { token } : {}) },
      })
      setToken('')
    } finally { setBusy('') }
  })

  if (!data) return <>{err.node}<div className="muted">Loading…</div></>

  const req = data.requirements || {}
  return (
    <>
      {err.node}
      {(req.errors || []).map((m, i) => <div key={i} className="err">{m}</div>)}

      <ReqList title="Roles" req={req.roles} installed={data.installed && data.installed.roles}
        empty="No roles/requirements.yml in this project." />
      <ReqList title="Collections" req={req.collections}
        installed={data.installed && data.installed.collections}
        empty="No collections/requirements.yml in this project." />

      {writable && (
        <div className="row" style={{ gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
          <button className="primary" disabled={!!busy} onClick={() => sync(false)}>
            {busy === 'sync' ? 'Starting…' : 'Install what is declared'}
          </button>
          <button className="ghost" disabled={!!busy} onClick={() => sync(true)}
            title="Re-download everything, even what is already present">
            {busy === 'force' ? 'Starting…' : 'Force re-install'}
          </button>
          <span className="muted" style={{ fontSize: 12 }}>
            Runs as a normal job, so it is logged and audited like everything else.
          </span>
        </div>
      )}

      {writable && (
        <>
          <h3 style={{ margin: '18px 0 2px' }}>Private Galaxy / Automation Hub</h3>
          <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
            Leave empty to use the public galaxy.ansible.com. The token is encrypted at
            rest and never shown again — blank keeps the stored one.
          </div>
          <Field label="Servers (one URL per line, in order)">
            <textarea rows={3} className="mono" value={servers}
              onChange={(e) => setServers(e.target.value)}
              placeholder="https://hub.example.com/api/galaxy/" />
          </Field>
          <Field label="Token">
            <input type="password" value={token} autoComplete="new-password"
              onChange={(e) => setToken(e.target.value)} />
          </Field>
          <div className="row" style={{ marginTop: 8 }}>
            <button className="ghost" disabled={!!busy} onClick={saveGalaxy}>
              {busy === 'galaxy' ? 'Saving…' : 'Save Galaxy settings'}
            </button>
          </div>
        </>
      )}
    </>
  )
}

function ReqList({ title, req, installed, empty }) {
  const have = new Set(installed || [])
  const extra = undeclared(req, installed)
  return (
    <>
      <h3 style={{ margin: '14px 0 2px' }}>{title}</h3>
      {!req
        ? <div className="muted" style={{ fontSize: 13 }}>{empty}</div>
        : declared(req).length === 0
          ? <div className="muted" style={{ fontSize: 13 }}>
              <span className="mono">{req.path}</span> declares nothing.
            </div>
          : (
            <>
              <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
                declared in <span className="mono">{req.path}</span>
              </div>
              <table>
                <tbody>
                  {declared(req).map((e, i) => {
                    const name = e.name || e.src || '(unnamed)'
                    // A declared entry that is not on disk is exactly why a run fails
                    // with "the role was not found", so say which is which.
                    const present = have.has(name)
                    return (
                      <tr key={i}>
                        <td className="mono">{name}</td>
                        <td className="muted">{e.version || ''}</td>
                        <td className="muted">{e.src && e.src !== name ? e.src : (e.source || '')}</td>
                        <td style={{ textAlign: 'right' }}>
                          <span className={'pill ' + (present ? 'ok' : 'failed')}>
                            {present ? 'installed' : 'missing'}
                          </span>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </>
          )}
      {extra.length > 0 && (
        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          also present, not declared: <span className="mono">{extra.join(', ')}</span>
        </div>
      )}
    </>
  )
}

// ------------------------------------------------------------------------- molecule
function MoleculeTab({ project, onOpenRun, onClose }) {
  const [scenarios, setScenarios] = useState(null)
  const [busy, setBusy] = useState('')
  const err = useErr()
  const writable = canWrite()

  useEffect(() => {
    api(`projects/${project.id}/molecule`)
      .then((d) => setScenarios(d.scenarios)).catch((e) => err.setErr(e.message))
  }, [project.id])

  const run = (name) => err.wrap(async () => {
    setBusy(name)
    try {
      const r = await api('runs', {
        method: 'POST', json: { project_id: project.id, kind: 'molecule', target: name },
      })
      onClose(); onOpenRun(r.run_id)
    } finally { setBusy('') }
  })

  if (!scenarios) return <>{err.node}<div className="muted">Loading…</div></>
  if (scenarios.length === 0) {
    return (
      <>
        {err.node}
        <div className="muted">
          This project has no <span className="mono">molecule/</span> scenarios. Add one with
          {' '}<span className="mono">molecule init scenario</span> to test a role before it
          touches real hosts.
        </div>
      </>
    )
  }

  return (
    <>
      {err.node}
      <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
        A scenario is told here whether SLEP can run it, rather than failing several
        minutes into a run.
      </div>
      <table>
        <thead><tr><th>Scenario</th><th>Driver</th><th>Status</th><th /></tr></thead>
        <tbody>
          {scenarios.map((s) => (
            <tr key={s.name}>
              <td className="mono">{s.name}</td>
              <td className="muted mono">{s.driver}</td>
              <td>
                {s.runnable
                  ? <span className="pill ok">runnable</span>
                  : <>
                      <span className="pill failed">cannot run</span>
                      <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>{s.reason}</div>
                    </>}
              </td>
              <td style={{ textAlign: 'right' }}>
                {writable && s.runnable && (
                  <button className="primary sm" disabled={!!busy} onClick={() => run(s.name)}>
                    {busy === s.name ? 'Starting…' : 'Test'}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
