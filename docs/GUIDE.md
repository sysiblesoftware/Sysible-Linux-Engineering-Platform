# SLEP User Guide

*Sysible Linux Engineering Platform — author, orchestrate, and run your
automation (Ansible, Terraform, Salt) from one self-hosted console.*

This guide is task-oriented: it walks through what each part of SLEP is for and
how to use it, from your first playbook to a full **build → configure → maintain →
enroll** pipeline. For installing and operating the server (Docker, TLS, the
`slep` CLI, backups), see **[RUNNING.md](RUNNING.md)**.

---

## Contents

1. [The mental model](#1-the-mental-model)
2. [First login](#2-first-login)
3. [A tour of the console](#3-a-tour-of-the-console)
4. [Projects and the IDE](#4-projects-and-the-ide)
5. [Inventories](#5-inventories)
6. [Credentials](#6-credentials)
7. [Jump hosts (bastions)](#7-jump-hosts-bastions)
8. [Variable Vault](#8-variable-vault)
9. [Running automation](#9-running-automation)
10. [The run visualizer](#10-the-run-visualizer)
11. [Infrastructure: provisioning VMs](#11-infrastructure-provisioning-vms)
12. [Controllers and enrollment](#12-controllers-and-enrollment)
13. [Pipelines](#13-pipelines)
14. [Schedules](#14-schedules)
15. [Organizations, users, and roles](#15-organizations-users-and-roles)
16. [Activity](#16-activity)
17. [Troubleshooting](#17-troubleshooting)
18. [Reference](#18-reference)

---

## 1. The mental model

SLEP is **AAP, but friendly**: the same core objects as Ansible Automation
Platform, without the Kubernetes/mesh/operator sprawl. Five nouns carry almost
everything:

| Object | What it is |
|---|---|
| **Project** | A workspace of automation content — playbooks, Terraform configs, Salt states — authored in the in-browser IDE or synced from Git. |
| **Inventory** | The hosts (and groups) a run targets. Entered by hand or **imported from a Sysible Controller**. |
| **Credential** | An SSH key/password, cloud cred, or vault token — stored server-side, injected into a run, never shown to the browser. |
| **Run** | One execution of an engine (`ansible-playbook`, `terraform`, `salt`) against an inventory with a credential, streaming live output into a history. |
| **Pipeline** | An ordered sequence of runs — e.g. Terraform apply → Ansible → Salt → Enroll — visualized together. |

Everything else (Infrastructure, Controllers, Jump Hosts, Variable Vault,
Schedules, Organizations) supports those five.

The golden path: **author** content in a Project → point it at an **Inventory**
with a **Credential** → **Run** it → watch the **visualizer** → chain steps into a
**Pipeline**.

---

## 2. First login

Open the console (`https://<host>:8810` by default) and complete the **first-run
screen** to create your administrator account — you choose the password; nothing
is pre-seeded. That account is a superuser in the default organization.

The console is HTTPS with a self-signed cert out of the box; your browser warns
once, then remembers. (See RUNNING.md for putting your hostname in the cert or
fronting it with a real certificate.)

---

## 3. A tour of the console

The left nav groups everything by job:

| Nav item | Use it to… |
|---|---|
| **Projects** | Create/open a project and its IDE; launch runs and pipelines. |
| **Inventories** | Define target hosts/groups; import a fleet from a Controller. |
| **Infrastructure** | Provision VMs with Terraform (libvirt/KVM today) and drive their lifecycle. |
| **Controllers** | Connect a Sysible Controller to import inventory and enroll VMs. |
| **Credentials** | Store SSH/cloud/vault secrets used by runs. |
| **Jump Hosts** | Register bastions that runs and enrollment tunnel through. |
| **Variable Vault** | Reusable extra-vars / key-values injected into runs. |
| **Runs** | Full run history; open any run's log + visualizer; re-run. |
| **Pipelines** | Author and launch multi-step sequences. |
| **Schedules** | Run a job/pipeline on a recurring cadence. |
| **Organizations** | Multi-tenant boundaries; who can see/act on what. |
| **Activity** | The audit trail of who did what. |
| **Users** | Accounts and their roles. |

---

## 4. Projects and the IDE

A **project** is a folder of automation content plus its own run history.

**Create one:** *Projects → New project.* Give it a name; a slug and a per-project
workdir are created for you.

**The IDE** is a Monaco (VS Code) editor with a file tree, syntax highlighting for
YAML/HCL/Jinja, an inline linter, and a run panel:

- **+ File** to add `site.yml`, `main.tf`, a Salt state, etc. **Ctrl/⌘-S** saves.
- The linter flags common mistakes early (for example, brittle apt version pins).
- **Sync from Git** instead of authoring in-browser if you keep content in SCM.
- The **infra bar** at the top of an infrastructure project exposes its lifecycle
  actions (Plan / Apply / Destroy / Edit / Enroll) so you can drive a build
  without leaving the editor.

> **Tip:** the IDE needs a *secure context* (HTTPS) for clipboard/paste — another
> reason the console defaults to TLS.

---

## 5. Inventories

An **inventory** is the set of hosts a run targets.

**Create one:** *Inventories → New inventory*, then **+ Host** to add machines
(name, address, connection user/port, group). Groups let a playbook target a
subset (`hosts: web`).

**Import from a Controller** instead of typing: *Import from Controller* with a
Controller URL + backend API key pulls the fleet you already manage — both
agent-enrolled and SSH-managed hosts — and keeps their environment as an Ansible
group. Re-importing refreshes addresses/vars without duplicating (upsert by name).

Infrastructure projects also **auto-build an inventory** from the VMs they apply,
so the machines you just created are immediately targetable (see §11).

---

## 6. Credentials

A **credential** is how a run authenticates.

**Create one:** *Credentials → New* — an SSH private key, an SSH password, a cloud
credential, or a vault token. Secrets are encrypted at rest, injected into the run
process server-side, and **never surfaced to the browser** or returned by the API.

A run takes exactly one credential; pick it at launch. For password-based SSH,
SLEP uses `sshpass` under the hood (baked into the Docker image).

---

## 7. Jump hosts (bastions)

When target hosts aren't directly reachable, register a **jump host**: *Jump
Hosts → add a bastion* (address + user + key). Runs and enrollment then tunnel
through it:

- Ansible/Terraform SSH through the bastion via a `ProxyCommand`.
- `salt-ssh` uses the same hop (SLEP builds a correctly-quoted roster
  `ProxyCommand` so multi-hop targets work).
- Agent enrollment installs the bundle over the bastion too.

Set a project's bastion once and every run/enroll for it inherits the hop.

---

## 8. Variable Vault

The **Variable Vault** stores reusable key/value sets (extra-vars) you inject into
runs — environment names, versions, feature flags — so you don't paste them each
time. Values are stored server-side; reference a vault entry when launching a run
or pipeline step. Keys are restricted to a safe charset so they pass cleanly to
the engines.

---

## 9. Running automation

Every engine follows the same shape: pick a **target**, an **inventory**, and a
**credential**, then **Launch**. Output streams live; the status badge settles on
`success` / `failed` / `canceled`.

| Engine | Target | Notes |
|---|---|---|
| **Ansible** | a playbook (`site.yml`) | Live PLAY/TASK/RECAP parsing; **Re-run failed** targets just the hosts that broke (`--limit` + `--start-at-task`). |
| **Terraform / OpenTofu** | `plan` / `apply` / `destroy` | Self-heals stale provider locks and orphaned libvirt objects; auto-reads applied VMs into inventory. |
| **Salt** | a state or `highstate` | `salt-ssh` over your inventory (optionally through a jump host). SLS names are normalized (`maintain.sls` → `maintain`). |

**Stop a run** with the Stop button — the engine process is terminated and the run
is marked `canceled`. For Terraform **apply**, canceling automatically launches a
follow-up **Destroy** to clean up any partially-created infrastructure (it removes
everything in that project's state — cleanup-after-abandon, not a partial
rollback).

**Re-run** any past run from *Runs* with the same engine/target/inventory/
credential/vars.

---

## 10. The run visualizer

Every run opens with a **Visualize** pane beside the raw **Log** (drag the divider
to resize, or **Hide log**). The visualization is engine-aware:

- **Ansible** — a host × task grid: which hosts the play is reaching and how each
  task fared (ok / changed / failed / unreachable / didn't-run).
- **Terraform** — what's being created/changed/destroyed, live, by real VM name
  (not "Machine 1"); destroy mode says *destroyed*, not *created*.
- **Salt** — per-minion success/changed/failed and states run.
- **Enroll** — one row per VM with its ✓/✗ into the Controller and the reason on
  failure, plus the `X/Y enrolled → <controller>` tally.

For a **pipeline**, the whole run is shown as **one window**: the stages laid out
side by side (Terraform → Ansible → Salt → Enroll), each with its live status, so
you see the entire build at a glance instead of scrolling. A stage flow strip at
the top doubles as navigation.

---

## 11. Infrastructure: provisioning VMs

The **Infrastructure** area provisions real machines with Terraform (libvirt/KVM
today) and drives their lifecycle — no hand-written HCL required.

**Create infrastructure:** the wizard collects the provider, image, sizing, and
**VM groups**. A group is a named set of identical VMs (image + count + disk +
resources), so one project can build **more than one kind of machine in a single
apply** — e.g. two Ubuntu web nodes *and* one Arch box. Disk size is free-form
(any size, e.g. `100G`). Cloud-init (NoCloud) seeds users, keys, and passwords so
the VMs come up reachable.

**Lifecycle** (from the infra bar or the project row):

- **Plan / Apply / Destroy** — standard Terraform actions with confirmation on
  destroy.
- **Edit** — reopen the wizard to change the spec and **regenerate** the Terraform
  (e.g. add a VM type after a destroy). Existing wiring (bastion, controller) is
  preserved.
- **Apply auto-inventory** — after a successful apply, the new VMs are read into
  the project's inventory automatically, ready for Ansible/Salt.
- **Enroll** — register the applied VMs into a Controller (see §12).

---

## 12. Controllers and enrollment

A **Sysible Controller** is the fleet-administration control plane. SLEP connects
to one to (a) import its inventory and (b) **enroll** the VMs it builds so they
show up in the fleet.

**Connect a Controller:** *Controllers → Connect.* Authenticate one of two ways:

- **Username + password** — a Controller superuser signs in with their console
  credentials; SLEP exchanges them for the backend API key behind the scenes.
- **API key** — paste the raw backend key directly.

**TLS trust-on-first-use (TOFU):** a standalone/on-prem Controller usually serves a
**self-signed** cert. The first call that fails verification makes SLEP fetch the
presented cert, **pin it**, and retry — then reuse it for all later calls. You
don't copy a PEM anywhere. This applies during **enrollment** too, so if the
Controller's cert changes (e.g. it's rebuilt or moved container→standalone), the
next enroll re-pins the new cert and recovers on its own.

**Enroll VMs (the agent / pull model — default):** for each applied VM, SLEP
downloads a **one-time agent bundle** from the Controller (with the machine API
key), installs it over SSH (through the jump host if set), and the VM
**self-enrolls outbound**. No inbound SSH-as-root, no human superuser token. The
Enroll stage's visualization shows each VM's ✓/✗ and the `X/Y enrolled` tally.

> If the project has no Controller set, the **Enroll** action opens a picker; your
> choice is remembered for future enrolls and the next apply's key-bake.

There's also a legacy **SSH-host** method (`method: "ssh"`) that registers hosts
for the Controller to reach directly; the agent path is the default because it
works with SLEP's machine API key alone.

---

## 13. Pipelines

A **pipeline** runs an ordered list of steps as one launched sequence, sharing a
group so the visualizer shows the whole thing. Steps stop on the first failure by
default.

**Author one** in the IDE's **Pipeline** panel or on the project. A common shape:

```
Terraform apply  →  Inventory (from VMs)  →  Ansible site.yml  →  Salt maintain  →  Enroll → Controller
```

Two **pseudo-steps** make the end-to-end flow one click:

- **Inventory (from VMs)** — reads the freshly-applied VMs into the project's
  inventory and points the following Ansible/Salt steps at it.
- **Enroll → Controller** — registers the applied VMs into the project's
  Controller (agent enrollment) as the final step.

The pipeline plays out automatically, advancing stage to stage, all shown in the
single-window visualizer (§10).

---

## 14. Schedules

**Schedules** run a job or pipeline on a recurring cadence. Create one with a
target and a time; SLEP fires it on schedule and records each firing in the run
history like any manual run. Timezone handling is explicit (schedules pin their
zone), so a job set for 02:00 fires at 02:00 local.

---

## 15. Organizations, users, and roles

SLEP is **multi-tenant**. **Organizations** are the isolation boundary: projects,
inventories, credentials, runs, controllers, and schedules all belong to an org,
and users only see and act on the orgs they're in. Cross-org references are
refused — a pipeline in org A can't borrow org B's credential or aim at its
inventory.

**Roles** (per the simple CE model):

- **Superuser** — full administration, including users and orgs.
- **Operator** — author content and launch runs/pipelines in their org(s).
- **Viewer** — read-only: see projects, runs, and logs without launching.

Manage accounts under **Users**; assign org membership and role there. (EE will
add teams, SSO/OIDC, and finer RBAC — the CE model stays forward-compatible.)

---

## 16. Activity

**Activity** is the audit trail: connections, runs launched, enrollments,
schedule changes, and administrative actions, attributed to the acting user and
scoped to what you're allowed to see. Use it to answer "who ran that / who
enrolled those hosts / when did this change."

---

## 17. Troubleshooting

**Enrollment fails: "agent bundle download failed: … no agent-bundle route (HTTP
404)."**
The Controller you're pointed at doesn't serve `/remote/agent-bundle`. Either it's
an **older build** without agent (pull) enrollment (update it), or the connection
URL points at the **wrong service** — the console/portal port instead of the
Controller **backend API** (`:9000` by default). Reconnect the Controller at its
API address. If it's containerized, make sure the running image was **built from
current code** — a stale image serves stale routes.

**Enrollment fails: "certificate verify failed: self-signed certificate."**
Expected for a self-signed Controller — SLEP trusts it on first use and retries.
If it persists, the Controller may be **unreachable** at that address, or two
services are answering the port (see below).

**Enrollment 404 even though the route exists on disk.**
The running process is serving old code. Restart the Controller so it reloads,
and confirm nothing *else* owns its port — e.g. a leftover **container** publishing
`:9000` will intercept LAN-IP traffic while `127.0.0.1` reaches a different
process. One controller per port.

**`salt-ssh` errors** (`Bad stdio forwarding %h:%p`, `Connection closed … port
65535`, `No matching sls`). These are handled by SLEP's roster generation (concrete
`ProxyCommand`, quoted values, normalized SLS names). If you hit one, confirm the
project's jump host is set correctly and the target address resolves.

**Apply left orphaned libvirt objects** ("domain already exists"). The Terraform
runner self-heals stale domains/cloud-init ISOs from a partial apply and retries;
if it still fails it tells you exactly what to remove.

**A canceled apply removed more than expected.** Canceling an apply auto-runs
Destroy, which removes **everything** in that project's Terraform state — it's
cleanup-after-abandon, not a surgical rollback.

---

## 18. Reference

**Ports:** `8810` web console · `9100` backend API. (Sysible Controller uses
`9000`/`8800`/`8090`, so SLEP and a Controller coexist on one host.)

**Key environment variables** (see RUNNING.md for the full list):

| Var | Meaning |
|---|---|
| `SLEP_DATA_DIR` | DB, project workdirs, run logs |
| `SLEP_TLS_HOSTS` | Names/IPs to cover in the self-signed console cert |
| `SLEP_TLS=0` | Serve plain HTTP (front with a reverse proxy for real TLS) |
| `SLEP_CONTROLLER_CA` | Trust a specific Controller CA/cert (alternative to TOFU) |
| `SLEP_CONTROLLER_INSECURE=1` | Skip Controller TLS verification (LAN last resort only) |

**Management CLI:** `slep update | status | logs | restart | backup | …` — wraps
the Docker lifecycle so you don't memorize compose commands (see RUNNING.md).

**Where things live:** `backend/` (FastAPI API + runners + `controller_import`),
`webgui/` (BFF + React/Monaco console), `deploy/` (Dockerfile + compose + `slep`
CLI). The CE↔EE seam is `backend/db.py` — all persistence goes through its
function API.

---

*Questions or gaps? This guide tracks the Community Edition. For install and
day-2 operations, see [RUNNING.md](RUNNING.md).*
