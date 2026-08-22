import React, { useState } from 'react'

// AAP-style run visualizations. Each takes the parsed model from runParse.js and
// renders a live, at-a-glance picture of the run: which hosts the playbook is
// reaching and how they're faring (Ansible), what Terraform is about to (or did)
// change, and how each Salt minion came out. Purely presentational — the parser
// owns all the log-reading.

const STATUS_COLOR = {
  ok: 'var(--ok, #63c869)', changed: 'var(--warn, #e0a83b)', failed: 'var(--err, #e5534b)',
  unreachable: '#c678dd', skipped: '#7d8ca3', pending: '#7d8ca3',
  notrun: '#8b7fd0',   // task never ran — host failed/aborted earlier, or the run ended
}

export default function RunViz({ engine, model, seedHosts }) {
  if (engine === 'terraform') return <TerraformViz model={model} />
  if (engine === 'salt') return <SaltViz model={model} />
  if (engine === 'inventory') return <InventoryViz model={model} />
  return <AnsibleViz model={model} seedHosts={seedHosts} />
}

// The pipeline's auto-inventory pseudo-step: no hosts to reach, it just reads the
// freshly-applied VMs into the project's inventory. Surface the hosts it built and
// the outcome (parsed from the run log) rather than an empty Ansible grid.
function InventoryViz({ model }) {
  const lines = (model.raw || '').split('\n')
  const built = lines.find((l) => l.includes('Built inventory')) || ''
  const failed = lines.find((l) => l.trim().startsWith('!!')) || ''
  return (
    <div style={{ padding: 10 }}>
      <div className="pane-title" style={{ marginTop: 0 }}>Build inventory from VMs</div>
      {failed
        ? <div className="pill failed" style={{ display: 'inline-block' }}>{failed.replace(/^!!\s*/, '')}</div>
        : built
          ? <div style={{ fontSize: 13 }}>✓ {built.trim()}</div>
          : <div className="muted">Reading the applied VMs into this project’s inventory…</div>}
      <div className="faint" style={{ fontSize: 12, marginTop: 10 }}>
        The Ansible/Salt steps that follow in this sequence are pointed at the inventory built here.
      </div>
    </div>
  )
}

