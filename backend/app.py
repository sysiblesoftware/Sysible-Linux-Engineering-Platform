"""SLEP backend API (FastAPI, :9100).

The single service the console talks to: admin auth, projects + their files (the
IDE reads/writes real files on disk), credentials, inventories (incl. import from
a Sysible Controller), and runs (launch an engine on a background thread and tail
its log). SQLite-backed, no external services — the "less of a hassle than AAP"
promise starts here.

Auth is intentionally lightweight for the MVP: a first-run setup creates the
admin, login returns an in-memory session token the console sends as a bearer.
Good enough to gate the API; hardening (durable sessions, lockout) mirrors
Controller later.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from . import controller_import, db
from .runners import ansible_runner

app = FastAPI(title="Sysible Linux Engineering Platform", version="0.1.0")

# In-memory sessions: token -> {"user", "created"}. Cleared on restart (MVP).
_SESSIONS: dict[str, dict] = {}
_SESSION_TTL = 12 * 3600


@app.on_event("startup")
def _startup():
    db.init_db()


# ------------------------------------------------------------------ auth utils
def _hash_password(password: str, salt: str | None = None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return h.hex(), salt


def _check_password(password: str, pw_hash: str, salt: str) -> bool:
    calc, _ = _hash_password(password, salt)
    return secrets.compare_digest(calc, pw_hash)


def _new_session(user: str) -> str:
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {"user": user, "created": time.time()}
    return token


def current_user(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    sess = _SESSIONS.get(token)
    if not sess or (time.time() - sess["created"]) > _SESSION_TTL:
        _SESSIONS.pop(token, None)
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return sess["user"]


# ------------------------------------------------------------------ health/setup
@app.get("/healthz")
def healthz():
    return {"status": "ok", "admins": db.count_admins()}


@app.post("/setup")
def setup(body: dict = Body(...)):
    """First-run: create the initial admin. Refused once an admin exists."""
    if db.count_admins() > 0:
        raise HTTPException(status_code=409, detail="Setup already complete.")
    user = str(body.get("username") or "").strip()
    pw = str(body.get("password") or "")
    if not user or len(pw) < 10:
        raise HTTPException(status_code=400, detail="Username and a 10+ char password required.")
    pw_hash, salt = _hash_password(pw)
    db.add_admin(user, pw_hash, salt, role="superuser")
    return {"status": "created", "username": user, "token": _new_session(user)}


@app.post("/login")
def login(body: dict = Body(...)):
    user = str(body.get("username") or "").strip()
    pw = str(body.get("password") or "")
    row = db.get_admin(user)
    if not row or not _check_password(pw, row["pw_hash"], row["salt"]):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    return {"status": "ok", "username": user, "token": _new_session(user),
            "must_change_password": bool(row.get("must_change_password"))}


@app.post("/logout")
def logout(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    _SESSIONS.pop(token, None)
    return {"status": "ok"}


@app.get("/me")
def me(user: str = Depends(current_user)):
    return {"username": user}


# ------------------------------------------------------------------ projects
def _slugify(name: str) -> str:
    s = "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s or "project"


@app.get("/projects")
def projects(user: str = Depends(current_user)):
    return {"projects": db.list_projects()}


@app.post("/projects")
def create_project(body: dict = Body(...), user: str = Depends(current_user)):
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name is required.")
    base = _slugify(name)
    slug, n = base, 1
    while any(p["slug"] == slug for p in db.list_projects()):
        n += 1
        slug = f"{base}-{n}"
    pid = db.create_project(name, slug, str(body.get("description") or ""),
                            str(body.get("scm_url") or ""), str(body.get("scm_branch") or ""))
    return db.get_project(pid)


@app.get("/projects/{pid}")
def get_project(pid: int, user: str = Depends(current_user)):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found.")
    return p


@app.delete("/projects/{pid}")
def delete_project(pid: int, user: str = Depends(current_user)):
    db.delete_project(pid)
    return {"status": "deleted"}


# ------------------------------------------------------------------ project files (IDE)
def _safe_path(pid: int, rel: str) -> Path:
    root = db.project_dir(pid).resolve()
    target = (root / (rel or "").lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=400, detail="Path escapes the project directory.")
    return target


def _tree(root: Path):
    """Return a sorted list of {path, type} relative to root (dirs first)."""
    out = []
    for p in sorted(root.rglob("*"), key=lambda x: (str(x.parent), not x.is_dir(), x.name)):
        if any(part in (".git", "__pycache__") for part in p.parts):
            continue
        out.append({"path": str(p.relative_to(root)), "type": "dir" if p.is_dir() else "file"})
    return out


@app.get("/projects/{pid}/files")
def list_files(pid: int, user: str = Depends(current_user)):
    if not db.get_project(pid):
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"files": _tree(db.project_dir(pid).resolve())}


@app.get("/projects/{pid}/file")
def read_file(pid: int, path: str = Query(...), user: str = Depends(current_user)):
    target = _safe_path(pid, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    try:
        return {"path": path, "content": target.read_text()}
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="Not a text file.")


@app.put("/projects/{pid}/file")
def write_file(pid: int, body: dict = Body(...), user: str = Depends(current_user)):
    path = str(body.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required.")
    target = _safe_path(pid, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.get("content") or "")
    db.touch_project(pid)
    return {"status": "saved", "path": path}


@app.post("/projects/{pid}/file")
def create_path(pid: int, body: dict = Body(...), user: str = Depends(current_user)):
    """Create an empty file or a directory (type=dir)."""
    path = str(body.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required.")
    target = _safe_path(pid, path)
    if body.get("type") == "dir":
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("")
    db.touch_project(pid)
    return {"status": "created", "path": path}


@app.delete("/projects/{pid}/file")
def delete_path(pid: int, path: str = Query(...), user: str = Depends(current_user)):
    import shutil
    target = _safe_path(pid, path)
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    elif target.exists():
        target.unlink()
    db.touch_project(pid)
    return {"status": "deleted", "path": path}


# ------------------------------------------------------------------ credentials
@app.get("/credentials")
def credentials(user: str = Depends(current_user)):
    return {"credentials": db.list_credentials()}


@app.post("/credentials")
def create_credential(body: dict = Body(...), user: str = Depends(current_user)):
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Credential name is required.")
    cid = db.create_credential(
        name, kind=str(body.get("kind") or "ssh"),
        username=str(body.get("username") or ""), secret=str(body.get("secret") or ""),
    )
    return db.get_credential(cid)


@app.delete("/credentials/{cid}")
def delete_credential(cid: int, user: str = Depends(current_user)):
    db.delete_credential(cid)
    return {"status": "deleted"}


# ------------------------------------------------------------------ inventories & hosts
@app.get("/inventories")
def inventories(project_id: int | None = None, user: str = Depends(current_user)):
    return {"inventories": db.list_inventories(project_id)}


@app.post("/inventories")
def create_inventory(body: dict = Body(...), user: str = Depends(current_user)):
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Inventory name is required.")
    iid = db.create_inventory(name, project_id=body.get("project_id"),
                              source=str(body.get("source") or "manual"))
    return db.get_inventory(iid)


@app.delete("/inventories/{iid}")
def delete_inventory(iid: int, user: str = Depends(current_user)):
    db.delete_inventory(iid)
    return {"status": "deleted"}


@app.get("/inventories/{iid}/hosts")
def inventory_hosts(iid: int, user: str = Depends(current_user)):
    return {"hosts": db.list_hosts(iid)}


@app.post("/inventories/{iid}/hosts")
def add_host(iid: int, body: dict = Body(...), user: str = Depends(current_user)):
    name = str(body.get("name") or "").strip()
    address = str(body.get("address") or "").strip() or name
    if not name:
        raise HTTPException(status_code=400, detail="Host name is required.")
    hid = db.add_host(iid, name, address, groups=str(body.get("groups") or ""),
                      variables=body.get("variables") or {})
    return {"status": "added", "id": hid}


@app.delete("/hosts/{hid}")
def delete_host(hid: int, user: str = Depends(current_user)):
    db.delete_host(hid)
    return {"status": "deleted"}


@app.post("/inventories/{iid}/import-controller")
def import_controller(iid: int, body: dict = Body(...), user: str = Depends(current_user)):
    """Pull hosts from a Sysible Controller into this inventory."""
    try:
        summary = controller_import.import_into_inventory(
            iid, str(body.get("controller_url") or ""), str(body.get("api_key") or ""),
        )
    except controller_import.ControllerImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", **summary}


# ------------------------------------------------------------------ runs
@app.post("/runs")
def launch_run(body: dict = Body(...), user: str = Depends(current_user)):
    pid = body.get("project_id")
    project = db.get_project(pid) if pid else None
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    kind = str(body.get("kind") or "ansible")
    if kind != "ansible":
        raise HTTPException(status_code=400, detail=f"Engine '{kind}' not wired yet (Ansible is first).")
    target = str(body.get("target") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="target (playbook path) is required.")
    run_id = db.create_run(
        pid, kind, target, inventory_id=body.get("inventory_id"),
        credential_id=body.get("credential_id"), extra_vars=body.get("extra_vars") or {},
        created_by=user,
    )
    # Launch on a background thread; the console tails the log.
    threading.Thread(target=ansible_runner.launch, args=(run_id,), daemon=True).start()
    return {"status": "launched", "run_id": run_id}


@app.get("/runs")
def runs(project_id: int | None = None, user: str = Depends(current_user)):
    return {"runs": db.list_runs(project_id)}


@app.get("/runs/{run_id}")
def get_run(run_id: int, user: str = Depends(current_user)):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Run not found.")
    return r


@app.get("/runs/{run_id}/log", response_class=PlainTextResponse)
def run_log(run_id: int, offset: int = 0, user: str = Depends(current_user)):
    """Return the run log from byte `offset` onward, so the console can poll-tail.
    The X-Log-Next header carries the next offset to request."""
    p = db.run_log_path(run_id)
    if not p.exists():
        return PlainTextResponse("", headers={"X-Log-Next": "0", "X-Run-Status": "pending"})
    data = p.read_bytes()
    chunk = data[offset:] if 0 <= offset <= len(data) else data
    run = db.get_run(run_id)
    return PlainTextResponse(
        chunk.decode("utf-8", "replace"),
        headers={"X-Log-Next": str(len(data)),
                 "X-Run-Status": (run or {}).get("status", "unknown")},
    )
