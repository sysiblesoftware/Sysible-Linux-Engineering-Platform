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
import json
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from . import controller_import, db, engines, gitops, infra, keydist, policy, projcfg, vault
from .runners import ansible_runner, salt_runner, terraform_runner

# Engine name -> runner.launch(run_id). Each runs to completion on a thread.
RUNNERS = {
    "ansible": ansible_runner.launch,
    "terraform": terraform_runner.launch,
    "salt": salt_runner.launch,
}


_scheduler_stop = threading.Event()


def _scheduler_loop():
    """Fire due schedules. Polls every 30s; each due schedule launches its saved
    run and advances to the next slot. Resilient — one bad schedule never stops
    the loop. Runs in the backend process (where the runners live)."""
    while not _scheduler_stop.wait(30):
        try:
            for s in db.due_schedules():
                try:
                    project = db.get_project(s["project_id"])
                    if not project:
                        db.delete_schedule(s["id"])
                        continue
                    actor = f"schedule:{s.get('name') or s['id']}"
                    if s["kind"] == "pipeline":
                        # target holds the saved pipeline id — fire the whole sequence.
                        pl = db.get_pipeline(int(s["target"]))
                        if not pl:
                            db.mark_schedule_fired(s["id"], 0, status="error")
                            continue
                        run_ids, _gid = _dispatch_pipeline(project, pl["steps"], actor,
                                                           stop_on_failure=pl["stop_on_failure"])
                        run_id = run_ids[0] if run_ids else 0
                    else:
                        run_id = _dispatch_run(project, s["kind"], s["target"], s.get("inventory_id"),
                                               s.get("credential_id"), s.get("extra_vars") or {}, actor)
                    db.mark_schedule_fired(s["id"], run_id)
                    db.log_audit("schedule_fired", "system",
                                 f"schedule '{s.get('name') or s['id']}' → run #{run_id}")
                except Exception:  # noqa: BLE001 — never let one schedule stall the loop
                    db.mark_schedule_fired(s["id"], 0, status="error")
        except Exception:  # noqa: BLE001
            pass


@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    engines.ensure_path()   # pick up any previously one-click-installed engines
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    yield
    _scheduler_stop.set()


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

# Per-org RBAC. Org roles mirror the global tiers, ranked so the highest wins.
_ORG_RANK = {"viewer": 1, "operator": 2, "admin": 3}


def _is_system_admin(role: str) -> bool:
    """The global superuser is the system administrator: full access to every
    org, and the only role that manages orgs, teams and users."""
    return role == "superuser"


def _visible_org_ids(request: Request):
    """Org ids the caller may see, or None for a system admin (meaning all)."""
    s = _session_or_401(request)
    if _is_system_admin(s["role"]):
        return None
    return db.orgs_for_user(s["user"])


def _effective_org_role(request: Request, org_id: int):
    """The caller's effective role in an org: 'admin' for a system admin,
    otherwise their combined membership/team role, or None if no access."""
    s = _session_or_401(request)
    if _is_system_admin(s["role"]):
        return "admin"
    return db.effective_org_role(s["user"], org_id)


def _require_org(request: Request, org_id, min_role: str = "operator") -> str:
    """Ensure the caller has at least `min_role` in the org — 404 if the org is
    unknown, 403 if they lack the role. Returns the acting username."""
    if not org_id or not db.get_org(org_id):
        raise HTTPException(status_code=404, detail="Organization not found.")
    role = _effective_org_role(request, org_id)
    if not role or _ORG_RANK[role] < _ORG_RANK[min_role]:
        raise HTTPException(status_code=403,
                            detail=f"Requires the {min_role} role in this organization.")
    return _session_or_401(request)["user"]


def _mask_extra_vars(row, role):
    """A run row's extra_vars may hold a secret an operator typed into the Variables
    box. Viewers see the var KEYS but masked VALUES; operator+ see them in full.
    (The designated secret channel is the vault — {{ vault.NAME }} — not extra_vars.)"""
    if not row or ROLE_RANK.get(role, 1) >= ROLE_RANK["operator"]:
        return row
    r = dict(row)
    try:
        ev = json.loads(r.get("extra_vars") or "{}")
        if isinstance(ev, dict) and ev:
            r["extra_vars"] = json.dumps({k: "***" for k in ev})
    except (TypeError, ValueError):
        r["extra_vars"] = "{}"
    return r


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
    sysadmin = _is_system_admin(s["role"])
    if sysadmin:
        orgs = [{**o, "role": "admin"} for o in db.list_orgs()]
    else:
        orgs = []
        for oid in db.orgs_for_user(s["user"]):
            o = db.get_org(oid)
            if o:
                orgs.append({**o, "role": db.effective_org_role(s["user"], oid)})
    return {"username": s["user"], "role": s.get("role", "operator"),
            "system_admin": sysadmin, "organizations": orgs}


# ---------------------------------------------------------------- Organizations
def _valid_org_role(r: str) -> str:
    if r not in ("admin", "operator", "viewer"):
        raise HTTPException(status_code=422, detail="role must be admin, operator or viewer")
    return r


@app.get("/organizations")
def organizations(request: Request):
    """Orgs the caller can see (all for a system admin), each with their role."""
    s = _session_or_401(request)
    sysadmin = _is_system_admin(s["role"])
    out = []
    for o in db.list_orgs():
        if sysadmin:
            role = "admin"
        else:
            role = db.effective_org_role(s["user"], o["id"])
            if not role:
                continue
        out.append({**o, "role": role,
                    "members": len(db.list_org_members(o["id"])),
                    "teams": len(db.list_teams(o["id"]))})
    return {"organizations": out}


@app.post("/organizations")
def create_organization(request: Request, body: dict = Body(...),
                        user: str = Depends(require_superuser)):
    """Create an organization (system admin only)."""
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    slug = _slugify(str(body.get("slug") or name))
    if db.get_org_by_slug(slug):
        raise HTTPException(status_code=409, detail="An organization with that slug already exists.")
    oid = db.create_org(name, slug, str(body.get("description") or ""))
    db.log_audit("org_created", user, f"{name} (#{oid})")
    return {"id": oid, "name": name, "slug": slug}


@app.get("/organizations/{oid}")
def organization(oid: int, request: Request):
    role = _effective_org_role(request, oid)
    o = db.get_org(oid)
    if not o or not role:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return {**o, "role": role}


@app.put("/organizations/{oid}")
def update_organization(oid: int, request: Request, body: dict = Body(...)):
    user = _require_org(request, oid, "admin")
    db.update_org(oid, name=body.get("name"), description=body.get("description"))
    db.log_audit("org_updated", user, f"#{oid}")
    return {"ok": True}


@app.delete("/organizations/{oid}")
def delete_organization(oid: int, request: Request, user: str = Depends(require_superuser)):
    o = db.get_org(oid)
    if not o:
        raise HTTPException(status_code=404, detail="Organization not found.")
    if o["slug"] == "default":
        raise HTTPException(status_code=400, detail="The Default organization can't be deleted.")
    db.delete_org(oid)
    db.log_audit("org_deleted", user, f"{o['name']} (#{oid})")
    return {"ok": True}


@app.get("/organizations/{oid}/members")
def org_members(oid: int, request: Request):
    _require_org(request, oid, "viewer")
    return {"members": db.list_org_members(oid)}


@app.post("/organizations/{oid}/members")
def set_org_member(oid: int, request: Request, body: dict = Body(...)):
    actor = _require_org(request, oid, "admin")
    username = str(body.get("username") or "").strip()
    if not username or not db.get_admin(username):
        raise HTTPException(status_code=404, detail="No such user.")
    role = _valid_org_role(str(body.get("role") or "viewer"))
    db.set_org_member(oid, username, role)
    db.log_audit("org_member_set", actor, f"{username}={role} in #{oid}")
    return {"ok": True}


@app.delete("/organizations/{oid}/members/{username}")
def remove_org_member(oid: int, username: str, request: Request):
    actor = _require_org(request, oid, "admin")
    db.remove_org_member(oid, username)
    db.log_audit("org_member_removed", actor, f"{username} from #{oid}")
    return {"ok": True}


# ---------------------------------------------------------------- Teams
@app.get("/organizations/{oid}/teams")
def org_teams(oid: int, request: Request):
    _require_org(request, oid, "viewer")
    return {"teams": [{**t, "members": len(db.list_team_members(t["id"]))}
                      for t in db.list_teams(oid)]}


