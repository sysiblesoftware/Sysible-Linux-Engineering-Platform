import React, { useEffect, useState } from 'react'
import { api, canWrite } from '../api.js'
import { PipelineModal } from './Ide.jsx'
import { JumpHostEditor, EnrollPicker, VmsModal } from './Infrastructure.jsx'

// The infrastructure lifecycle actions (Plan/Apply/Destroy, → Inventory, Access,
// Enroll, Configure/Maintain, Cadence) for ONE infra project — the workflow that
// used to live only on the Infrastructure page. Surfaced two ways so you can drive
// the build from either place and switch back and forth:
//   • <InfraActions variant="bar">  → a button bar on top of the IDE
//   • useInfraRowActions()          → items for the Projects row ⋯ menu
// Both share one set of handlers (below) and the same modals.

// Build the handler set + the [label, fn, class, title] action list for an infra
// row `r`, wired to the given modal setters / callbacks.
function buildActions(r, { controllers, setPipe, setVms, setEnrollFor, setJumpFor, onOpenRun, onOpenProject, refresh }) {
  const openProj = (extra) => onOpenProject &&
    onOpenProject({ id: r.project_id, name: r.project_name, slug: r.project_slug, ...extra })
  const run = async (target) => {
    try { const d = await api('runs', { method: 'POST', json: { project_id: r.project_id, kind: 'terraform', target } }); onOpenRun && onOpenRun(d.run_id) }
    catch (e) { alert(e.message) }
  }
  const enroll = async (controllerId) => {
    if (!r.controller_id && !controllerId) {
      if (!controllers.length) { alert('Connect a Controller first (Controllers tab).'); return }
      setEnrollFor(r); return
    }
    try {
      const body = controllerId ? { controller_id: Number(controllerId) } : {}
      const d = await api(`infra/${r.project_id}/enroll`, { method: 'POST', json: body })
      setEnrollFor(null); refresh && refresh()
      const lines = d.results.map((h) => `${h.ok ? '✓' : '✗'} ${h.name} ${h.ip} — ${h.detail}`).join('\n')
      alert(`Enrolled ${d.enrolled}/${d.total} into ${d.controller}:\n\n${lines || '(no hosts yet — apply the VMs first)'}`)
    } catch (e) { alert(e.message) }
  }
  const toInventory = async () => {
    try { const d = await api(`infra/${r.project_id}/inventory`, { method: 'POST' }); alert(`Built inventory “${d.name}” with ${d.hosts} host(s).`) }
    catch (e) { alert(e.message) }
  }
  const scaffold = async (stage) => {
    try { const d = await api(`infra/${r.project_id}/scaffold`, { method: 'POST', json: { stage } }); openProj({ openFile: d.path }) }
    catch (e) { alert(e.message) }
  }
  const listVms = async () => {
    setVms({ name: r.project_name, loading: true })
    try { const d = await api(`infra/${r.project_id}/vms`, { method: 'POST' }); setVms({ name: r.project_name, list: d.vms || [], error: d.ok ? '' : d.output }) }
    catch (e) { setVms({ name: r.project_name, error: e.message }) }
  }
  const cadence = async () => {
    try {
      await api(`infra/${r.project_id}/scaffold`, { method: 'POST', json: { stage: 'configure' } })
      await api(`infra/${r.project_id}/scaffold`, { method: 'POST', json: { stage: 'maintain' } })
      setPipe({
        project: { id: r.project_id, name: r.project_name, slug: r.project_slug },
        steps: [
          { kind: 'terraform', target: 'apply', tool: 'terraform' },
          { kind: 'inventory', target: 'from VMs' },
          { kind: 'ansible', target: 'configure.yml' },
          { kind: 'salt', target: 'maintain.sls' },
        ],
      })
    } catch (e) { alert(e.message) }
  }
  return [
    ['Plan', () => run('plan'), 'ghost', 'Terraform/OpenTofu plan'],
    ['Apply', () => run('apply'), 'primary', 'Terraform/OpenTofu apply — create the VMs'],
    ['Destroy', () => run('destroy'), 'danger ghost', 'Terraform/OpenTofu destroy', true],
    ['🖥 VMs', listVms, 'ghost', "List the VMs on this project's hypervisor"],
    ['→ Inventory', toInventory, 'ghost', 'Read the applied VMs into a SLEP inventory'],
    ['⚙ Access', () => setJumpFor(r), 'ghost', 'Login user, password (Vault) and jump host'],
    ['Enroll → Controller', () => enroll(), 'primary', 'Register the applied VMs into a Controller'],
    ['Configure', () => scaffold('configure'), 'ghost', 'Scaffold an Ansible playbook and open it'],
    ['Maintain', () => scaffold('maintain'), 'ghost', 'Scaffold a Salt state and open it'],
    ['▶ Cadence', cadence, 'primary', 'Run the whole cadence: apply → configure → maintain'],
  ].map(([label, fn, cls, title, danger]) => ({ label, fn, cls, title, danger, enroll }))
}