// -------------------------------------------------------------- Ansible
function AnsibleViz({ model, seedHosts }) {
  // Merge live results over any seeded (inventory) hosts so the grid shows every
  // target from the start, filling in as the play reaches them.
  const names = Array.from(new Set([...(seedHosts || []), ...Object.keys(model.hosts)])).sort()
  const hosts = names.map((n) => ({ name: n, ...(model.hosts[n] || { ok: 0, changed: 0, failed: 0, skipped: 0, unreachable: 0, last: 'pending' }) }))
  const recap = model.recap
  const [zoom, setZoom] = useState(1)
  const nudge = (d) => setZoom((z) => Math.min(3, Math.max(0.5, Math.round((z + d) * 10) / 10)))

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
          <div className="zoom-ctl">
            <button className="ghost sm" title="Zoom out" onClick={() => nudge(-0.2)}>−</button>
            <button className="ghost sm" title="Reset zoom" onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>
            <button className="ghost sm" title="Zoom in" onClick={() => nudge(0.2)}>+</button>
          </div>
          <span className="faint" style={{ fontSize: 12 }}>{hosts.length} host(s) · {model.tasks} task(s)</span>
        </div>
        <div className="flow-legend faint">
          <span><i style={{ background: STATUS_COLOR.ok }} />ok</span>
          <span><i style={{ background: STATUS_COLOR.changed }} />changed</span>
          <span><i style={{ background: STATUS_COLOR.failed }} />failed</span>
          <span><i style={{ background: STATUS_COLOR.unreachable }} />unreachable</span>
          <span><i style={{ background: 'transparent', border: `2px solid ${STATUS_COLOR.notrun}` }} />didn’t run</span>
        </div>
        <div className="flow-resize">
          <HostReachFlow hosts={hosts} tasks={model.tasks} hostTasks={model.hostTasks || {}} taskList={model.taskList || []} done={!!recap} zoom={zoom} />
        </div>
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
// Colour of a track segment by the outcome of the task it leaves: green when the
// task completed and the flow continued, red on failure, purple unreachable,
// else a dim track (pending / not run).
const segColor = (st) => (st === 'ok' || st === 'changed') ? STATUS_COLOR.ok
  : st === 'failed' ? STATUS_COLOR.failed
    : st === 'unreachable' ? STATUS_COLOR.unreachable
      : 'var(--line-strong)'

function HostReachFlow({ hosts, tasks, hostTasks, taskList, done, zoom = 1 }) {
  const n = hosts.length
  const nTasks = Math.max(1, tasks || 0)
  // Scale down for big playbooks: tighter dots/rows, and drop the angled column
  // headers once they'd overlap (tooltips still name each dot). Everything stays
  // inside the pane's scroll, so hundreds of tasks/hosts just scroll.
  const dense = nTasks > 18
  const dotGap = nTasks > 60 ? 8 : nTasks > 30 ? 11 : dense ? 15 : 22
  const dotR = nTasks > 60 ? 2.6 : nTasks > 30 ? 3.2 : 4.5
  const rowH = n > 40 ? 15 : n > 20 ? 20 : 26
  const maxLabel = 26
  const trunc = (s) => (s.length > maxLabel ? s.slice(0, maxLabel - 1) + '…' : s)
  // Header height must fit the angled (-40°) labels so their tops aren't clipped:
  // rise ≈ sin(40°)·labelWidth. Size it from the longest visible label.
  const longest = (tasks > 0 && !dense)
    ? Math.min(maxLabel, Math.max(6, ...Array.from({ length: nTasks }, (_, i) => (taskList[i]?.name || '').length)))
    : 0
  const headerH = (tasks > 0 && !dense) ? Math.round(20 + longest * 4.2) : 10
  const bodyH = Math.max(84, n * rowH + 18)
  const H = headerH + bodyH
  const cx = 46, cy = headerH + bodyH / 2, ex = 150   // ex = where the fan lines end
  const dotsX = ex + 20
  const lastDotX = dotsX + (nTasks - 1) * dotGap
  const nameX = lastDotX + 16
  const W = nameX + 160 + Math.round(longest * 3)   // room for the rightmost angled label
  const step = n > 1 ? (bodyH - 26) / (n - 1) : 0
  const rowY = (i) => n > 1 ? headerH + 13 + i * step : cy
  return (
    <div style={{ overflow: 'auto', height: '100%' }}>
      <svg className="viz-flow" width={Math.round(W * zoom)} height={Math.round(H * zoom)} viewBox={`0 0 ${W} ${H}`}>
        {/* Task column headers (angled) + faint guide line down each column.
            Hidden when there are too many tasks to label without overlap. */}
        {tasks > 0 && !dense && Array.from({ length: nTasks }).map((_, ti) => {
          const x = dotsX + ti * dotGap
          return (
            <g key={'h' + ti}>
              <line x1={x} y1={headerH - 6} x2={x} y2={H - 8} stroke="var(--line)" strokeOpacity="0.5" strokeWidth="1" />
              <text x={x} y={headerH - 10} fontSize="10.5" fill="var(--muted)" textAnchor="start"
                transform={`rotate(-40 ${x} ${headerH - 10})`}>{trunc(taskList[ti]?.name || 'task ' + (ti + 1))}
                <title>{taskList[ti]?.name || 'task ' + (ti + 1)}</title>
              </text>
            </g>
          )
        })}
        {dense && <text x={dotsX} y={8} fontSize="10.5" fill="var(--faint)">{nTasks} tasks — hover a dot for its name</text>}
        {hosts.map((h, i) => {
          const y = rowY(i)
          const col = STATUS_COLOR[h.last] || STATUS_COLOR.pending
          const reached = h.last !== 'pending'
          const statuses = hostTasks[h.name] || []
          // Effective status per task: after a host fails/becomes unreachable
          // (or once the run has ended), tasks it never reached are 'notrun'.
          let stopped = false
          const eff = Array.from({ length: nTasks }).map((_, ti) => {
            const st = statuses[ti]
            if (st) { if (st === 'failed' || st === 'unreachable') stopped = true; return st }
            return (stopped || done) ? 'notrun' : 'pending'
          })
          return (
            <g key={h.name}>
              {/* server → host fan line, coloured by whether the first task passed */}
              <path d={`M ${cx + 15} ${cy} C ${(cx + ex) / 2} ${cy}, ${(cx + ex) / 2} ${y}, ${ex - 6} ${y}`}
                fill="none" stroke={reached ? segColor(eff[0]) : col} strokeWidth={reached ? 1.8 : 1} strokeOpacity={reached ? 0.85 : 0.22} />
              <circle cx={ex} cy={y} r={4} fill={col} fillOpacity={reached ? 1 : 0.3} />
              {/* each track segment reflects the task it LEAVES: green if that task
                  completed and the flow continued, red on failure. */}
              {eff.map((st, ti) => {
                const x2 = dotsX + ti * dotGap
                const x1 = ti === 0 ? ex : dotsX + (ti - 1) * dotGap
                return <line key={'seg' + ti} x1={x1} y1={y} x2={x2} y2={y}
                  stroke={segColor(ti === 0 ? eff[0] : eff[ti - 1])} strokeWidth="2"
                  strokeOpacity={eff[ti === 0 ? 0 : ti - 1] === 'pending' || eff[ti === 0 ? 0 : ti - 1] === 'notrun' ? 0.35 : 0.85} />
              })}
              {eff.map((st, ti) => {
                const ran = st !== 'pending' && st !== 'notrun'
                const c = STATUS_COLOR[st] || STATUS_COLOR.pending
                // 'notrun' shows as a hollow purple ring; ran = solid; pending = dim.
                return (
                  <circle key={ti} cx={dotsX + ti * dotGap} cy={y} r={dotR}
                    fill={st === 'notrun' ? 'none' : c} fillOpacity={ran ? 1 : (st === 'pending' ? 0.3 : 1)}
                    stroke={st === 'notrun' ? STATUS_COLOR.notrun : 'var(--bg)'} strokeWidth={st === 'notrun' ? 1.8 : 1.5}>
                    <title>{`${h.name} · ${taskList[ti]?.name || 'task ' + (ti + 1)}: ${st}`}</title>
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