@app.post("/organizations/{oid}/teams")
def create_org_team(oid: int, request: Request, body: dict = Body(...)):
    actor = _require_org(request, oid, "admin")
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    role = _valid_org_role(str(body.get("org_role") or "viewer"))
    tid = db.create_team(oid, name, role)
    db.log_audit("team_created", actor, f"{name} ({role}) in #{oid}")
    return {"id": tid, "name": name, "org_role": role}


@app.put("/teams/{tid}")
def update_team_route(tid: int, request: Request, body: dict = Body(...)):
    t = db.get_team(tid)
    if not t:
        raise HTTPException(status_code=404, detail="Team not found.")
    actor = _require_org(request, t["org_id"], "admin")
    role = body.get("org_role")
    if role is not None:
        _valid_org_role(role)
    db.update_team(tid, name=body.get("name"), org_role=role)
    db.log_audit("team_updated", actor, f"#{tid}")
    return {"ok": True}


@app.delete("/teams/{tid}")
def delete_team_route(tid: int, request: Request):
    t = db.get_team(tid)
    if not t:
        raise HTTPException(status_code=404, detail="Team not found.")
    actor = _require_org(request, t["org_id"], "admin")
    db.delete_team(tid)
    db.log_audit("team_deleted", actor, f"{t['name']} (#{tid})")
    return {"ok": True}


@app.get("/teams/{tid}/members")
def team_members(tid: int, request: Request):
    t = db.get_team(tid)
    if not t:
        raise HTTPException(status_code=404, detail="Team not found.")
    _require_org(request, t["org_id"], "viewer")
    return {"members": db.list_team_members(tid)}


@app.post("/teams/{tid}/members")
def add_team_member_route(tid: int, request: Request, body: dict = Body(...)):
    t = db.get_team(tid)
    if not t:
        raise HTTPException(status_code=404, detail="Team not found.")
    actor = _require_org(request, t["org_id"], "admin")
    username = str(body.get("username") or "").strip()
    if not username or not db.get_admin(username):
        raise HTTPException(status_code=404, detail="No such user.")
    db.add_team_member(tid, username)
    db.log_audit("team_member_added", actor, f"{username} to team #{tid}")
    return {"ok": True}


@app.delete("/teams/{tid}/members/{username}")
def remove_team_member_route(tid: int, username: str, request: Request):
    t = db.get_team(tid)
    if not t:
        raise HTTPException(status_code=404, detail="Team not found.")
    actor = _require_org(request, t["org_id"], "admin")
    db.remove_team_member(tid, username)
    db.log_audit("team_member_removed", actor, f"{username} from team #{tid}")
    return {"ok": True}


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
def projects(request: Request, user: str = Depends(current_user)):
    return {"projects": db.list_projects(_visible_org_ids(request))}


