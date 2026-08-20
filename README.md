# Sysible Linux Engineering Platform (SLEP)

**Author, orchestrate, and run your automation — Ansible, Terraform, and Salt —
from one self-hosted console, with a real in-browser IDE.**

SLEP is to infrastructure *automation* what Sysible Controller is to fleet
*administration*. Think **Ansible Automation Platform (AAP), but friendly** — the
same core model (projects, inventories, credentials, job templates, runs) without
the Kubernetes execution-environments, service mesh, and operator sprawl that make
AAP a project to stand up. One container, SQLite by default, SSH-native, and an
IDE built in.

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
