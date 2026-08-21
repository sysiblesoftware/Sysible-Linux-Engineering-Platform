# Running SLEP

## Docker (recommended)

One container runs everything (both services + Ansible + Terraform baked in);
SQLite, project files, and run logs persist in the `slep-data` volume.

```sh
docker compose -f deploy/docker-compose.yml up -d --build
# open https://localhost:8810 and create your admin on the first-run screen
```

### Managing / updating

A small management CLI (`deploy/slep`) wraps the docker commands so you
don't have to remember them. Put it on your PATH once:

```sh
sudo ./deploy/slep install     # symlinks `slep` into /usr/local/bin
```

(Without sudo it installs into `~/.local/bin` for your user only.) After that,
run it from anywhere. To pull the latest code and rebuild + restart the container
in place (the `slep-data` volume is always preserved):

```sh
slep update
```

Other commands: `status` (container state + console health probe), `logs`,
`restart`, `start`, `stop`, `backup` (timestamped tarball of the data volume in
the current dir), `version`, and `uninstall`. Run `slep help` for the
full list. (Before installing, invoke it as `./deploy/slep <command>`.)

### HTTPS / TLS

The console is served over **HTTPS** by default with a **self-signed** certificate,
generated at first start and stored under the data volume (`data/tls/`). HTTPS is
what makes the browser treat the page as a *secure context* — required for the
Monaco IDE's **clipboard/paste** and for encrypting the console across the network.

- Your browser shows a one-time "not private" warning (expected for self-signed) —
  proceed once and it's remembered.
- Put your server's IP/hostname in the cert to drop the *name-mismatch* part of the
  warning: set `SLEP_TLS_HOSTS` (comma-separated), e.g.
  `SLEP_TLS_HOSTS=192.168.1.50,slep.lan docker compose … up -d`. Delete `data/tls/`
  to regenerate after changing it.
- **Warning-free (real) cert:** set `SLEP_TLS=0` (serve plain HTTP internally) and
  put a reverse proxy (Caddy/nginx/Traefik) in front that terminates TLS with a
  Let's Encrypt/your-CA certificate for your domain.
- Firewall the console port (8810) as needed; the backend stays on loopback inside
  the container and is never exposed.

Salt (`salt-ssh`) is optional and left out of the default image to keep it lean —
uncomment the `pip install salt` line in `deploy/Dockerfile` to bake it in.

---

# Development (without Docker)

SLEP is two small FastAPI services plus a static console. No node toolchain is
required for the MVP console (Monaco loads from a CDN; vendor it under
`webgui/static/vs` for airgapped installs).

## 1. Install

```sh
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
# The runner shells out to these — install whichever engines you'll use:
.venv/bin/pip install ansible-core          # Ansible (first-class today)
# System tools the runners shell out to (Docker image bakes these in already):
#   sudo apt install -y openssh-client sshpass    # sshpass = password-based SSH auth
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