@app.post("/projects")
def create_project(request: Request, body: dict = Body(...), user: str = Depends(current_user)):
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name is required.")
    # A sub-project inherits its parent's org; a top-level project takes org_id.
    parent_id = body.get("parent_id")
    if parent_id:
        parent = db.get_project(parent_id)
        if not parent:
            raise HTTPException(status_code=404, detail="Parent project not found.")
        org_id = parent.get("org_id") or db.default_org_id()
    else:
        org_id = body.get("org_id") or db.default_org_id()
    _require_org(request, org_id, "operator")
    base = _slugify(name)
    slug, n = base, 1
    while any(p["slug"] == slug for p in db.list_projects()):   # slug is globally unique
        n += 1
        slug = f"{base}-{n}"
    pid = db.create_project(name, slug, str(body.get("description") or ""),
                            str(body.get("scm_url") or ""), str(body.get("scm_branch") or ""),
                            org_id=org_id, parent_id=parent_id or None)
    # Optionally seed the project by cloning a git repo into its (empty) workdir.
    clone_url = str(body.get("clone_url") or "").strip()
    if clone_url:
        try:
            gitops.clone(pid, clone_url, str(body.get("git_token") or ""))
        except gitops.GitError as e:
            db.delete_project(pid)
            import shutil
            shutil.rmtree(db.project_dir(pid), ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Clone failed: {e}")
        db.log_audit("project_cloned", user, f"{name} ← {clone_url}")
    return db.get_project(pid)


@app.get("/projects/{pid}")
def get_project(pid: int, user: str = Depends(current_user)):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found.")
    return p


@app.patch("/projects/{pid}")
def move_project(pid: int, request: Request, body: dict = Body(...), user: str = Depends(current_user)):
    """Re-parent a project (organize it into a folder / sub-project tree).
    parent_id null makes it top-level. The new parent must be in the same org."""
    p = db.get_project(pid)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found.")
    _require_org(request, p.get("org_id") or db.default_org_id(), "operator")
    if "parent_id" in body:
        new_parent = body.get("parent_id")
        if new_parent:
            parent = db.get_project(new_parent)
            if not parent:
                raise HTTPException(status_code=404, detail="Parent project not found.")
            if (parent.get("org_id") or db.default_org_id()) != (p.get("org_id") or db.default_org_id()):
                raise HTTPException(status_code=400, detail="A project can only nest under one in the same organization.")
        try:
            db.set_project_parent(pid, new_parent or None)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.log_audit("project_moved", user, f"#{pid} → parent {new_parent or 'top-level'}")
    return db.get_project(pid)


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


# ------------------------------------------------------------------ ansible.cfg
# A project's configuration is a real ansible.cfg on disk (git-committable, read
# by ansible-playbook at run time). These give the console a first-class handle
# on it — create from a SLEP-tuned template, read, and validate-on-save.
@app.get("/projects/{pid}/config")
def get_config(pid: int, user: str = Depends(current_user)):
    if not db.get_project(pid):
        raise HTTPException(status_code=404, detail="Project not found.")
    return projcfg.read(pid)


@app.post("/projects/{pid}/config/default")
def create_default_config(pid: int, user: str = Depends(current_user)):
    """Create ansible.cfg from the starter template if it doesn't exist yet."""
    if not db.get_project(pid):
        raise HTTPException(status_code=404, detail="Project not found.")
    db.log_audit("config_default", user, f"project:{pid}")
    return projcfg.ensure_default(pid)


@app.put("/projects/{pid}/config")
def put_config(pid: int, body: dict = Body(...), user: str = Depends(current_user)):
    if not db.get_project(pid):
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        return projcfg.write(pid, str(body.get("content") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid ansible.cfg: {e}")


# ------------------------------------------------------------------ git ops
def _git(fn):
    try:
        return fn()
    except gitops.GitError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/projects/{pid}/git/status")
def git_status(pid: int, user: str = Depends(current_user)):
    if not db.get_project(pid):
        raise HTTPException(status_code=404, detail="Project not found.")
    return _git(lambda: gitops.status(pid))


@app.post("/projects/{pid}/git/init")
def git_init(pid: int, user: str = Depends(current_user)):
    db.log_audit("git_init", user, f"project:{pid}")
    return _git(lambda: gitops.init(pid))


@app.post("/projects/{pid}/git/commit")
def git_commit(pid: int, body: dict = Body(...), user: str = Depends(current_user)):
    paths = body.get("paths")
    return _git(lambda: gitops.commit(pid, str(body.get("message") or ""),
                                      paths=list(paths) if isinstance(paths, list) else None))


@app.get("/projects/{pid}/git/log")
def git_log(pid: int, user: str = Depends(current_user)):
    return {"commits": _git(lambda: gitops.log(pid))}


@app.get("/projects/{pid}/git/diff", response_class=PlainTextResponse)
def git_diff(pid: int, path: str | None = None, staged: bool = False, user: str = Depends(current_user)):
    return PlainTextResponse(_git(lambda: gitops.diff(pid, path=path, staged=staged)))


@app.get("/projects/{pid}/git/branches")
def git_branches(pid: int, user: str = Depends(current_user)):
    return {"branches": _git(lambda: gitops.branches(pid))}


@app.post("/projects/{pid}/git/checkout")
def git_checkout(pid: int, body: dict = Body(...), user: str = Depends(current_user)):
    return _git(lambda: gitops.checkout(pid, str(body.get("branch") or ""), create=bool(body.get("create"))))


@app.post("/projects/{pid}/git/remote")
def git_remote(pid: int, body: dict = Body(...), user: str = Depends(current_user)):
    """Set the remote URL and (optionally) an encrypted push/pull token."""
    if not db.get_project(pid):
        raise HTTPException(status_code=404, detail="Project not found.")
    if "url" in body:
        gitops.set_remote(pid, str(body.get("url") or ""))
    if "token" in body:
        tok = str(body.get("token") or "")
        db.set_project_scm(pid, git_token=vault.encrypt(tok) if tok else "")
    return _git(lambda: gitops.status(pid))


@app.post("/projects/{pid}/git/push")
def git_push(pid: int, user: str = Depends(current_user)):
    db.log_audit("git_push", user, f"project:{pid}")
    return _git(lambda: gitops.push(pid))


@app.post("/projects/{pid}/git/pull")
def git_pull(pid: int, user: str = Depends(current_user)):
    db.log_audit("git_pull", user, f"project:{pid}")
    return _git(lambda: gitops.pull(pid))


@app.post("/projects/{pid}/file/rename")
def rename_path(pid: int, body: dict = Body(...), user: str = Depends(current_user)):
    """Rename/move a file or directory within the project (both paths are
    confined to the project dir by _safe_path)."""
    src = _safe_path(pid, str(body.get("from") or "").strip())
    dst = _safe_path(pid, str(body.get("to") or "").strip())
    if not str(body.get("to") or "").strip():
        raise HTTPException(status_code=400, detail="A new name is required.")
    if not src.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    if dst.exists():
        raise HTTPException(status_code=409, detail="A file with that name already exists.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    db.touch_project(pid)
    return {"status": "renamed", "path": str(body.get("to")).strip()}


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
def credentials(request: Request, user: str = Depends(current_user)):
    return {"credentials": db.list_credentials(org_ids=_visible_org_ids(request))}


@app.post("/credentials")
def create_credential(request: Request, body: dict = Body(...), user: str = Depends(current_user)):
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Credential name is required.")
    org_id = body.get("org_id") or db.default_org_id()
    _require_org(request, org_id, "operator")
    # The SSH key / cloud secret and the sudo/become password are encrypted at rest
    # by the db layer — pass them as plaintext.
    cid = db.create_credential(
        name, kind=str(body.get("kind") or "ssh"),
        username=str(body.get("username") or ""), secret=str(body.get("secret") or ""),
        become_secret=str(body.get("become_password") or ""), org_id=org_id,
    )
    return db.get_credential(cid)


@app.patch("/credentials/{cid}")
def update_credential(cid: int, body: dict = Body(...), user: str = Depends(current_user)):
    """Set or clear a credential's sudo/become password (encrypted at rest). Lets
    the auto-created 'SLEP managed key' carry the sudo password for `become` tasks."""
    if not db.get_credential(cid):
        raise HTTPException(status_code=404, detail="Credential not found.")
    if "become_password" in body:
        db.set_credential_become(cid, str(body.get("become_password") or ""))   # encrypted at rest by db
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
    # Enroll the new user in the Default org with the org-role matching their
    # system role, so a fresh operator/viewer can work immediately (a superuser
    # is a system admin and sees every org regardless).
    org_role = "admin" if role == "superuser" else ("viewer" if role == "viewer" else "operator")
    db.set_org_member(db.default_org_id(), name, org_role)
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
def inventories(request: Request, project_id: int | None = None, user: str = Depends(current_user)):
    return {"inventories": db.list_inventories(project_id, org_ids=_visible_org_ids(request))}


def _validate_bastion(bastion: str) -> str:
    """A jump host is `[user@]host[:port]`. When the host part looks like a dotted
    IPv4, make sure every octet is 0–255 — a typo like 192.268.8.212 otherwise sails
    through and every play fails UNREACHABLE with a cryptic SSH error at run time."""
    import ipaddress
    import re as _re
    b = (bastion or "").strip()
    if not b:
        return ""
    host = b.split("@", 1)[1] if "@" in b else b
    host = host.rsplit(":", 1)[0] if _re.fullmatch(r".+:\d+", host) else host
    if _re.fullmatch(r"\d+(\.\d+){3}", host):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"“{host}” is not a valid IP address — each part must be 0–255. Check the jump host.")
    return b


@app.post("/inventories")
def create_inventory(request: Request, body: dict = Body(...), user: str = Depends(current_user)):
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Inventory name is required.")
    # Inherit the project's org when attached, else the body's org_id / Default.
    pid = body.get("project_id")
    if pid:
        proj = db.get_project(pid)
        if not proj:
            raise HTTPException(status_code=404, detail="Project not found.")
        org_id = proj.get("org_id") or db.default_org_id()
    else:
        org_id = body.get("org_id") or db.default_org_id()
    _require_org(request, org_id, "operator")
    # Get-or-create within a project: an inventory with this exact name already
    # there is returned as-is, so a double-submit (or re-adding the same name)
    # can't produce two identical inventories.
    if pid:
        dup = db.find_inventory_by_name(pid, name)
        if dup:
            return db.get_inventory(dup["id"])
    iid = db.create_inventory(name, project_id=pid,
                              source=str(body.get("source") or "manual"),
                              bastion=_validate_bastion(str(body.get("bastion") or "")),
                              org_id=org_id)
    return db.get_inventory(iid)


@app.patch("/inventories/{iid}")
def update_inventory(iid: int, body: dict = Body(...), user: str = Depends(current_user)):
    """Update an inventory's SSH jump host (bastion). Empty string clears it."""
    if not db.get_inventory(iid):
        raise HTTPException(status_code=404, detail="Inventory not found.")
    if "bastion" in body:
        db.set_inventory_bastion(iid, _validate_bastion(str(body.get("bastion") or "")))
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


@app.get("/controllers/{cid}/hosts")
def controller_hosts(cid: int, user: str = Depends(current_user)):
    """List a connected Controller's hosts (agents + SSH) WITHOUT importing — the
    console shows this so the operator can pick which hosts go to which inventory."""
    ctrl = db.get_controller(cid, include_key=True)
    if not ctrl:
        raise HTTPException(status_code=404, detail="Controller connection not found.")
    try:
        return controller_import.fetch_hosts(ctrl["base_url"], ctrl["api_key"])
    except controller_import.ControllerImportError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/inventories/{iid}/import-controller")
def import_controller(iid: int, body: dict = Body(...), user: str = Depends(current_user)):
    """Import hosts into this inventory from a Controller — either a saved one
    (controller_id) or an ad-hoc controller_url + api_key. Pass `host_names` (a
    list) to import only that selection; omit it to import everything."""
    cid = body.get("controller_id")
    if cid:
        ctrl = db.get_controller(int(cid), include_key=True)
        if not ctrl:
            raise HTTPException(status_code=404, detail="Controller connection not found.")
        url, key = ctrl["base_url"], ctrl["api_key"]
    else:
        url, key = str(body.get("controller_url") or ""), str(body.get("api_key") or "")
    names = body.get("host_names")
    only = list(names) if isinstance(names, list) else None
    try:
        summary = controller_import.import_into_inventory(iid, url, key, only_names=only)
    except controller_import.ControllerImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if cid:
        db.set_controller_last_import(int(cid))
    return {"status": "ok", **summary}


# ------------------------------------------------------------------ controllers (Connect to Controller)
@app.get("/controllers")
def controllers(request: Request, user: str = Depends(current_user)):
    return {"controllers": db.list_controllers(org_ids=_visible_org_ids(request))}


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
    cid = db.create_controller(name or url, url, key,
                               org_id=body.get("org_id") or db.default_org_id())
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
def _dispatch_run(project, kind, target, inventory_id, credential_id, extra_vars, actor,
                  become_password="", limit="", start_at_task="", tf_tool=""):
    """Create a run row and launch its engine on a background thread. Shared by the
    manual /runs route and the scheduler. Returns the run id. `become_password` is
    a transient per-run sudo password — stashed in memory, never persisted.
    `tf_tool` picks Terraform vs OpenTofu ('terraform' | 'tofu') for terraform runs."""
    run_id = db.create_run(
        project["id"], kind, target, inventory_id=inventory_id,
        credential_id=credential_id, extra_vars=extra_vars or {}, created_by=actor,
    )
    if kind == "ansible":
        from .runners import ansible_runner
        if become_password:
            ansible_runner.stash_become(run_id, become_password)
        if limit or start_at_task:
            ansible_runner.stash_opts(run_id, {"limit": limit, "start_at_task": start_at_task})
    if kind == "terraform" and tf_tool:
        terraform_runner.stash_tool(run_id, tf_tool)
    db.log_audit("run_launched", actor, f"#{run_id} {kind} '{target}' on {project['name']}")
    threading.Thread(target=RUNNERS[kind], args=(run_id,), daemon=True).start()
    return run_id


# Pipeline steps are the real engines, plus the "inventory" pseudo-step: it has no
# runner of its own — the worker reads the just-applied VMs into the project's
# inventory and points the following Ansible/Salt steps at it.
def _validate_steps(steps):
    if not steps:
        raise HTTPException(status_code=400, detail="A pipeline needs at least one step.")
    for i, s in enumerate(steps, 1):
        kind = s.get("kind")
        if kind not in RUNNERS and kind != "inventory":
            raise HTTPException(status_code=400, detail=f"Step {i}: unknown engine '{kind}'.")
        if kind != "inventory" and not str(s.get("target") or "").strip():
            raise HTTPException(status_code=400, detail=f"Step {i}: a target is required.")


# Saved pipelines are stored verbatim (steps JSON) and readable by ANY authenticated
# user, viewers included, via GET /pipelines — so a saved step must never carry a
# secret. Strip the per-step sudo/become password before persisting: a saved
# pipeline draws its become password from the attached credential (encrypted at
# rest) at run time. Ad-hoc /pipelines/run still passes it transiently (in-memory,
# never stored).
def _strip_saved_pipeline_secrets(steps):
    return [{k: v for k, v in (s or {}).items() if k != "become_password"} for s in (steps or [])]


def _dispatch_pipeline(project, steps, actor, stop_on_failure=True):
    """Run an ordered list of steps in succession. Every step becomes a normal run
    row (queued up front so the whole sequence is visible immediately), sharing one
    group_id so the visualizer can show the sequence. They execute one after another
    on a single background thread; if a step doesn't succeed and stop_on_failure is
    set, the remaining queued steps are canceled. Returns (run_ids, group_id)."""
    from .runners import ansible_runner
    group_id = secrets.token_hex(8)
    prepared = []
    for s in steps:
        kind = s.get("kind")
        target = str(s.get("target") or "").strip() or ("from VMs" if kind == "inventory" else "")
        rid = db.create_run(
            project["id"], kind, target,
            inventory_id=s.get("inventory_id"), credential_id=s.get("credential_id"),
            extra_vars=s.get("extra_vars") or {}, created_by=actor, group_id=group_id,
        )
        if kind == "ansible":
            if s.get("become_password"):
                ansible_runner.stash_become(rid, str(s["become_password"]))
            if s.get("limit") or s.get("start_at_task"):
                ansible_runner.stash_opts(rid, {"limit": str(s.get("limit") or "").strip(),
                                                "start_at_task": str(s.get("start_at_task") or "").strip()})
        if kind == "terraform" and s.get("tool"):
            terraform_runner.stash_tool(rid, str(s["tool"]))
        prepared.append((rid, kind))

    def worker():
        for i, (rid, kind) in enumerate(prepared):
            if kind == "inventory":
                iid = _run_inventory_step(rid, project)
                # Back-fill the inventory just built from the applied VMs into the
                # following Ansible/Salt steps that don't already name one, so the
                # sequence can configure/maintain the machines it just created.
                if iid:
                    for rid2, kind2 in prepared[i + 1:]:
                        r2 = db.get_run(rid2)
                        if kind2 in ("ansible", "salt") and r2 and not r2.get("inventory_id"):
                            db.set_run_inventory(rid2, iid)
            else:
                RUNNERS[kind](rid)        # blocking — runs to completion
            r = db.get_run(rid)
            if stop_on_failure and (not r or r.get("status") != "success"):
                for rid2, _ in prepared[i + 1:]:
                    db.set_run_status(rid2, "canceled", finished=int(time.time()))
                break

    db.log_audit("pipeline_launched", actor, f"{len(prepared)} steps on {project['name']}")
    threading.Thread(target=worker, daemon=True).start()
    return [rid for rid, _ in prepared], group_id


def _run_inventory_step(run_id: int, project) -> int | None:
    """The 'inventory' pseudo-step: read the just-applied VMs into the project's
    infra inventory, writing a normal run log + status so it shows in the sequence
    visualizer. Returns the inventory id on success (for back-filling later steps),
    or None on failure."""
    log_path = db.run_log_path(run_id)
    db.set_run_status(run_id, "running", started=int(time.time()))
    with log_path.open("w", buffering=1) as log:
        def emit(m):
            log.write(m if m.endswith("\n") else m + "\n")

        emit(f"== SLEP run #{run_id} · project '{project['name']}' · build inventory from applied VMs ==")
        meta = db.get_infra(project["id"])
        if not meta:
            emit("!! Not an infrastructure project — nothing to read.")
            db.set_run_status(run_id, "failed", exit_code=2, finished=int(time.time()))
            return None
        try:
            iid, name, n = _build_infra_inventory(project, meta)
        except HTTPException as e:
            emit(f"!! {e.detail}")
            db.set_run_status(run_id, "failed", exit_code=2, finished=int(time.time()))
            return None
        emit(f"Built inventory “{name}” (#{iid}) with {n} host(s).")
        emit("The following Ansible/Salt steps in this sequence will target it.")
        emit("\n== finished: exit code 0 ==")
        db.set_run_status(run_id, "success", exit_code=0, finished=int(time.time()))
        return iid


@app.post("/pipelines/run")
def run_pipeline_adhoc(body: dict = Body(...), user: str = Depends(current_user)):
    """Launch an ad-hoc sequence of runs (create → configure → maintain, or any
    order). Steps run one after another; by default the sequence stops on the first
    failure. Each step is the same shape as a /runs body."""
    project = db.get_project(body.get("project_id")) if body.get("project_id") else None
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    steps = body.get("steps") or []
    _validate_steps(steps)
    run_ids, group_id = _dispatch_pipeline(project, steps, user, stop_on_failure=bool(body.get("stop_on_failure", True)))
    return {"status": "launched", "run_ids": run_ids, "group_id": group_id}


@app.get("/pipelines/runs/{group_id}")
def pipeline_group_runs(group_id: str, request: Request, user: str = Depends(current_user)):
    """The runs of one launched pipeline, in order — powers the sequence strip in
    the run visualizer."""
    role = _session_or_401(request)["role"]
    return {"runs": [_mask_extra_vars(r, role) for r in db.runs_in_group(group_id)]}


# ---- saved pipelines (named, re-runnable sequences) ----
@app.get("/pipelines")
def list_saved_pipelines(user: str = Depends(current_user)):
    return {"pipelines": db.list_pipelines()}


@app.post("/pipelines")
def create_saved_pipeline(body: dict = Body(...), user: str = Depends(current_user)):
    project = db.get_project(body.get("project_id")) if body.get("project_id") else None
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A pipeline name is required.")
    steps = body.get("steps") or []
    _validate_steps(steps)
    pid = db.create_pipeline(project["id"], name, _strip_saved_pipeline_secrets(steps), bool(body.get("stop_on_failure", True)))
    db.log_audit("pipeline_saved", user, f"#{pid} '{name}' on {project['name']}")
    return {"pipeline": db.get_pipeline(pid)}


@app.put("/pipelines/{pipeline_id}")
def edit_saved_pipeline(pipeline_id: int, body: dict = Body(...), user: str = Depends(current_user)):
    if not db.get_pipeline(pipeline_id):
        raise HTTPException(status_code=404, detail="Pipeline not found.")
    steps = body.get("steps")
    if steps is not None:
        _validate_steps(steps)
        steps = _strip_saved_pipeline_secrets(steps)
    db.update_pipeline(pipeline_id, name=(str(body["name"]).strip() if "name" in body else None),
                       steps=steps, stop_on_failure=body.get("stop_on_failure"))
    return {"pipeline": db.get_pipeline(pipeline_id)}


@app.delete("/pipelines/{pipeline_id}")
def remove_saved_pipeline(pipeline_id: int, user: str = Depends(current_user)):
    db.delete_pipeline(pipeline_id)
    return {"status": "deleted"}


@app.post("/pipelines/{pipeline_id}/run")
def run_saved_pipeline(pipeline_id: int, user: str = Depends(current_user)):
    pl = db.get_pipeline(pipeline_id)
    if not pl:
        raise HTTPException(status_code=404, detail="Pipeline not found.")
    project = db.get_project(pl["project_id"])
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    _validate_steps(pl["steps"])
    run_ids, group_id = _dispatch_pipeline(project, pl["steps"], user, stop_on_failure=pl["stop_on_failure"])
    return {"status": "launched", "run_ids": run_ids, "group_id": group_id}


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
    run_id = _dispatch_run(project, kind, target, body.get("inventory_id"),
                           body.get("credential_id"), body.get("extra_vars") or {}, user,
                           become_password=str(body.get("become_password") or ""),
                           limit=str(body.get("limit") or "").strip(),
                           start_at_task=str(body.get("start_at_task") or "").strip(),
                           tf_tool=str(body.get("tool") or "").strip())
    return {"status": "launched", "run_id": run_id}


@app.get("/runs")
def runs(request: Request, project_id: int | None = None, user: str = Depends(current_user)):
    role = _session_or_401(request)["role"]
    return {"runs": [_mask_extra_vars(r, role) for r in db.list_runs(project_id)]}


@app.get("/runs/{run_id}")
def get_run(run_id: int, request: Request, user: str = Depends(current_user)):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Run not found.")
    return _mask_extra_vars(r, _session_or_401(request)["role"])


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


@app.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, user: str = Depends(require_operator)):
    """Stop an in-flight run. Kills the engine's child process (its process group,
    so terraform provider / ssh grandchildren go too) and records the run as
    canceled. A run that hasn't started a child yet (queued, or between pipeline
    steps) is flagged and marked canceled directly."""
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Run not found.")
    if r.get("status") in ("success", "failed", "canceled"):
        return {"status": r["status"]}   # already finished — nothing to stop
    from .runners import _common
    live = _common.request_stop(run_id)
    if not live:
        # Nothing streaming yet (queued step): mark it canceled so the sequence
        # and the console reflect it immediately; the runner sees the flag and
        # skips it if it was about to start.
        db.set_run_status(run_id, "canceled", finished=int(time.time()))
    db.log_audit("run_canceled", user, f"#{run_id} {r.get('kind')} '{r.get('target')}'")
    return {"status": "canceling"}


# ------------------------------------------------------------------ schedules
_CADENCES = {"hourly", "daily", "weekly"}


@app.get("/schedules")
def schedules_list(project_id: int | None = None, user: str = Depends(current_user)):
    return {"schedules": db.list_schedules(project_id)}


@app.post("/schedules")
def schedules_create(body: dict = Body(...), user: str = Depends(require_operator)):
    pid = body.get("project_id")
    project = db.get_project(pid) if pid else None
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    kind = str(body.get("kind") or "ansible")
    if kind not in RUNNERS and kind != "pipeline":
        raise HTTPException(status_code=400, detail=f"Unknown engine '{kind}'.")
    target = str(body.get("target") or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="target is required.")
    # A pipeline schedule stores the saved-pipeline id as its target — check it
    # exists and belongs to the chosen project so the scheduler can fire it.
    if kind == "pipeline":
        try:
            pl = db.get_pipeline(int(target))
        except (TypeError, ValueError):
            pl = None
        if not pl:
            raise HTTPException(status_code=400, detail="Pick a saved pipeline to schedule.")
        if pl["project_id"] != pid:
            raise HTTPException(status_code=400, detail="That pipeline belongs to a different project.")
    cadence = str(body.get("cadence") or "daily")
    if cadence not in _CADENCES:
        raise HTTPException(status_code=400, detail=f"cadence must be one of: {', '.join(_CADENCES)}.")
    sid = db.create_schedule(
        str(body.get("name") or "").strip(), pid, kind, target, cadence,
        str(body.get("at") or "02:00"), int(body.get("weekday") or 0),
        inventory_id=body.get("inventory_id"), credential_id=body.get("credential_id"),
        extra_vars=body.get("extra_vars") or {}, created_by=user)
    db.log_audit("schedule_created", user, f"'{body.get('name') or sid}' {kind} {cadence}")
    return db.get_schedule(sid)


@app.patch("/schedules/{sid}")
def schedules_update(sid: int, body: dict = Body(...), user: str = Depends(require_operator)):
    if not db.get_schedule(sid):
        raise HTTPException(status_code=404, detail="Schedule not found.")
    if "cadence" in body and body["cadence"] not in _CADENCES:
        raise HTTPException(status_code=400, detail=f"cadence must be one of: {', '.join(_CADENCES)}.")
    db.update_schedule(sid, **body)
    return db.get_schedule(sid)


@app.delete("/schedules/{sid}")
def schedules_delete(sid: int, user: str = Depends(require_operator)):
    db.delete_schedule(sid)
    db.log_audit("schedule_deleted", user, str(sid))
    return {"status": "deleted"}


# ------------------------------------------------------------------ health warnings
@app.get("/health-warnings")
def health_warnings(user: str = Depends(current_user)):
    """Surface operational problems the operator should know about — a missing
    engine binary, an unwritable data dir. Empty list = all clear."""
    import shutil
    warnings = []
    # Terraform is satisfied by either `terraform` or OpenTofu's `tofu` (a drop-in).
    engine_bins = (
        (("ansible-playbook",), "Ansible", "ansible-playbook"),
        (("terraform", "tofu"), "Terraform", "terraform or tofu (OpenTofu)"),
        (("salt-ssh",), "Salt", "salt-ssh"),
    )
    for binaries, engine, shown in engine_bins:
        if not any(shutil.which(b) for b in binaries):
            warnings.append({
                "id": f"engine-{engine.lower()}",
                "severity": "warning",
                "title": f"{engine} is not installed",
                "detail": f"`{shown}` isn't on PATH, so {engine} runs will fail.",
                "hint": f"Install {engine} on the SLEP host (the container image bakes it in).",
            })
    if not any(shutil.which(b) for b in ("mkisofs", "genisoimage", "xorriso")):
        warnings.append({
            "id": "cloudinit-iso",
            "severity": "warning",
            "title": "cloud-init ISO tool missing",
            "detail": "No `mkisofs`/`genisoimage` on PATH — libvirt (KVM) VM builds fail at the "
                      "cloud-init step (the dmacvicar/libvirt provider needs it to build each VM's ISO).",
            "hint": "Install genisoimage on the SLEP host (the container image bakes it in — `slep update`).",
        })
    if not os.access(db.DATA_DIR, os.W_OK):
        warnings.append({
            "id": "data-dir",
            "severity": "critical",
            "title": "Data directory is not writable",
            "detail": f"{db.DATA_DIR} can't be written — runs and project files will fail.",
            "hint": "Check the volume mount and its ownership.",
        })
    return {"warnings": warnings}


# ------------------------------------------------------------------ engines (1-click install)
@app.get("/engines")
def engines_status(user: str = Depends(current_user)):
    """Install state of each automation engine (installed / installing)."""
    return {"engines": engines.status()}


@app.post("/engines/{engine}/install")
def engines_install(engine: str, user: str = Depends(require_superuser)):
    """Kick off a one-click install of a missing engine. Streams to its log."""
    if engine not in engines.ENGINES:
        raise HTTPException(status_code=404, detail="Unknown engine.")
    if engines.is_installed(engine):
        return {"status": "already-installed"}
    engines.start_install(engine)
    db.log_audit("engine_install", user, engine)
    return {"status": "started"}


@app.get("/engines/{engine}/install-log", response_class=PlainTextResponse)
def engines_install_log(engine: str, offset: int = 0, user: str = Depends(current_user)):
    if engine not in engines.ENGINES:
        raise HTTPException(status_code=404, detail="Unknown engine.")
    text, nxt = engines.install_log(engine, offset)
    st = engines.status()[engine]
    install_status = "installed" if st["installed"] else (st["last_status"] or "")
    return PlainTextResponse(text, headers={"X-Log-Next": str(nxt),
                                            "X-Install-Status": install_status})


# ---- Ansible Galaxy collections (one-click install of the modules snippets use)
@app.get("/engines/collections")
def collections_status(user: str = Depends(current_user)):
    """Installed Galaxy collections + the curated 'common' set the task snippets use."""
    return engines.collections_status()


@app.post("/engines/collections/install")
def collections_install(body: dict = Body(default={}), user: str = Depends(require_superuser)):
    """Install Galaxy collections via ansible-galaxy (defaults to the common set).
    Pass {"collections": ["ns.name", ...]} to choose. Streams to the collections log."""
    names = body.get("collections")
    names = list(names) if isinstance(names, list) and names else None
    try:
        engines.start_collections_install(names)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.log_audit("collections_install", user, ",".join(names) if names else "common")
    return {"status": "started"}


@app.get("/engines/collections/install-log", response_class=PlainTextResponse)
def collections_install_log(offset: int = 0, user: str = Depends(current_user)):
    text, nxt = engines.collections_install_log(offset)
    st = engines.collections_status()
    install_status = "installing" if st["installing"] else (st["last_status"] or "")
    return PlainTextResponse(text, headers={"X-Log-Next": str(nxt),
                                            "X-Install-Status": install_status})


# ------------------------------------------------------------------ distribute SSH key
@app.get("/keydist/public-key")
def keydist_public_key(user: str = Depends(current_user)):
    """SLEP's managed public key (generated on first use) — shown so an operator
    can also install it by hand if they prefer."""
    try:
        return {"public_key": keydist.ensure_key()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/inventories/{iid}/distribute-key")
def keydist_distribute(iid: int, body: dict = Body(...), user: str = Depends(require_superuser)):
    """Install SLEP's public key on the inventory's hosts (a selection via
    host_names, or all), authenticating once with the supplied SSH password
    through the inventory's jump host. On success a 'SLEP managed key' credential
    is (re)created for key-based runs. Streams progress to the distribute log."""
    names = body.get("host_names")
    only = list(names) if isinstance(names, list) else None
    try:
        keydist.start_distribute(iid, only, str(body.get("username") or "").strip(),
                                 str(body.get("password") or ""), str(body.get("bastion") or "").strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.log_audit("keydist", user, f"inventory:{iid}")
    return {"status": "started"}


@app.get("/inventories/{iid}/distribute-key/log", response_class=PlainTextResponse)
def keydist_log(iid: int, offset: int = 0, user: str = Depends(current_user)):
    text, nxt = keydist.job_log(iid, offset)
    status = "running" if keydist.job_running(iid) else "done"
    return PlainTextResponse(text, headers={"X-Log-Next": str(nxt), "X-Run-Status": status})


@app.post("/inventories/{iid}/prepare-bastion")
def keydist_prepare_bastion(iid: int, body: dict = Body(...), user: str = Depends(require_superuser)):
    """Install SLEP's key on the inventory's jump host so the ProxyJump hop is
    key-based. Uses the bastion spec from the body (or the inventory's saved one)."""
    inv = db.get_inventory(iid)
    if not inv:
        raise HTTPException(status_code=404, detail="Inventory not found.")
    bastion = str(body.get("bastion") or inv.get("bastion") or "").strip()
    try:
        keydist.start_prepare_bastion(iid, bastion, str(body.get("password") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.log_audit("prepare_bastion", user, f"inventory:{iid}")
    return {"status": "started"}


@app.get("/inventories/{iid}/prepare-bastion/log", response_class=PlainTextResponse)
def keydist_prepare_bastion_log(iid: int, offset: int = 0, user: str = Depends(current_user)):
    key = f"{iid}-bastion"
    text, nxt = keydist.job_log(key, offset)
    status = "running" if keydist.job_running(key) else "done"
    return PlainTextResponse(text, headers={"X-Log-Next": str(nxt), "X-Run-Status": status})


@app.post("/inventories/{iid}/test-connection")
def keydist_test(iid: int, body: dict = Body(default={}), user: str = Depends(current_user)):
    """Probe SSH reachability of the inventory's hosts with a chosen credential
    (or the SLEP managed key), through the jump host. Streams a per-host report."""
    names = body.get("host_names")
    only = list(names) if isinstance(names, list) else None
    try:
        keydist.start_test(iid, only, body.get("credential_id"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "started"}


@app.get("/inventories/{iid}/test-connection/log", response_class=PlainTextResponse)
def keydist_test_log(iid: int, offset: int = 0, user: str = Depends(current_user)):
    key = f"{iid}-test"
    text, nxt = keydist.job_log(key, offset)
    status = "running" if keydist.job_running(key) else "done"
    return PlainTextResponse(text, headers={"X-Log-Next": str(nxt), "X-Run-Status": status})


# ------------------------------------------------------------------ create infrastructure
@app.get("/infra/providers")
def infra_providers(user: str = Depends(current_user)):
    """Provider + VM option menus for the Create Infrastructure wizard."""
    return {"providers": infra.provider_schema()}


# Libvirt connection URIs are operator-supplied and handed to virsh / the terraform
# provider. The ssh transport honours params that run an arbitrary local/remote
# binary (command=, netcat=), and an arbitrary scheme/host is an SSRF vector. Allow
# only known qemu transports and reject the exec-capable params (keyfile/no_verify —
# which SLEP itself sets — stay allowed).
_LIBVIRT_URI_SCHEMES = {"qemu", "qemu+ssh", "qemu+tls", "qemu+tcp", "qemu+unix", "qemu+libssh", "qemu+libssh2"}
_LIBVIRT_URI_DENY_PARAMS = {"command", "netcat", "proxy"}


def _validate_libvirt_uri(uri: str) -> str:
    import urllib.parse
    u = str(uri or "").strip()
    if not u:
        raise HTTPException(status_code=400, detail="A hypervisor connection URI is required.")
    scheme = u.split("://", 1)[0].split(":", 1)[0].lower() if "://" in u else u.split(":", 1)[0].lower()
    if scheme not in _LIBVIRT_URI_SCHEMES:
        raise HTTPException(status_code=400,
                            detail=f"Unsupported hypervisor URI scheme '{scheme}'. Use qemu:///system or qemu+ssh:// / qemu+tls:// / qemu+tcp://.")
    params = {k.lower() for k, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query)}
    bad = sorted(params & _LIBVIRT_URI_DENY_PARAMS)
    if bad:
        raise HTTPException(status_code=400,
                            detail=f"Hypervisor URI parameter(s) not allowed (they can run arbitrary commands): {', '.join(bad)}.")
    return u


@app.post("/infra/test-hypervisor")
def infra_test_hypervisor(body: dict = Body(...), user: str = Depends(current_user)):
    """Probe a libvirt (KVM/QEMU) hypervisor connection before applying — runs a
    read-only `virsh -c <uri> version` so the operator can confirm reachability
    (local qemu:///system, or a remote qemu+ssh://host). When `network`/`pool` are
    given, also verifies those exist and are active — the two most common apply
    blockers (`can't retrieve network 'default'`, missing storage pool) — so they're
    caught in seconds instead of minutes into an apply. Never mutates anything."""
    import re
    import shutil
    uri = _validate_libvirt_uri(body.get("uri"))
    network = str(body.get("network") or "").strip()
    pool = str(body.get("pool") or "").strip()
    if not shutil.which("virsh"):
        return {"ok": False, "output":
                "`virsh` (libvirt-clients) isn't installed on the SLEP host, so the connection can't be "
                "probed here — Terraform apply will still use the URI. Install libvirt-clients to enable this test."}
    env = dict(os.environ)
    # For qemu+ssh:// don't wedge on an unknown host key or a passphrase prompt;
    # BatchMode makes ssh fail fast instead of hanging for input.
    env["GIT_SSH_COMMAND"] = env.get("GIT_SSH_COMMAND", "")  # no-op guard
    try:
        p = subprocess.run(
            ["virsh", "-c", uri, "--readonly", "version"],
            capture_output=True, text=True, timeout=25, env=env,
        )
        ok = p.returncode == 0
        out = (p.stdout + p.stderr).strip()
        if not ok:
            return {"ok": False, "output": out or "connection failed"}
        out = out or "connected."

        def avail_names(listsub):
            try:
                r = subprocess.run(["virsh", "-c", uri, "--readonly", listsub, "--all", "--name"],
                                   capture_output=True, text=True, timeout=20, env=env)
                return [x.strip() for x in r.stdout.split("\n") if x.strip()]
            except Exception:  # noqa: BLE001
                return []

        # Preflight the resources the apply needs: the named network + storage pool
        # must exist and be active, or `libvirt_domain` / `libvirt_volume` fail. On a
        # miss, list what IS available so the operator knows what to use (e.g. this
        # host has 'homelab', not 'default').
        for label, sub, listsub, name in (
                ("network", "net-info", "net-list", network),
                ("storage pool", "pool-info", "pool-list", pool)):
            if not name:
                continue
            try:
                c2 = subprocess.run(["virsh", "-c", uri, "--readonly", sub, name],
                                    capture_output=True, text=True, timeout=20, env=env)
            except subprocess.TimeoutExpired:
                out += f"\n• {label} '{name}': check timed out"
                continue
            if c2.returncode != 0:
                avail = avail_names(listsub)
                out += f"\n• {label} '{name}': ✗ MISSING" + (f" — available: {', '.join(avail)}" if avail else " (none defined)")
                ok = False
            elif not re.search(r"Active:\s+yes", c2.stdout):
                out += f"\n• {label} '{name}': ✗ defined but INACTIVE — start it (virsh {sub.split('-')[0]}-start {name})"
                ok = False
            else:
                out += f"\n• {label} '{name}': ✓"
        return {"ok": ok, "output": out}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output":
                "timed out after 25s — the hypervisor didn't respond. For qemu+ssh:// check SSH reachability "
                "and that the host key is already trusted on the SLEP host (a new key can't be accepted here)."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "output": str(e)}


def _virsh_list_domains(uri: str):
    """Read the domains on a libvirt hypervisor (`virsh list --all`). Returns
    (list[{id,name,state}], None) or (None, error_message)."""
    import shutil
    if not shutil.which("virsh"):
        return None, "`virsh` (libvirt-clients) isn't installed on the SLEP host."
    try:
        p = subprocess.run(["virsh", "-c", uri, "--readonly", "list", "--all"],
                           capture_output=True, text=True, timeout=25, env=dict(os.environ))
    except subprocess.TimeoutExpired:
        return None, "timed out after 25s — the hypervisor didn't respond (check it's up and SSH-reachable)."
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    if p.returncode != 0:
        return None, (p.stderr or p.stdout or "virsh failed").strip()[:300]
    doms = []
    for line in p.stdout.splitlines():
        s = line.strip()
        if not s or s.startswith("Id ") or set(s) <= set("- "):
            continue
        parts = s.split()
        if len(parts) < 3:
            continue
        doms.append({"id": parts[0], "name": parts[1], "state": " ".join(parts[2:])})
    return doms, None


def _libvirt_uri_for_project(project_id: int) -> str:
    """Pull the libvirt connection URI baked into a project's variables.tf."""
    import re
    vf = db.project_dir(project_id) / "variables.tf"
    if not vf.exists():
        return ""
    m = re.search(r'variable\s+"uri"\s*\{[^}]*?default\s*=\s*"([^"]*)"', vf.read_text(), re.S)
    return m.group(1) if m else ""


@app.post("/infra/list-vms")
def infra_list_vms(body: dict = Body(...), user: str = Depends(require_operator)):
    """List the domains on a libvirt hypervisor from an ad-hoc URI (the Create form)."""
    uri = _validate_libvirt_uri(body.get("uri"))
    doms, err = _virsh_list_domains(uri)
    return {"ok": err is None, "vms": doms or [], "output": err or ""}


@app.post("/infra/{project_id}/vms")
def infra_project_vms(project_id: int, user: str = Depends(require_operator)):
    """List the VMs on the hypervisor this infra project targets (its var.uri) — so
    the operator can see what's actually running without SSHing to the host."""
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    uri = _libvirt_uri_for_project(project_id)
    if not uri:
        raise HTTPException(status_code=400, detail="This project has no libvirt connection URI (not a libvirt project?).")
    _validate_libvirt_uri(uri)
    doms, err = _virsh_list_domains(uri)
    return {"ok": err is None, "vms": doms or [], "output": err or "", "uri": uri}


@app.post("/infra/hypervisor-key")
def infra_hypervisor_key(user: str = Depends(require_operator)):
    """Ensure SLEP's managed hypervisor SSH key exists (a persistent ed25519 pair
    under the data volume) and return its public half + the in-container keyfile
    path. This is what makes remote-KVM setup a two-step flow: the operator drops
    the returned public key into the hypervisor's authorized_keys, and SLEP builds
    the `qemu+ssh://…?keyfile=…` URI itself — no manual ssh-keygen or path-guessing.
    The key lives on the data volume, so it survives image rebuilds."""
    import shutil
    key = db.DATA_DIR / "ssh" / "hypervisor"
    key.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(key.parent, 0o700)
    except OSError:
        pass
    if not key.exists():
        if not shutil.which("ssh-keygen"):
            raise HTTPException(status_code=500, detail="ssh-keygen isn't available on the SLEP host.")
        r = subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "slep-hypervisor",
                            "-f", str(key)], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise HTTPException(status_code=500, detail="Couldn't generate the key: " + (r.stderr or "").strip()[:200])
    try:
        pub = Path(str(key) + ".pub").read_text().strip()   # ssh-keygen writes <key>.pub
    except OSError:
        raise HTTPException(status_code=500, detail="Key generated but its public half couldn't be read.")
    return {"public_key": pub, "keyfile": str(key)}


@app.post("/infra/{project_id}/scaffold")
def infra_scaffold(project_id: int, body: dict = Body(...), user: str = Depends(current_user)):
    """Scaffold the next cadence stage into an infra project: 'configure' writes a
    starter Ansible playbook, 'maintain' a starter Salt state — so an operator goes
    straight from built VMs to configuring/maintaining them. Won't overwrite an
    existing file. Returns the path to open in the IDE."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        fname, content = infra.scaffold(str(body.get("stage") or ""), project.get("name", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="stage must be 'configure' or 'maintain'.")
    dest = db.project_dir(project_id) / fname
    created = not dest.exists()
    if created:
        dest.write_text(content)
        db.touch_project(project_id)
    return {"path": fname, "created": created}


@app.get("/infra")
def infra_list(user: str = Depends(current_user)):
    return {"infra": db.list_infra()}


def _derive_public_key(private_key: str) -> str:
    """Derive the OpenSSH public key from a private key (`ssh-keygen -y`). Returns ''
    when it can't — no ssh-keygen, a malformed key, or a passphrase-protected one
    (which fails non-interactively). Never writes the key anywhere persistent."""
    import shutil
    import tempfile
    if not private_key or not shutil.which("ssh-keygen"):
        return ""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "k"
        data = (private_key if private_key.endswith("\n") else private_key + "\n").encode()
        # Atomic create at 0600 (never a 0644 window) — mirrors the runners' _write_key.
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        try:
            r = subprocess.run(["ssh-keygen", "-y", "-f", str(p)],
                               capture_output=True, text=True, timeout=15)
        except Exception:  # noqa: BLE001
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""


@app.post("/infra")
def infra_create(body: dict = Body(...), user: str = Depends(require_operator)):
    """Generate a Terraform VM project from the wizard selections. A chosen deploy
    SSH credential's public key is baked into the VMs' cloud-init (so SLEP's own
    Ansible/Salt can log in), as is a chosen Controller's key (so it can reach them
    after boot); the project is tagged for one-click enroll."""
    name = str(body.get("name") or "").strip()
    provider = str(body.get("provider") or "")
    options = body.get("options") or {}
    controller_id = body.get("controller_id")
    if not name:
        raise HTTPException(status_code=400, detail="A name is required.")
    if provider not in infra.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{provider}'.")
    # The libvirt connection URI is baked into the generated Terraform; validate it
    # (scheme allowlist + reject exec-capable params) at the boundary.
    if provider == "libvirt" and str(options.get("uri") or "").strip():
        _validate_libvirt_uri(options.get("uri"))

    controller_key = ""
    if controller_id:
        ctrl = db.get_controller(int(controller_id), include_key=True)
        if not ctrl:
            raise HTTPException(status_code=404, detail="Controller not found.")
        try:
            controller_key = controller_import.get_controller_key(ctrl["base_url"], ctrl["api_key"])
        except controller_import.ControllerImportError:
            controller_key = ""   # non-fatal: generate anyway, enroll can install the key later

    # Deploy credential: bake the public half of a SLEP SSH credential into the VMs
    # so the same credential you pick for the cadence's Ansible/Salt steps can log in.
    deploy_key = ""
    deploy_cred_id = body.get("deploy_credential_id")
    if deploy_cred_id:
        cred = db.get_credential(int(deploy_cred_id), include_secret=True)
        if not cred:
            raise HTTPException(status_code=404, detail="Deploy credential not found.")
        if cred.get("kind") != "ssh":
            raise HTTPException(status_code=400, detail="The deploy credential must be an SSH key credential (not a password/cloud one).")
        deploy_key = _derive_public_key(cred.get("secret") or "")
        if not deploy_key:
            raise HTTPException(status_code=400,
                                detail="Couldn't derive a public key from that credential — it must be an unencrypted SSH private key.")

    try:
        files = infra.generate(provider, options, controller_key, deploy_key=deploy_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # New project (unique slug), then write the generated files to its workdir.
    base = _slugify(name)
    slug, n = base, 1
    while any(p["slug"] == slug for p in db.list_projects()):
        n += 1
        slug = f"{base}-{n}"
    pid = db.create_project(name, slug, f"Terraform ({infra.PROVIDERS[provider]['label']}) — built with Create Infrastructure", "", "")
    workdir = db.project_dir(pid)
    workdir.mkdir(parents=True, exist_ok=True)
    for fn, content in files.items():
        (workdir / fn).write_text(content)
    # Target inventory for the applied VMs: an existing one (inventory_id, e.g. Dev),
    # a new one (inventory_name), or None → a dedicated "<name> (VMs)" is built.
    inv_target = None
    if body.get("inventory_id"):
        if not db.get_inventory(int(body["inventory_id"])):
            raise HTTPException(status_code=404, detail="Target inventory not found.")
        inv_target = int(body["inventory_id"])
    elif str(body.get("inventory_name") or "").strip():
        nm = str(body["inventory_name"]).strip()
        dup = db.find_inventory_by_name(pid, nm)
        inv_target = dup["id"] if dup else db.create_inventory(nm, project_id=pid, source="infra")
    db.set_infra(pid, provider, int(controller_id) if controller_id else None,
                 str(options.get("ssh_user", "")), str(options.get("environment", "")), inventory_id=inv_target)
    db.log_audit("infra_created", user, f"{provider} project '{name}'")
    return {"project_id": pid, "slug": slug, "files": list(files), "provider": provider, "inventory_id": inv_target}


def _infra_applied_hosts(project_id: int):
    """Read the created VMs from `terraform output` (the sysible_hosts list). Works
    for either terraform or tofu state (both write the same output). Raises
    HTTPException with a clear message when apply hasn't produced hosts yet."""
    import shutil
    workdir = db.project_dir(project_id)
    engines.ensure_path()
    tool = "terraform" if shutil.which("terraform") else ("tofu" if shutil.which("tofu") else "terraform")
    try:
        out = subprocess.run([tool, "output", "-json"], cwd=str(workdir),
                             capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail="Terraform/OpenTofu isn't installed — install it, then apply first.")
    if out.returncode != 0:
        raise HTTPException(status_code=400,
                            detail="No outputs yet — run apply first. " + (out.stderr or "").strip()[:200])
    try:
        data = json.loads(out.stdout or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="Couldn't parse the Terraform/OpenTofu outputs.")
    hosts = (data.get("sysible_hosts") or {}).get("value") or []
    if not hosts:
        raise HTTPException(status_code=400, detail="No hosts in the outputs (apply may not be complete).")
    return hosts


def _build_infra_inventory(project, meta, target_inventory_id=None):
    """Read the applied VMs (sysible_hosts) into an Ansible inventory (created once,
    refreshed on re-run): name → address = ip, ansible_user set, grouped by the
    environment tag. Returns (inventory_id, name, host_count). Raises HTTPException
    (via _infra_applied_hosts) when apply hasn't produced hosts yet. Shared by the
    manual '→ Inventory' action and the pipeline's auto-inventory step.

    Inventory target precedence: an explicit `target_inventory_id` (e.g. the one a
    pipeline's Ansible/Salt step already selected) wins, so the VMs land exactly
    where the next step will look for them; otherwise the infra's configured
    inventory; otherwise a dedicated "<name> (VMs)" inventory for this project."""
    hosts = _infra_applied_hosts(project["id"])
    # Resolve to exactly one inventory, deterministically, so no apply/enroll/
    # pipeline path can ever spin up a second inventory for the same project:
    #   explicit target → the project's pinned infra inventory → any existing
    #   'infra'-sourced inventory → get-or-create the canonical "<name> (VMs)".
    target = target_inventory_id or meta.get("inventory_id")
    if target and db.get_inventory(target):
        iid = target
    else:
        inv = db.find_inventory(project["id"], "infra")
        if inv:
            iid = inv["id"]
        else:
            canonical = f"{project['name']} (VMs)"
            existing = db.find_inventory_by_name(project["id"], canonical)
            iid = existing["id"] if existing else db.create_inventory(
                canonical, project_id=project["id"], source="infra")
    # Pin the resolved inventory to the infra project the first time, so subsequent
    # builds go straight down the `target` branch above and never re-create.
    if meta.get("inventory_id") != iid:
        db.set_infra_inventory(project["id"], iid)
    group = ansible_runner._ansible_group(meta.get("environment", "")) if meta.get("environment") else ""
    n = 0
    for h in hosts:
        nm, ip = str(h.get("name") or ""), str(h.get("ip") or "")
        huser = str(h.get("user") or meta.get("ssh_user") or "root")
        if not nm or not ip:
            continue
        db.upsert_host(iid, nm, ip, groups=group, variables={"ansible_user": huser}, source="infra")
        n += 1
    return iid, db.get_inventory(iid)["name"], n


def _autobuild_infra_inventory(project_id: int, run=None):
    """Best-effort: read a project's applied VMs into an inventory. Returns
    (inventory_id, name, host_count), or None when it's not an infra project or
    apply hasn't produced hosts yet. Called automatically by the terraform runner
    after a successful apply so new VMs land in an inventory with no manual step.

    When `run` is part of a pipeline (has a group_id), the VMs are written into the
    inventory the *next* Ansible/Salt step already selected — so "I picked an
    inventory for the Configure step" just works — and any following Ansible/Salt
    steps that didn't name one are back-filled to the same inventory."""
    project = db.get_project(project_id)
    meta = db.get_infra(project_id)
    if not project or not meta:
        return None

    # Downstream steps of this run's pipeline (in order), if any.
    later_steps = []
    if run and run.get("group_id"):
        later_steps = [r for r in db.runs_in_group(run["group_id"])
                       if r.get("id", 0) > run.get("id", 0)
                       and r.get("kind") in ("ansible", "salt")]

    # If a downstream step already names an inventory, that's where the operator
    # expects the machines — target it directly instead of the default.
    target = next((r.get("inventory_id") for r in later_steps if r.get("inventory_id")), None)

    try:
        iid, name, n = _build_infra_inventory(project, meta, target_inventory_id=target)
    except HTTPException:
        return None

    # Point any downstream Ansible/Salt steps that didn't pick an inventory at the
    # one we just populated, so the whole sequence configures these VMs.
    for r in later_steps:
        if not r.get("inventory_id"):
            db.set_run_inventory(r["id"], iid)
    return iid, name, n


@app.post("/infra/{project_id}/inventory")
def infra_to_inventory(project_id: int, user: str = Depends(require_operator)):
    """After apply, read the created VMs (sysible_hosts) into a SLEP Ansible
    inventory for this project. Reuses/refreshes the project's infra inventory on
    re-run, so the Configure (Ansible) step can immediately target the new
    machines."""
    project = db.get_project(project_id)
    meta = db.get_infra(project_id)
    if not project or not meta:
        raise HTTPException(status_code=404, detail="Not an infrastructure project.")
    iid, name, n = _build_infra_inventory(project, meta)
    db.log_audit("infra_to_inventory", user, f"{n} host(s) into inventory #{iid}")
    return {"inventory_id": iid, "name": name, "hosts": n}


def _enroll_infra_hosts(project_id: int, controller_id=None):
    """Register a project's applied VMs (the sysible_hosts output) into a
    Controller as SSH hosts. Returns {results, enrolled, total, controller}.
    `controller_id` overrides the infra's configured Controller (so the operator
    can enroll on demand into any connected Controller); when it's given and the
    infra had none stored, it's persisted so key-baking and later enrolls reuse
    it. Raises HTTPException when the project isn't infra, no Controller is chosen
    or found, or apply produced no hosts yet."""
    meta = db.get_infra(project_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Not an infrastructure project.")
    cid = controller_id or meta.get("controller_id")
    if not cid:
        raise HTTPException(status_code=400, detail="No Controller was chosen for this infrastructure.")
    ctrl = db.get_controller(cid, include_key=True)
    if not ctrl:
        raise HTTPException(status_code=400, detail="The chosen Controller no longer exists.")
    # Persist an operator-picked Controller when the infra didn't have one, so the
    # choice sticks for future enrolls (and the SSH-key bake on the next apply).
    if controller_id and not meta.get("controller_id"):
        db.set_infra(project_id, meta["provider"], controller_id=cid,
                     ssh_user=meta.get("ssh_user", ""), environment=meta.get("environment", ""),
                     inventory_id=meta.get("inventory_id"))
    hosts = _infra_applied_hosts(project_id)

    results, ok_n = [], 0
    for h in hosts:
        nm, ip = str(h.get("name") or ""), str(h.get("ip") or "")
        huser = str(h.get("user") or meta.get("ssh_user") or "root")
        if not ip:
            results.append({"name": nm, "ip": "", "ok": False, "detail": "no IP yet"})
            continue
        ok, detail = controller_import.register_ssh_host(
            ctrl["base_url"], ctrl["api_key"], nm, ip, huser, meta.get("environment", ""))
        ok_n += 1 if ok else 0
        results.append({"name": nm, "ip": ip, "ok": ok, "detail": detail})
    return {"results": results, "enrolled": ok_n, "total": len(hosts), "controller": ctrl["name"]}


@app.post("/infra/{project_id}/enroll")
def infra_enroll(project_id: int, body: dict = Body(default=None),
                 user: str = Depends(require_operator)):
    """After `terraform apply`, read the created VMs (the sysible_hosts output) and
    register each into a Controller as an SSH host. Uses the infra's configured
    Controller, or an optional `controller_id` in the body to pick one on demand
    (which is then remembered for the project)."""
    cid = (body or {}).get("controller_id")
    out = _enroll_infra_hosts(project_id, controller_id=int(cid) if cid else None)
    db.log_audit("infra_enrolled", user,
                 f"{out['enrolled']}/{out['total']} into Controller '{out['controller']}'")
    return out
