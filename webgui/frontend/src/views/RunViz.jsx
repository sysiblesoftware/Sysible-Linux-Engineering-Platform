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

// The WHOLE run as a stage flow — Terraform → Inventory → Ansible → Salt (whatever
// steps were launched) — shown above the per-stage detail. Each node is one step's
// run, coloured by its status, the one you're viewing ringed. Clicking a stage opens
// that step's run, which swaps BOTH the detail viz and the log below to it — so the
// log and steps track the stage you select. Renders nothing for a lone (non-pipeline)
// run. `seq` is the group's runs [{id, kind, target, status}], from /pipelines/runs.
const STAGE_ICON = { terraform: '⬢', inventory: '▤', ansible: '⏻', salt: '◆' }
const STAGE_COLOR = {
  success: 'var(--ok,#63c869)', failed: 'var(--err,#e5534b)', canceled: '#7d8ca3',
  running: 'var(--warn,#e0a83b)', queued: '#7d8ca3', pending: '#7d8ca3',
}
export function PipelineFlow({ seq, runId, onOpenRun }) {
  if (!seq || seq.length < 2) return null
  return (
    <div className="pipe-flow">
      {seq.map((s, i) => {
        const col = STAGE_COLOR[s.status] || STAGE_COLOR.pending
        const current = s.id === runId
        const label = (s.kind || '').charAt(0).toUpperCase() + (s.kind || '').slice(1)
        return (
          <React.Fragment key={s.id}>
            <button className={'pipe-stage' + (current ? ' current' : '')} style={{ borderColor: col }}
              title={`${s.kind} · ${s.target} · ${s.status}${current ? ' — viewing' : ' — click to view this stage'}`}
              onClick={() => !current && onOpenRun && onOpenRun(s.id)}>
              <span className="pipe-ico" style={{ color: col }}>{STAGE_ICON[s.kind] || '●'}</span>
              <span className="pipe-body">
                <b>{label}</b>
                <span className="faint mono">{s.target}</span>
              </span>
              <span className={'pipe-dot' + (s.status === 'running' ? ' pulse' : '')} style={{ background: col }} />
            </button>
            {i < seq.length - 1 && <span className="pipe-arrow" aria-hidden="true">→</span>}
          </React.Fragment>
        )
      })}
    </div>
  )
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
// Terraform resources for a VM build come indexed per machine — libvirt_domain.vm[0],
// libvirt_volume.disk[0], libvirt_cloudinit_disk.ci[0] — so we group by that [N]
// index into "machines" and show each machine building (like the Ansible host grid):
// a SLEP → machines flow up top, then a card per machine with its components' live
// status + elapsed. Resources with no index (networks, resource groups) are shown
// as shared infrastructure.
const TF_STATUS_COLOR = { complete: 'var(--ok,#63c869)', 'in-progress': 'var(--warn,#e0a83b)', planned: '#7d8ca3', error: 'var(--err,#e5534b)' }

// Friendly component label from a resource address.
function compKind(addr) {
  const type = (addr.split('.')[0] || '').toLowerCase()
  if (type.includes('domain') || type.includes('instance') || type.includes('droplet') || (type.includes('virtual_machine') && !type.includes('interface'))) return 'machine'
  if (type.includes('cloudinit') || type.includes('cloud_init')) return 'cloud-init'
  if (type.includes('volume') || type.includes('disk')) return 'disk'
  if (type.includes('network_interface') || type.includes('_nic')) return 'NIC'
  if (type.includes('public_ip')) return 'public IP'
  if (type.includes('network') || type.includes('subnet') || type.includes('vnet')) return 'network'
  if (type.includes('resource_group')) return 'resource group'
  return type.replace(/^[a-z]+_/, '').replace(/_/g, ' ')
}
const aggStatus = (rs) => rs.some((r) => r.status === 'error') ? 'error'
  : rs.every((r) => r.status === 'complete') ? 'complete'
    : rs.some((r) => r.status === 'in-progress') ? 'in-progress' : 'planned'

function TerraformViz({ model }) {
  const sum = model.applied || model.plan
  // Is this a destroy? (all-destroy actions, or a plan that only destroys.) Drives
  // the wording so a teardown reads "Destroying…/Destroyed/destroyed" — not "created".
  const acts = Object.values(model.resources).map((r) => r.action)
  const isDestroy = (acts.length > 0 && acts.includes('destroy') && !acts.includes('create') && !acts.includes('update'))
    || (!!model.plan && model.plan.add === 0 && model.plan.change === 0 && model.plan.destroy > 0)
  const anyBusy = Object.values(model.resources).some((r) => r.status !== 'planned')
  const label = isDestroy
    ? (model.applied ? 'Destroyed' : model.errored ? 'Error' : model.plan ? (anyBusy ? 'Destroying…' : 'Planned') : 'Working…')
    : (model.applied ? 'Applied' : model.errored ? 'Error' : model.plan ? (anyBusy ? 'Applying…' : 'Planned') : 'Working…')

  // Group resources by their trailing [N] index → one machine each; the rest is
  // shared infrastructure.
  const machines = {}, loose = []
  for (const [addr, r] of Object.entries(model.resources)) {
    const idx = addr.match(/\[(\d+)\]\s*$/)
    if (idx) { const k = idx[1]; (machines[k] || (machines[k] = { key: k, comps: [] })).comps.push({ addr, kind: compKind(addr), ...r }) }
    else loose.push({ addr, kind: compKind(addr), ...r })
  }
  const mlist = Object.values(machines).sort((a, b) => Number(a.key) - Number(b.key))
    // Prefer the real VM name (the domain resource's `name = "…"`, e.g. prod-web-1)
    // over a generic "Machine N"; fall back to any named component, then the index.
    .map((m) => ({
      ...m,
      status: aggStatus(m.comps),
      name: (m.comps.find((c) => c.kind === 'machine') || {}).name
        || (m.comps.find((c) => c.name) || {}).name || '',
    }))
  const mlabel = (m) => m.name || `Machine ${Number(m.key) + 1}`
  const compOrder = { disk: 0, 'cloud-init': 1, NIC: 2, machine: 3 }
  const sortComps = (cs) => [...cs].sort((a, b) => (compOrder[a.kind] ?? 5) - (compOrder[b.kind] ?? 5))

  return (
    <div className="viz">
      <div className="viz-head">
        <div style={{ fontWeight: 600 }}>{label}</div>
        <div className="spacer" />
        {mlist.length > 0 && <span className="faint" style={{ fontSize: 12 }}>{mlist.length} machine(s)</span>}
        {sum && (
          <div className="row" style={{ gap: 10, fontSize: 13 }}>
            <span style={{ color: 'var(--ok,#63c869)' }}>+{sum.add}</span>
            <span style={{ color: 'var(--warn,#e0a83b)' }}>~{sum.change}</span>
            <span style={{ color: 'var(--err,#e5534b)' }}>-{sum.destroy}</span>
          </div>
        )}
      </div>

      {Object.keys(model.resources).length === 0
        ? <div className="muted" style={{ padding: 8 }}>Planning… no resource changes detected yet.</div>
        : (
          <>
            {mlist.length > 0 && (
              <div className="viz-flow-box">
                <div className="flow-legend faint">
                  <span><i style={{ background: TF_STATUS_COLOR.complete }} />{isDestroy ? 'destroyed' : 'created'}</span>
                  <span><i style={{ background: TF_STATUS_COLOR['in-progress'] }} />{isDestroy ? 'destroying' : 'building'}</span>
                  <span><i style={{ background: TF_STATUS_COLOR.planned }} />planned</span>
                  <span><i style={{ background: TF_STATUS_COLOR.error }} />error</span>
                </div>
                <div className="flow-resize"><MachineFlow machines={mlist} isDestroy={isDestroy} mlabel={mlabel} /></div>
              </div>
            )}

            <div className="viz-grid">
              {mlist.map((m) => {
                const col = TF_STATUS_COLOR[m.status]
                const busy = m.comps.find((c) => c.status === 'in-progress')
                return (
                  <div key={m.key} className="viz-host" style={{ borderLeft: `3px solid ${col}` }}>
                    <div className="row" style={{ justifyContent: 'space-between' }}>
                      <b className="mono" style={{ fontSize: 13 }}>{mlabel(m)}</b>
                      {m.status === 'in-progress'
                        ? <span className="dot pulse" style={{ background: col }} />
                        : <span className="dot" style={{ background: col }} />}
                    </div>
                    <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 3 }}>
                      {sortComps(m.comps).map((c) => (
                        <div key={c.addr} className="row" style={{ gap: 6, fontSize: 11.5 }}>
                          <span className="dot" style={{ background: TF_STATUS_COLOR[c.status], width: 7, height: 7 }} />
                          <span style={{ minWidth: 66 }}>{c.kind}</span>
                          <span className="faint">
                            {c.status === 'complete' ? (c.took ? `${isDestroy ? 'removed' : 'done'} in ${c.took}` : (isDestroy ? 'removed' : 'done'))
                              : c.status === 'in-progress' ? (isDestroy ? (c.elapsed ? `destroying… ${c.elapsed}` : 'destroying…') : (c.elapsed ? `building… ${c.elapsed}` : 'building…'))
                                : c.status === 'error' ? 'error' : 'planned'}
                          </span>
                        </div>
                      ))}
                    </div>
                    {!isDestroy && busy && busy.kind === 'disk' && <div className="faint" style={{ fontSize: 10.5, marginTop: 4 }}>disk is slow while the base image is copied</div>}
                  </div>
                )
              })}
            </div>

            {loose.length > 0 && (
              <>
                <div className="pane-title" style={{ marginTop: 10 }}>Shared infrastructure</div>
                <div className="viz-grid">
                  {loose.map((r) => (
                    <div key={r.addr} className="viz-host" style={{ borderLeft: `3px solid ${TF_STATUS_COLOR[r.status]}` }}>
                      <div className="row" style={{ justifyContent: 'space-between' }}>
                        <b className="mono" style={{ fontSize: 12 }}>{r.kind}</b>
                        <span className={'dot' + (r.status === 'in-progress' ? ' pulse' : '')} style={{ background: TF_STATUS_COLOR[r.status] }} />
                      </div>
                      <div className="faint" style={{ fontSize: 11, marginTop: 3 }}>{r.addr}</div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </>
        )}
    </div>
  )
}

// SLEP → machines: the box fanning out to each machine being built, coloured by
// status (green created · amber building · dim planned · red error), with a live
// elapsed on whatever's still building — the Terraform analogue of the Ansible
// host-reach flow.
function MachineFlow({ machines, isDestroy = false, mlabel = (m) => `Machine ${Number(m.key) + 1}` }) {
  const n = machines.length
  const rowH = n > 12 ? 22 : 30
  const H = Math.max(90, n * rowH + 16)
  const cx = 40, cy = H / 2, ex = 150, nodeX = ex, nameX = ex + 16
  const step = n > 1 ? (H - 30) / (n - 1) : 0
  const rowY = (i) => (n > 1 ? 15 + i * step : cy)
  const W = 460
  return (
    <div style={{ overflow: 'auto', height: '100%' }}>
      <svg className="viz-flow" width={W} height={H} viewBox={`0 0 ${W} ${H}`}>
        {machines.map((m, i) => {
          const y = rowY(i)
          const col = TF_STATUS_COLOR[m.status]
          const active = m.status !== 'planned'
          const busy = m.comps.find((c) => c.status === 'in-progress')
          const done = m.comps.filter((c) => c.status === 'complete').length
          return (
            <g key={m.key}>
              <path d={`M ${cx + 15} ${cy} C ${(cx + ex) / 2} ${cy}, ${(cx + ex) / 2} ${y}, ${nodeX - 8} ${y}`}
                fill="none" stroke={col} strokeWidth={active ? 1.9 : 1} strokeOpacity={active ? 0.85 : 0.25} />
              {m.status === 'in-progress'
                ? <circle cx={nodeX} cy={y} r={5.5} fill="none" stroke={col} strokeWidth="2" className="pulse" />
                : <circle cx={nodeX} cy={y} r={5} fill={col} fillOpacity={active ? 1 : 0.3} />}
              <text x={nameX} y={y - 2} fontSize="12" fill="var(--text)" className="mono">{mlabel(m)}</text>
              <text x={nameX} y={y + 11} fontSize="10.5" fill="var(--muted)">
                {m.status === 'complete' ? (isDestroy ? 'destroyed' : 'created')
                  : m.status === 'error' ? 'error'
                    : busy ? `${isDestroy ? 'destroying' : 'building'} ${busy.kind}${busy.elapsed ? ' · ' + busy.elapsed : ''} (${done}/${m.comps.length})`
                      : 'planned'}
              </text>
            </g>
          )
        })}
        <ServerIcon cx={cx} cy={cy} />
      </svg>
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
