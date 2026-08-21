import React from 'react'

// AAP-style run visualizations. Each takes the parsed model from runParse.js and
// renders a live, at-a-glance picture of the run: which hosts the playbook is
// reaching and how they're faring (Ansible), what Terraform is about to (or did)
// change, and how each Salt minion came out. Purely presentational — the parser
// owns all the log-reading.

const STATUS_COLOR = {
  ok: 'var(--ok, #63c869)', changed: 'var(--warn, #e0a83b)', failed: 'var(--err, #e5534b)',
  unreachable: '#c678dd', skipped: '#7d8ca3', pending: '#7d8ca3',
}

export default function RunViz({ engine, model, seedHosts }) {
  if (engine === 'terraform') return <TerraformViz model={model} />
  if (engine === 'salt') return <SaltViz model={model} />
  return <AnsibleViz model={model} seedHosts={seedHosts} />
}

// -------------------------------------------------------------- Ansible
function AnsibleViz({ model, seedHosts }) {
  // Merge live results over any seeded (inventory) hosts so the grid shows every
  // target from the start, filling in as the play reaches them.
  const names = Array.from(new Set([...(seedHosts || []), ...Object.keys(model.hosts)])).sort()
  const hosts = names.map((n) => ({ name: n, ...(model.hosts[n] || { ok: 0, changed: 0, failed: 0, skipped: 0, unreachable: 0, last: 'pending' }) }))
  const recap = model.recap

  return (
    <div className="viz">
      {/* The flow "panel": a header with the task-progress dots + play/task
          heading, over the server-reaches-hosts diagram. */}
      <div className="viz-flow-box">
        <div className="flow-head">
          <div className="flow-title">
            <span className="faint">{model.currentPlay ? `PLAY · ${model.currentPlay}` : 'Ansible'}</span>
            <b>{model.currentTask ? model.currentTask : recap ? 'Play recap' : 'Starting…'}</b>
          </div>
          <div className="spacer" />
          <span className="faint" style={{ fontSize: 12 }}>{hosts.length} host(s) · {model.tasks} task(s)</span>
        </div>
        <HostReachFlow hosts={hosts} tasks={model.tasks} hostTasks={model.hostTasks || {}} taskList={model.taskList || []} />
      </div>

      <div className="viz-grid">
        {hosts.map((h) => (
          <div key={h.name} className="viz-host" style={{ borderLeft: `3px solid ${STATUS_COLOR[h.last] || STATUS_COLOR.pending}` }}>
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <b className="mono" style={{ fontSize: 13 }}>{h.name}</b>
              <span className="dot" style={{ background: STATUS_COLOR[h.last] || STATUS_COLOR.pending }} />
            </div>
            <div className="faint" style={{ fontSize: 11.5, marginTop: 4 }}>
              ok {h.ok} · <span style={{ color: STATUS_COLOR.changed }}>changed {h.changed}</span>
              {h.failed ? <> · <span style={{ color: STATUS_COLOR.failed }}>failed {h.failed}</span></> : null}
              {h.unreachable ? <> · <span style={{ color: STATUS_COLOR.unreachable }}>unreachable</span></> : null}
              {h.skipped ? ` · skipped ${h.skipped}` : ''}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// The SLEP server reaching each host, and — per host — a row of task dots (one
// per task in the play) so you can see which task failed on which host, then the
// full hostname. Green ok · amber changed · red failed · purple unreachable ·
// dim pending. Hover a dot for "task: status".
function HostReachFlow({ hosts, tasks, hostTasks, taskList }) {
  const n = hosts.length
  const nTasks = Math.max(1, tasks || 0)
  const rowH = 26
  const H = Math.max(96, n * rowH + 26)
  const cx = 46, cy = H / 2, ex = 190          // ex = where the fan lines end
  const dotR = 4, dotGap = 13
  const dotsX = ex + 16
  const nameX = dotsX + nTasks * dotGap + 6
  const W = nameX + 150
  const step = n > 1 ? (H - 30) / (n - 1) : 0
  return (
    <div style={{ overflowX: 'auto' }}>
      <svg className="viz-flow" width={W} height={H} viewBox={`0 0 ${W} ${H}`}>
        {hosts.map((h, i) => {
          const y = n > 1 ? 15 + i * step : cy
          const col = STATUS_COLOR[h.last] || STATUS_COLOR.pending
          const reached = h.last !== 'pending'
          const statuses = hostTasks[h.name] || []
          return (
            <g key={h.name}>
              <path d={`M ${cx + 15} ${cy} C ${(cx + ex) / 2} ${cy}, ${(cx + ex) / 2} ${y}, ${ex - 6} ${y}`}
                fill="none" stroke={col} strokeWidth={reached ? 1.8 : 1} strokeOpacity={reached ? 0.8 : 0.22} />
              <circle cx={ex} cy={y} r={4} fill={col} fillOpacity={reached ? 1 : 0.3} />
              {Array.from({ length: nTasks }).map((_, ti) => {
                const st = statuses[ti]
                const c = STATUS_COLOR[st] || STATUS_COLOR.pending
                return (
                  <circle key={ti} cx={dotsX + ti * dotGap} cy={y} r={dotR} fill={c} fillOpacity={st ? 1 : 0.28}>
                    <title>{`${taskList[ti]?.name || 'task ' + (ti + 1)}: ${st || 'pending'}`}</title>
                  </circle>
                )
              })}
              <text x={nameX} y={y + 3.5} fontSize="12" fill="var(--text)" className="mono">{h.name}</text>
            </g>
          )
        })}
        <ServerIcon cx={cx} cy={cy} />
      </svg>
    </div>
  )
}

// The source node: a SLEP server (a small rack with unit slots + status LEDs)
// instead of a generic play triangle — this is the SLEP box reaching the fleet.
function ServerIcon({ cx, cy }) {
  const w = 26, h = 30, x = cx - w / 2, y = cy - h / 2
  const green = 'var(--green-bright, #63c869)'
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={4}
        fill="var(--panel2, #1b2230)" stroke="var(--accent)" strokeWidth="1.6" />
      {[0, 1, 2].map((i) => {
        const uy = y + 5 + i * 7.5
        return (
          <g key={i}>
            <rect x={x + 3} y={uy} width={w - 6} height={5} rx={1.4} fill="none" stroke="var(--accent)" strokeOpacity="0.5" strokeWidth="1" />
            <circle cx={x + 6} cy={uy + 2.5} r={1.3} fill={green} />
            <line x1={x + 10} y1={uy + 2.5} x2={x + w - 5} y2={uy + 2.5} stroke="var(--accent)" strokeOpacity="0.35" strokeWidth="1" />
          </g>
        )
      })}
    </g>
  )
}

// ------------------------------------------------------------ Terraform
function TerraformViz({ model }) {
  const sum = model.applied || model.plan
  const label = model.applied ? 'Applied' : model.plan ? 'Planned' : 'Working…'
  const list = Object.entries(model.resources).map(([addr, r]) => ({ addr, ...r }))
  const ACT = { create: ['+', 'var(--ok,#63c869)'], update: ['~', 'var(--warn,#e0a83b)'], destroy: ['-', 'var(--err,#e5534b)'], replace: ['±', '#c678dd'], noop: ['·', '#7d8ca3'] }

  return (
    <div className="viz">
      <div className="viz-head">
        <div style={{ fontWeight: 600 }}>{label}{model.errored ? ' · error' : ''}</div>
        {sum && (
          <div className="row" style={{ gap: 10, fontSize: 13 }}>
            <span style={{ color: 'var(--ok,#63c869)' }}>+{sum.add} add</span>
            <span style={{ color: 'var(--warn,#e0a83b)' }}>~{sum.change} change</span>
            <span style={{ color: 'var(--err,#e5534b)' }}>-{sum.destroy} destroy</span>
          </div>
        )}
      </div>
      {list.length === 0 ? <div className="muted" style={{ padding: 8 }}>No resource changes detected yet.</div> : (
        <div className="viz-grid">
          {list.map((r) => {
            const [sign, col] = ACT[r.action] || ACT.noop
            return (
              <div key={r.addr} className="viz-host" style={{ borderLeft: `3px solid ${col}` }}>
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <b className="mono" style={{ fontSize: 12.5 }}><span style={{ color: col }}>{sign}</span> {r.addr}</b>
                  {r.status === 'in-progress' && <span className="dot pulse" style={{ background: col }} />}
                  {r.status === 'complete' && <span className="dot" style={{ background: 'var(--ok,#63c869)' }} />}
                </div>
                <div className="faint" style={{ fontSize: 11.5, marginTop: 4 }}>{r.action} · {r.status}</div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ------------------------------------------------------------------ Salt
function SaltViz({ model }) {
  const minions = Object.entries(model.minions).map(([name, m]) => ({ name, ...m }))
  return (
    <div className="viz">
      <div className="viz-head"><div style={{ fontWeight: 600 }}>Salt minions</div>
        <div className="faint" style={{ fontSize: 12 }}>{minions.length} minion(s)</div></div>
      {minions.length === 0 ? <div className="muted" style={{ padding: 8 }}>Waiting for minion results…</div> : (
        <div className="viz-grid">
          {minions.map((m) => {
            const col = m.failed > 0 ? 'var(--err,#e5534b)' : m.changed > 0 ? 'var(--warn,#e0a83b)' : 'var(--ok,#63c869)'
            return (
              <div key={m.name} className="viz-host" style={{ borderLeft: `3px solid ${col}` }}>
                <div className="row" style={{ justifyContent: 'space-between' }}>
                  <b className="mono" style={{ fontSize: 13 }}>{m.name}</b>
                  <span className="dot" style={{ background: col }} />
                </div>
                <div className="faint" style={{ fontSize: 11.5, marginTop: 4 }}>
                  <span style={{ color: 'var(--ok,#63c869)' }}>{m.succeeded} ok</span>
                  {m.changed ? <> · <span style={{ color: 'var(--warn,#e0a83b)' }}>{m.changed} changed</span></> : null}
                  {m.failed ? <> · <span style={{ color: 'var(--err,#e5534b)' }}>{m.failed} failed</span></> : null}
                  {m.total ? ` · ${m.total} states` : ''}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
