import React, { useEffect, useRef, useState } from 'react'
import { api, getToken, apiUrl } from '../api.js'
import { Modal } from '../ui.jsx'

// Install the Ansible Galaxy collections the task snippets reach for
// (community.general, ansible.posix, …) with one click, via ansible-galaxy. Shows
// what's already installed, lets you install the common set (or a custom list),
// and tails the streaming install log.
export default function CollectionsInstall({ onClose }) {
  const [st, setSt] = useState(null)          // status payload
  const [text, setText] = useState('')
  const [phase, setPhase] = useState('idle')  // idle|running|done|failed
  const [custom, setCustom] = useState('')
  const boxRef = useRef(null)

  const load = () => api('engines/collections').then(setSt).catch(() => setSt({ ansible_installed: false, installed: [], common: [], missing_common: [] }))
  useEffect(() => { load() }, [])

  const install = async (collections) => {
    setPhase('running'); setText('')
    try { await api('engines/collections/install', { method: 'POST', json: collections ? { collections } : {} }) }
    catch (e) { setText('Could not start: ' + e.message); setPhase('failed'); return }
    let offset = 0, acc = '', alive = true
    while (alive) {
      const r = await fetch(apiUrl(`/api/engines/collections/install-log?offset=${offset}`),
        { headers: getToken() ? { Authorization: 'Bearer ' + getToken() } : {} })
      const chunk = await r.text()
      if (chunk) { acc += chunk; setText(acc); const b = boxRef.current; if (b) b.scrollTop = b.scrollHeight }
      offset = Number(r.headers.get('X-Log-Next') || offset)
      const s = r.headers.get('X-Install-Status') || ''
      if (s === 'done') { setPhase('done'); alive = false; load() }
      else if (s === 'failed') { setPhase('failed'); alive = false }
      else await new Promise((res) => setTimeout(res, 1000))
    }
  }

  const customList = custom.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean)

  return (
    <Modal title="Ansible collections" onClose={onClose} wide>
      {!st ? <div className="muted">Loading…</div>
        : !st.ansible_installed ? (
          <div className="err">Ansible isn’t installed yet — install the Ansible engine first, then come back to add collections.</div>
        ) : (
          <>
            <div className="muted">Modules like <span className="mono">community.general.*</span> and <span className="mono">ansible.posix.*</span> live in Galaxy collections. Install the common set the snippets use, or name your own.</div>
            <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
              {st.common.map((c) => {
                const have = !st.missing_common.includes(c)
                return <span key={c} className={'pill' + (have ? ' ok' : '')} title={have ? 'installed' : 'not installed'}>{have ? '✓ ' : ''}{c}</span>
              })}
            </div>
            <div className="row" style={{ gap: 8, marginTop: 10 }}>
              <button className="primary" disabled={phase === 'running' || st.missing_common.length === 0}
                onClick={() => install(null)}>
                {st.missing_common.length === 0 ? 'Common set installed' : `Install ${st.missing_common.length} missing`}
              </button>
              <button className="ghost" disabled={phase === 'running'} onClick={() => install(st.common)}>Reinstall / upgrade all common</button>
            </div>
            <div className="row" style={{ gap: 8, marginTop: 10, alignItems: 'flex-end' }}>
              <label className="fld" style={{ flex: 1 }}>Custom collections (namespace.name, space/comma-separated)
                <input value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="community.mysql community.postgresql" />
              </label>
              <button className="ghost" disabled={phase === 'running' || customList.length === 0} onClick={() => install(customList)}>Install</button>
            </div>
            {(text || phase !== 'idle') && (
              <>
                <div className="row" style={{ marginTop: 10 }}>
                  {phase === 'done' ? <span className="pill ok">done</span>
                    : phase === 'failed' ? <span className="pill failed">failed</span>
                      : <span className="pill running">installing…</span>}
                </div>
                <div className="log" ref={boxRef} style={{ maxHeight: '38vh', marginTop: 6 }}>{text || 'starting…'}</div>
              </>
            )}
          </>
        )}
      <div className="row" style={{ marginTop: 10 }}><div className="spacer" /><button className="ghost" onClick={onClose}>Close</button></div>
    </Modal>
  )
}