// Shared modal state + the modals JSX. `refresh` is called after enroll/access save.
function useInfraModals({ onOpenRun, refresh }) {
  const [controllers, setControllers] = useState([])
  const [pipe, setPipe] = useState(null)
  const [vms, setVms] = useState(null)
  const [enrollFor, setEnrollFor] = useState(null)
  const [jumpFor, setJumpFor] = useState(null)
  useEffect(() => { api('controllers').then((d) => setControllers(d.controllers || [])).catch(() => {}) }, [])
  const enrollPick = enrollFor && buildActions(enrollFor, { controllers, setPipe, setVms, setEnrollFor, setJumpFor, onOpenRun, refresh })[0].enroll
  const modals = (
    <>
      {pipe && <PipelineModal project={pipe.project} initialSteps={pipe.steps}
        onClose={() => setPipe(null)} onLaunched={(id) => { setPipe(null); onOpenRun && onOpenRun(id) }} />}
      {vms && <VmsModal data={vms} onClose={() => setVms(null)} />}
      {enrollFor && <EnrollPicker r={enrollFor} controllers={controllers}
        onClose={() => setEnrollFor(null)} onPick={(cid) => enrollPick(cid)} />}
      {jumpFor && <JumpHostEditor r={jumpFor} onClose={() => setJumpFor(null)}
        onSaved={() => { setJumpFor(null); refresh && refresh() }} />}
    </>
  )
  return { controllers, setPipe, setVms, setEnrollFor, setJumpFor, modals }
}

// Bar on top of the IDE. Fetches this project's infra row; renders nothing for a
// non-infra project or a viewer, so it's safe to mount above every open project.
export function InfraActions({ project, onOpenRun, onOpenProject, onReload, refreshKey }) {
  const [r, setR] = useState(null)
  const loadRow = () => api('infra')
    .then((d) => setR((d.infra || []).find((x) => x.project_id === project.id) || null))
    .catch(() => setR(null))
  useEffect(() => { loadRow() }, [project.id, refreshKey])   // eslint-disable-line
  const refresh = () => { loadRow(); onReload && onReload() }
  const m = useInfraModals({ onOpenRun, refresh })
  if (!canWrite() || !r) return null
  const actions = buildActions(r, { ...m, onOpenRun, onOpenProject, refresh })
  return (
    <div className="infra-bar">
      <span className="infra-bar-label" title="Infrastructure project">☁ {r.provider}{r.bastion ? ` · via ${r.bastion}` : ''}</span>
      <div className="infra-bar-actions">
        {actions.map((a) => <button key={a.label} className={a.cls + ' sm'} title={a.title} onClick={a.fn}>{a.label}</button>)}
      </div>
      {m.modals}
    </div>
  )
}

// Hook for the Projects row ⋯ menu. Returns `itemsFor(infraRow)` (menu items in the
// RowMenu shape) and the shared `modals` to render once at the Projects level.
export function useInfraRowActions({ onOpenRun, onOpenProject, onReload }) {
  const m = useInfraModals({ onOpenRun, refresh: onReload })
  const itemsFor = (infraRow) => {
    if (!infraRow || !canWrite()) return []
    const actions = buildActions(infraRow, { ...m, onOpenRun, onOpenProject, refresh: onReload })
    return [{ sep: true }, ...actions.map((a) => ({ label: a.label, run: a.fn, danger: a.danger }))]
  }
  return { itemsFor, modals: m.modals }
}
