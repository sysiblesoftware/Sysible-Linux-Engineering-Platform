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

from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from . import controller_import, db, policy, vault
from .runners import ansible_runner, salt_runner, terraform_runner

# Engine name -> runner.launch(run_id). Each runs to completion on a thread.
RUNNERS = {
    "ansible": ansible_runner.launch,
    "terraform": terraform_runner.launch,
    "salt": salt_runner.launch,
}


@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    yield


app = FastAPI(title="Sysible Linux Engineering Platform", version="0.1.0", lifespan=lifespan)

_SESSION_TTL = 12 * 3600

# RBAC: viewer (read-only) < operator (author + run) < superuser (manage users).
ROLE_RANK = {"viewer": 1, "operator": 2, "superuser": 3}
ROLES = set(ROLE_RANK)

# Login throttle knobs (mirrors Controller): 10 failures in 15 min → 10-min lock,
# keyed per-username and stored durably so a restart can't clear a lockout.
_LOGIN_MAX_FAILURES = 10
_LOGIN_WINDOW_S = 15 * 60
_LOGIN_LOCKOUT_S = 10 * 60


# ------------------------------------------------------------------ auth utils
# PBKDF2-HMAC-SHA256. Hashes are stored as "<iters>$<hexdigest>" so the cost can
# rise over time and old hashes upgrade transparently on next login. A bare digest
# (no "$") is a legacy 200k hash.
_PBKDF2_ITERS = 600_000
_LEGACY_ITERS = 200_000


def _pbkdf2(password: str, salt: str, iters: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iters).hex()


def _hash_password(password: str, salt: str | None = None):
    salt = salt or secrets.token_hex(16)
    return f"{_PBKDF2_ITERS}${_pbkdf2(password, salt, _PBKDF2_ITERS)}", salt


def _parse_hash(stored: str):
    if "$" in stored:
        it, _, dig = stored.partition("$")
        try:
            return int(it), dig
        except ValueError:
            return _LEGACY_ITERS, stored
    return _LEGACY_ITERS, stored


def _check_password(password: str, pw_hash: str, salt: str) -> bool:
    iters, digest = _parse_hash(pw_hash)
    ok = secrets.compare_digest(_pbkdf2(password, salt, iters), digest)
    # Burn the shortfall so a legacy (200k) verify costs the same wall-time as a
    # current (600k) one — no timing signal distinguishes account cost tiers.
    if iters < _PBKDF2_ITERS:
        _pbkdf2(password, salt, _PBKDF2_ITERS - iters)
    return ok


def _needs_rehash(pw_hash: str) -> bool:
    iters, _ = _parse_hash(pw_hash)
    return iters < _PBKDF2_ITERS


# A throwaway decoy hash so a login for an UNKNOWN username costs the same PBKDF2
# time as a real one — closes the username-enumeration timing oracle.
_DECOY_HASH, _DECOY_SALT = _hash_password(secrets.token_hex(16))


