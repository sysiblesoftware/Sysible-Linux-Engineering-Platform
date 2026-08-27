# Sysible Linux Engineering Platform (SLEP)

**Author, orchestrate, and run your automation — Ansible, Terraform, and Salt —
from one self-hosted console, with a real in-browser IDE.**

SLEP is to infrastructure *automation* what Sysible Controller is to fleet
*administration*. Think **Ansible Automation Platform (AAP), but friendly** — the
same core model (projects, inventories, credentials, job templates, runs) without
the Kubernetes execution-environments, service mesh, and operator sprawl that make
AAP a project to stand up. One container, SQLite by default, SSH-native, and an
IDE built in.

<img width="765" height="441" alt="Screenshot 2026-08-27 at 2 30 57 PM" src="https://github.com/user-attachments/assets/747efd2c-3668-47b5-b4f7-cf7aa1841c74" />


## What it does

- **Projects** — a workspace of playbooks / Terraform configs / Salt states,
  either authored right in the browser IDE or synced from Git.
- **IDE** — a Monaco (VS Code) editor with a file tree, YAML/HCL/Jinja syntax,
  and a run panel. Write a playbook and run it without leaving the page.
- **Inventory** — hosts and groups, entered by hand or **imported from a Sysible
  Controller** (pull the fleet you already manage into your automation inventory).
- **Credentials** — SSH keys/passwords, cloud creds, vault tokens, stored server
  side and injected into runs, never surfaced to the browser.
- **Runners** — execute `ansible-playbook`, `terraform plan/apply`, and
  `salt`/`salt-ssh` in an isolated per-run workdir, streaming live output, with a
  full run history.

## Why it exists (vs. AAP)

| AAP | SLEP |
|---|---|
| Kubernetes + Execution Environments + Operator | One container (or a venv), SQLite by default |
| Automation mesh, receptors | Direct SSH (the way Ansible already works) |
| Ansible only | Ansible **+ Terraform + Salt** |
| Edit playbooks elsewhere, sync via SCM | **In-browser IDE**, SCM optional |
| Heavy RBAC/licensing | Simple admin model, self-hosted |

## Editions (CE today, EE later)

SLEP follows the same edition split as Sysible Controller. What exists here today
is effectively the **Community Edition (CE)**: single container, SQLite, SSH-native,
one admin model — everything a team needs to author and run automation.

**Enterprise Edition (EE)** will mirror `sysible-controller-ee` when we get to it —
a separate (private) repo that reuses this codebase's shape and adds the
enterprise concerns:

- **PostgreSQL-exclusive** datastore (the CE↔EE seam is `backend/db.py`: all
  persistence goes through its function API, so EE swaps the storage layer without
  touching the API surface or the runners).
- **Teams / RBAC** and SSO/SAML/OIDC login.
- **Approvals & scheduled/recurring jobs**, job-template surveys, and an
  audit trail.
- **HA / distributed execution** (multiple runner nodes) for large fleets.
- **Licensing** gate, same as Controller EE.

The current architecture keeps that path cheap: the backend/BFF split is already
the two-role seam Controller EE uses, and nothing in the console assumes SQLite.
We are **not** building EE now — this note just records the intent so CE stays
EE-ready.

## Architecture

Mirrors Sysible Controller so the two are operationally identical to run:

```
 Browser ──▶ Web console (BFF, :8810)  ──▶  Backend API (FastAPI, :9100)
                React SPA + Monaco IDE          projects · inventory · runs
                                                 └─▶ runners: ansible / terraform / salt
                                                 └─▶ SSH ──▶ your hosts
                                                 └─▶ import ──▶ Sysible Controller /agents
```

- `backend/` — FastAPI service (:9100). API for projects, files, inventory,
  credentials, runs. Owns the SQLite DB and the per-project workdirs.
  - `runners/` — one module per engine (`ansible_runner.py` first).
  - `controller_import.py` — pulls host inventory from a Sysible Controller.
- `webgui/` — the console: a FastAPI BFF (:8810) that serves the React SPA and
  proxies the backend, keeping the backend API key server-side (same split as
  Controller's console).
  - `frontend/` — React + Vite, Monaco editor.
- `deploy/` — Dockerfile + compose (single container), mirroring Controller.

## Status

Early MVP under active construction. First vertical slice: create a project,
edit a playbook in the IDE, import/enter inventory, run `ansible-playbook`, watch
the output stream. Terraform and Salt runners follow the same shape.

## Ports

`9100` backend · `8810` web console. (Controller uses 9000/8800/8090, so SLEP and
Controller coexist on one host.)
