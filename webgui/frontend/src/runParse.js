// Pure parsers that turn streamed engine output into a structured, live model the
// run visualizations render. Each takes the full accumulated log text and returns
// a normalized snapshot — no state carried between calls, so re-parsing the growing
// log on every poll is cheap and always consistent. All parsing is done on the
// ANSI-stripped text (runners emit colour codes).

export const stripAnsi = (s) => s.replace(/\[[0-9;]*m/g, '')

// ---------------------------------------------------------------- Ansible
// Reads ansible-playbook's default (human) output: PLAY / TASK banners, per-host
// ok/changed/skipping/unreachable/fatal lines, and the closing PLAY RECAP.
export function parseAnsible(text) {
  const t = stripAnsi(text)
  const hosts = {}
  const bump = (name, key) => {
    const h = hosts[name] || (hosts[name] = { ok: 0, changed: 0, failed: 0, skipped: 0, unreachable: 0, last: 'pending' })
    h[key] += 1
    h.last = key === 'ok' ? 'ok' : key === 'changed' ? 'changed'
      : key === 'skipped' ? 'skipped' : key === 'unreachable' ? 'unreachable' : 'failed'
  }
  let currentTask = null, currentPlay = null, plays = 0, tasks = 0, taskIdx = -1
  const taskList = []          // ordered {play, name}
  const hostTasks = {}         // host -> [status per task index] — the per-host, per-task grid
  const recap = {}
  let inRecap = false

  for (const line of t.split('\n')) {
    const play = line.match(/^PLAY \[(.+?)\]/)
    if (play) { currentPlay = play[1]; plays += 1; inRecap = false; continue }
    if (/^PLAY RECAP/.test(line)) { inRecap = true; continue }
    const task = line.match(/^TASK \[(.+?)\]/)
    if (task) { currentTask = task[1]; tasks += 1; taskIdx += 1; taskList.push({ play: currentPlay, name: task[1] }); continue }

    if (inRecap) {
      // web1  : ok=3 changed=1 unreachable=0 failed=0 skipped=1 rescued=0 ignored=0
      const m = line.match(/^(\S+)\s*:\s*(ok=.*)/)
      if (m) {
        const stats = {}
        for (const kv of m[2].matchAll(/(\w+)=(\d+)/g)) stats[kv[1]] = Number(kv[2])
        recap[m[1]] = stats
      }
      continue
    }
    // Per-host result lines: "ok: [web1]", "changed: [web1] => {...}", etc.
    const r = line.match(/^(ok|changed|skipping|failed|fatal|unreachable):\s*\[([^\]]+)\]/)
    if (r) {
      const host = r[2].split(' -> ')[0]
      const status = r[1] === 'ok' ? 'ok' : r[1] === 'changed' ? 'changed'
        : r[1] === 'skipping' ? 'skipped'
        : (/UNREACHABLE/.test(line) || r[1] === 'unreachable') ? 'unreachable' : 'failed'
      bump(host, status)
      if (taskIdx >= 0) (hostTasks[host] || (hostTasks[host] = []))[taskIdx] = status
    }
  }
  const hasRecap = Object.keys(recap).length > 0
  return { engine: 'ansible', hosts, recap: hasRecap ? recap : null, currentTask, currentPlay, plays, tasks, taskList, hostTasks }
}

// --------------------------------------------------------------- Terraform
// Reads terraform plan/apply output: the "# addr will be ..." plan lines, the
// "Plan: X to add..." summary, live "addr: Creating..." progress, and the final
// "Apply complete!" tally.
export function parseTerraform(text) {
  const t = stripAnsi(text)
  const resources = {}
  const res = (addr) => resources[addr] || (resources[addr] = { action: 'noop', status: 'planned' })
  let plan = null, applied = null, errored = false

  for (const line of t.split('\n')) {
    let m
    if ((m = line.match(/^\s*#\s*(\S+)\s+will be created/))) { res(m[1]).action = 'create' }
    else if ((m = line.match(/^\s*#\s*(\S+)\s+will be updated/))) { res(m[1]).action = 'update' }
    else if ((m = line.match(/^\s*#\s*(\S+)\s+will be destroyed/))) { res(m[1]).action = 'destroy' }
    else if ((m = line.match(/^\s*#\s*(\S+)\s+must be replaced/))) { res(m[1]).action = 'replace' }
    else if ((m = line.match(/^(\S+):\s*Creating\.\.\./))) { res(m[1]).action = 'create'; res(m[1]).status = 'in-progress' }
    else if ((m = line.match(/^(\S+):\s*Modifying\.\.\./))) { res(m[1]).action = 'update'; res(m[1]).status = 'in-progress' }
    else if ((m = line.match(/^(\S+):\s*Destroying\.\.\./))) { res(m[1]).action = 'destroy'; res(m[1]).status = 'in-progress' }
    // "addr: Still creating... [1m20s elapsed]" — keep the live elapsed so the viz
    // can show how long a slow resource (a volume pulling a base image) is taking.
    else if ((m = line.match(/^(\S+):\s*Still (?:creating|modifying|destroying)\.\.\.\s*\[(.+?)(?:\s+elapsed)?\]/))) {
      const r = res(m[1]); r.status = 'in-progress'; r.elapsed = m[2]
    } else if ((m = line.match(/^(\S+):\s*(?:Creation|Modifications|Destruction) complete(?:\s+after\s+(\S+))?(?:\s*\[id=(.+?)\])?/))) {
      const r = res(m[1]); r.status = 'complete'; r.elapsed = null; if (m[2]) r.took = m[2]; if (m[3]) r.id = m[3]
    } else if ((m = line.match(/^Plan:\s*(\d+) to add,\s*(\d+) to change,\s*(\d+) to destroy/))) {
      plan = { add: +m[1], change: +m[2], destroy: +m[3] }
    } else if ((m = line.match(/Apply complete!\s*Resources:\s*(\d+) added,\s*(\d+) changed,\s*(\d+) destroyed/))) {
      applied = { add: +m[1], change: +m[2], destroy: +m[3] }
    } else if (/^Error:/.test(line)) errored = true
  }
  return { engine: 'terraform', resources, plan, applied, errored }
}

// -------------------------------------------------------------------- Salt
// Reads salt-ssh output: the per-minion "Summary for <minion>" blocks with
// "Succeeded: N (changed=M)" / "Failed: N". Falls back to counting Result lines
// when a run errors before a summary prints.
export function parseSalt(text) {
  const t = stripAnsi(text)
  const minions = {}
  const lines = t.split('\n')
  let current = null

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    let m
    if ((m = line.match(/^Summary for (\S+)/))) {
      current = minions[m[1]] || (minions[m[1]] = { succeeded: 0, changed: 0, failed: 0, total: 0 })
    } else if (current && (m = line.match(/^Succeeded:\s*(\d+)(?:\s*\(changed=(\d+)\))?/))) {
      current.succeeded = +m[1]; current.changed = m[2] ? +m[2] : 0
    } else if (current && (m = line.match(/^Failed:\s*(\d+)/))) {
      current.failed = +m[1]
    } else if (current && (m = line.match(/^Total states run:\s*(\d+)/))) {
      current.total = +m[1]; current = null
    }
  }
  return { engine: 'salt', minions }
}

export function parseRun(engine, text) {
  if (engine === 'terraform') return parseTerraform(text)
  if (engine === 'salt') return parseSalt(text)
  // The pipeline's auto-inventory pseudo-step has no host/task model — its viz
  // reads the summary straight from the log, so hand back the raw text.
  if (engine === 'inventory') return { raw: text || '' }
  return parseAnsible(text)
}