def _new_session(user: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    db.create_admin_token(token, user, role, time.time() + _SESSION_TTL)
    return token


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def _session_or_401(request: Request) -> dict:
    sess = db.resolve_admin_token(_bearer(request))
    if not sess:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return {"user": sess["username"], "role": sess["role"]}


def current_user(request: Request) -> str:
    return _session_or_401(request)["user"]


def require_role(min_role: str):
    """Dependency: require at least `min_role`. Returns the acting username."""
    floor = ROLE_RANK[min_role]

    def dep(request: Request) -> str:
        s = _session_or_401(request)
        if ROLE_RANK.get(s.get("role", "viewer"), 1) < floor:
            raise HTTPException(status_code=403, detail=f"Requires the {min_role} role or higher.")
        return s["user"]
    return dep


require_operator = require_role("operator")
require_superuser = require_role("superuser")


# Blanket read-only guard for viewers: they may only issue GETs. Superuser/operator
# distinctions are enforced per-route with require_superuser above. Auth endpoints
# are exempt so a viewer can still log in/out.
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_AUTH_PATHS = {"/login", "/logout", "/setup"}


@app.middleware("http")
async def viewer_read_only(request: Request, call_next):
    if request.method in _WRITE_METHODS and request.url.path not in _AUTH_PATHS:
        s = db.resolve_admin_token(_bearer(request))
        if s and ROLE_RANK.get(s.get("role", "viewer"), 1) < ROLE_RANK["operator"]:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Your role is read-only (viewer)."}, status_code=403)
    return await call_next(request)


# Bound request bodies so an unbounded upload can't exhaust memory (the file-write
# routes accept arbitrary content). 16 MiB default, overridable.
_MAX_REQUEST_BYTES = int(os.environ.get("SLEP_MAX_REQUEST_BYTES", str(16 * 1024 * 1024)))


@app.middleware("http")
async def body_limit(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_REQUEST_BYTES:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Request body too large."}, status_code=413)
    return await call_next(request)


# Defense-in-depth response headers. The backend is a JSON API, so it can run the
# strictest CSP (default-src 'none'); the console BFF serves the SPA with its own.
@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    resp.headers.setdefault("Cache-Control", "no-store")
    if request.url.scheme == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    return resp


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
    if not user:
        raise HTTPException(status_code=400, detail="Username is required.")
    ok, msg = policy.validate_password(pw)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    pw_hash, salt = _hash_password(pw)
    db.add_admin(user, pw_hash, salt, role="superuser")
    db.log_audit("setup", user, "first administrator created")
    return {"status": "created", "username": user, "role": "superuser",
            "token": _new_session(user, "superuser")}


@app.post("/login")
def login(body: dict = Body(...)):
    user = str(body.get("username") or "").strip()
    pw = str(body.get("password") or "")
    throttle_key = user or "(empty)"

    locked = db.login_throttle_locked_for(throttle_key)
    if locked:
        db.log_audit("login_throttled", user, f"locked {locked}s")
        raise HTTPException(status_code=429,
                            detail=f"Too many failed attempts. Try again in about "
                                   f"{max(1, locked // 60)} minute(s).")

    row = db.get_admin(user)
    if row is not None:
        valid = _check_password(pw, row["pw_hash"], row["salt"])
    else:
        # Decoy verify so an unknown username costs the same as a real one.
        _check_password(pw, _DECOY_HASH, _DECOY_SALT)
        valid = False

    if not valid:
        db.login_throttle_record_failure(throttle_key, _LOGIN_WINDOW_S,
                                         _LOGIN_MAX_FAILURES, _LOGIN_LOCKOUT_S)
        db.log_audit("login_failed", user, "invalid username or password")
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    # Transparently upgrade a legacy/under-cost hash now that we hold the plaintext.
    if _needs_rehash(row["pw_hash"]):
        try:
            nh, ns = _hash_password(pw)
            db.set_admin_password(user, nh, ns, must_change=row.get("must_change_password", 0))
        except Exception:
            pass

    db.login_throttle_clear(throttle_key)
    role = row.get("role") or "operator"
    db.log_audit("login", user, f"role={role}")
    return {"status": "ok", "username": user, "role": role,
            "token": _new_session(user, role),
            "must_change_password": bool(row.get("must_change_password"))}


@app.post("/logout")
def logout(request: Request):
    db.delete_admin_token(_bearer(request))
    return {"status": "ok"}


@app.get("/me")
def me(request: Request):
    s = _session_or_401(request)
    return {"username": s["user"], "role": s.get("role", "operator")}


@app.get("/audit")
def audit(limit: int = 100, since_id: int = 0, user: str = Depends(require_superuser)):
    """Tamper-evident activity/audit feed (superuser). Newest first."""
    return {"entries": db.list_audit(limit=limit, since_id=since_id)}


@app.get("/audit/verify")
def audit_verify(user: str = Depends(require_superuser)):
    """Recompute the audit hash-chain and report whether it's intact."""
    return db.verify_audit_chain()


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


# ------------------------------------------------------------------ vault (secrets)
@app.get("/vault")
def vault_list(user: str = Depends(require_operator)):
    """Secret names only — values are encrypted and never returned."""
    return {"secrets": db.list_secrets()}


@app.post("/vault")
def vault_set(body: dict = Body(...), user: str = Depends(require_operator)):
    """Create or replace a secret. `name` is referenced from playbooks as
    {{ vault.NAME }}; the value is encrypted at rest and never returned."""
    name = str(body.get("name") or "").strip()
    value = body.get("value")
    if not name:
        raise HTTPException(status_code=400, detail="Secret name is required.")
    if value is None or value == "":
        raise HTTPException(status_code=400, detail="Secret value is required.")
    # Ansible var names: letters/digits/underscore, not starting with a digit.
    import re as _re
    if not _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise HTTPException(status_code=400,
                            detail="Name must be a valid variable: letters/digits/underscore, no leading digit.")
    sid = db.upsert_secret(name, vault.encrypt(str(value)))
    return {"status": "ok", "id": sid, "name": name}


@app.delete("/vault/{sid}")
def vault_delete(sid: int, user: str = Depends(require_operator)):
    db.delete_secret(sid)
    return {"status": "deleted"}


# ------------------------------------------------------------------ users (RBAC admin)
@app.get("/users")
def users_list(user: str = Depends(require_superuser)):
    return {"users": db.list_admins(), "roles": sorted(ROLES, key=lambda r: ROLE_RANK[r])}


@app.post("/users")
def users_create(body: dict = Body(...), acting: str = Depends(require_superuser)):
    name = str(body.get("username") or "").strip()
    pw = str(body.get("password") or "")
    role = str(body.get("role") or "operator")
    if not name:
        raise HTTPException(status_code=400, detail="Username is required.")
    if role not in ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of: {', '.join(ROLES)}.")
    ok, msg = policy.validate_password(pw)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    if db.get_admin(name):
        raise HTTPException(status_code=409, detail="That username already exists.")
    pw_hash, salt = _hash_password(pw)
    db.add_admin(name, pw_hash, salt, role=role)
    db.log_audit("user_created", acting, f"{name} (role={role})")
    return {"status": "created", "username": name, "role": role}


@app.patch("/users/{username}")
def users_update(username: str, body: dict = Body(...), acting: str = Depends(require_superuser)):
    """Change a user's role and/or reset their password. Guards against removing
    the last superuser."""
    row = db.get_admin(username)
    if not row:
        raise HTTPException(status_code=404, detail="User not found.")
    if "role" in body:
        role = str(body.get("role") or "")
        if role not in ROLES:
            raise HTTPException(status_code=400, detail=f"Role must be one of: {', '.join(ROLES)}.")
        if row.get("role") == "superuser" and role != "superuser" and db.count_superusers() <= 1:
            raise HTTPException(status_code=400, detail="Can't demote the last superuser.")
        if role != row.get("role"):
            db.set_admin_role(username, role)
            # Live sessions carry the old role — drop them so the change is immediate.
            db.delete_admin_tokens_for_user(username)
            db.log_audit("user_role_changed", acting, f"{username}: {row.get('role')} -> {role}")
    if body.get("password"):
        pw = str(body.get("password"))
        ok, msg = policy.validate_password(pw)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        pw_hash, salt = _hash_password(pw)
        db.set_admin_password(username, pw_hash, salt)
        # A password reset revokes existing sessions (forces re-auth).
        db.delete_admin_tokens_for_user(username)
        db.log_audit("user_password_reset", acting, username)
    r = db.get_admin(username)
    return {"status": "ok", "username": r["username"], "role": r.get("role")}


@app.delete("/users/{username}")
def users_delete(username: str, acting: str = Depends(require_superuser)):
    row = db.get_admin(username)
    if not row:
        raise HTTPException(status_code=404, detail="User not found.")
    if username == acting:
        raise HTTPException(status_code=400, detail="You can't delete your own account.")
    if row.get("role") == "superuser" and db.count_superusers() <= 1:
        raise HTTPException(status_code=400, detail="Can't delete the last superuser.")
    db.delete_admin(username)
    db.delete_admin_tokens_for_user(username)
    db.log_audit("user_deleted", acting, username)
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
                              source=str(body.get("source") or "manual"),
                              bastion=str(body.get("bastion") or "").strip())
    return db.get_inventory(iid)


@app.patch("/inventories/{iid}")
def update_inventory(iid: int, body: dict = Body(...), user: str = Depends(current_user)):
    """Update an inventory's SSH jump host (bastion). Empty string clears it."""
    if not db.get_inventory(iid):
        raise HTTPException(status_code=404, detail="Inventory not found.")
    if "bastion" in body:
        db.set_inventory_bastion(iid, str(body.get("bastion") or "").strip())
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
    """Pull hosts into this inventory from a Controller — either a saved one
    (controller_id) or an ad-hoc controller_url + api_key."""
    cid = body.get("controller_id")
    if cid:
        ctrl = db.get_controller(int(cid), include_key=True)
        if not ctrl:
            raise HTTPException(status_code=404, detail="Controller connection not found.")
        url, key = ctrl["base_url"], ctrl["api_key"]
    else:
        url, key = str(body.get("controller_url") or ""), str(body.get("api_key") or "")
    try:
        summary = controller_import.import_into_inventory(iid, url, key)
    except controller_import.ControllerImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if cid:
        db.set_controller_last_import(int(cid))
    return {"status": "ok", **summary}


# ------------------------------------------------------------------ controllers (Connect to Controller)
@app.get("/controllers")
def controllers(user: str = Depends(current_user)):
    return {"controllers": db.list_controllers()}


@app.post("/controllers")
def connect_controller(body: dict = Body(...), user: str = Depends(require_superuser)):
    """Connect to a Sysible Controller and save it. Two ways to authenticate:

      * username + password — a Controller *superuser* signs in with the same
        credentials they use for the Controller console; SLEP exchanges them for
        the backend API key behind the scenes (POST /auth/api-key). The friendly
        default — no key-hunting.
      * api_key — the raw backend key, for older Controllers or headless setups.

    Either way the resolved key is stored server-side and never returned."""
    name = str(body.get("name") or "").strip()
    url = str(body.get("base_url") or body.get("controller_url") or "").strip()
    key = str(body.get("api_key") or "")
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    totp_code = str(body.get("totp_code") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Controller URL is required.")

    # Username/password path: exchange console creds for the API key first.
    if not key and (username or password):
        try:
            key = controller_import.exchange_credentials_for_key(url, username, password, totp_code)
        except controller_import.ControllerMFARequired as e:
            # Not an error for the SLEP session — tell the UI to collect a second
            # factor and resubmit. (A 401 here would trip api.js's auto-logout.)
            return {"status": "mfa_required", "detail": str(e)}
        except controller_import.ControllerImportError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if not key:
        raise HTTPException(status_code=400,
                            detail="Provide a Controller superuser username + password, or its backend API key.")
    try:
        probe = controller_import.test_connection(url, key)   # fails closed on a bad key/URL
    except controller_import.ControllerImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    cid = db.create_controller(name or url, url, key)
    return {"status": "connected", "controller": db.get_controller(cid), **probe}


@app.post("/controllers/{cid}/test")
def test_controller(cid: int, user: str = Depends(require_superuser)):
    ctrl = db.get_controller(cid, include_key=True)
    if not ctrl:
        raise HTTPException(status_code=404, detail="Controller connection not found.")
    try:
        return {"status": "ok", **controller_import.test_connection(ctrl["base_url"], ctrl["api_key"])}
    except controller_import.ControllerImportError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/controllers/{cid}")
def disconnect_controller(cid: int, user: str = Depends(require_superuser)):
    db.delete_controller(cid)
    return {"status": "disconnected"}


# ------------------------------------------------------------------ runs
@app.post("/runs")
def launch_run(body: dict = Body(...), user: str = Depends(current_user)):
    pid = body.get("project_id")
    project = db.get_project(pid) if pid else None
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    kind = str(body.get("kind") or "ansible")
    if kind not in RUNNERS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown engine '{kind}'. One of: {', '.join(RUNNERS)}.")
    target = str(body.get("target") or "").strip()
    if not target:
        # ansible: playbook path · terraform: plan/apply/destroy · salt: state name
        what = {"ansible": "playbook path", "terraform": "action (plan/apply/destroy)",
                "salt": "state name"}.get(kind, "target")
        raise HTTPException(status_code=400, detail=f"target ({what}) is required.")
    run_id = db.create_run(
        pid, kind, target, inventory_id=body.get("inventory_id"),
        credential_id=body.get("credential_id"), extra_vars=body.get("extra_vars") or {},
        created_by=user,
    )
    db.log_audit("run_launched", user, f"#{run_id} {kind} '{target}' on {project['name']}")
    # Launch the right engine on a background thread; the console tails the log.
    threading.Thread(target=RUNNERS[kind], args=(run_id,), daemon=True).start()
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
