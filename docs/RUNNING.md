# Running SLEP (development)

SLEP is two small FastAPI services plus a static console. No node toolchain is
required for the MVP console (Monaco loads from a CDN; vendor it under
`webgui/static/vs` for airgapped installs).

## 1. Install

```sh
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
# The runner shells out to these — install whichever engines you'll use:
.venv/bin/pip install ansible-core          # Ansible (first-class today)
# terraform / salt: install per their vendors (Terraform via `sysible-tools` on
# Sysible Server; salt via apt/pip). Runners for these land next.
```

## 2. Run the two services

```sh
# Backend API (:9100) — owns the DB, project files, and runners.
.venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 9100 &

# Web console / BFF (:8810) — serves the UI and proxies /api to the backend.
.venv/bin/uvicorn webgui.server:app --host 0.0.0.0 --port 8810
```

Then open **http://localhost:8810** and create your admin on the first-run
screen.

Environment knobs:

| Var | Default | Meaning |
|---|---|---|
| `SLEP_DATA_DIR` | `./data` | DB, project workdirs, run logs |
| `SLEP_BACKEND_URL` | `http://127.0.0.1:9100` | where the BFF finds the backend |
| `SLEP_CONSOLE_PORT` | `8810` | BFF listen port |

## 3. First run, end to end

1. **Projects → New project.**
2. In the IDE, **+ File** → `site.yml`, write a playbook, **Save** (Ctrl/⌘-S).
3. **Inventories → New inventory**, then **+ Host** (or **Import from
   Controller** with a Controller URL + backend API key to pull your fleet).
4. **Credentials → New** — add the SSH key or password the run will use.
5. Back in the project, **▶ Run playbook**, pick the inventory + credential,
   **Launch**. The run log streams live and the status badge settles on
   `success`/`failed`.

## Note on the shared dev box

If `PYTHONPATH` is set to another project (e.g. the Controller repo), export
`PYTHONPATH=$(pwd)` before launching uvicorn so SLEP's `backend`/`webgui`
packages resolve instead of the other project's. A normal install (its own
venv/container) doesn't hit this.
