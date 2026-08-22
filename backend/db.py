"""SLEP datastore (Community Edition) — SQLite by default (one file, no server),
mirroring the "single container, no external dependencies" ethos. Everything the
platform needs to remember lives here: admins, projects, credentials,
inventories, hosts, and run history. Project *files* live on disk under
DATA/projects/<id>/ (so the IDE and the runners both see real files); run *logs*
stream to DATA/runs/<id>.log.

This module IS the CE↔EE datastore seam: the rest of the backend calls only the
functions below, never raw SQL, so the future Enterprise Edition can supply a
PostgreSQL-backed implementation of this same API (as sysible-controller-ee does
for Controller) without changing the API surface or the runners. Keep new
persistence behind a function here, not inline in app.py.

The connection is opened per-call (SQLite is happiest that way under a threaded
server) with WAL enabled for concurrent readers during a long run.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(os.environ.get("SLEP_DATA_DIR", "./data")).resolve()
DB_PATH = Path(os.environ.get("SLEP_DB_PATH", str(DATA_DIR / "slep.sqlite3")))
PROJECTS_DIR = DATA_DIR / "projects"
RUNS_DIR = DATA_DIR / "runs"

SCHEMA_VERSION = 1


def _now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create the schema if absent. Idempotent — safe to call on every boot."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS admins (
                username TEXT PRIMARY KEY,
                pw_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '',
                scm_url TEXT DEFAULT '',
                scm_branch TEXT DEFAULT '',
                git_token TEXT DEFAULT '',          -- encrypted push/pull token
                created INTEGER NOT NULL,
                updated INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS secrets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                value TEXT NOT NULL,            -- ciphertext (see backend/vault.py); never plaintext
                created INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS controllers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,          -- server-side only, never returned to the browser
                created INTEGER NOT NULL,
                last_import INTEGER
            );

            CREATE TABLE IF NOT EXISTS credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'ssh',   -- ssh | ssh_password | cloud | vault
                username TEXT DEFAULT '',
                secret TEXT DEFAULT '',             -- private key / password / token (server-side only)
                become_secret TEXT DEFAULT '',      -- sudo/become password, ENCRYPTED at rest
                created INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                name TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',  -- manual | controller
                bastion TEXT DEFAULT '',                -- SSH jump host: user@host[:port]
                created INTEGER NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS hosts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inventory_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                groups TEXT DEFAULT '',             -- comma-separated
                variables TEXT DEFAULT '{}',        -- JSON of host vars
                source TEXT NOT NULL DEFAULT 'manual',
                created INTEGER NOT NULL,
                FOREIGN KEY (inventory_id) REFERENCES inventories(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'ansible',   -- ansible | terraform | salt
                target TEXT NOT NULL,                   -- playbook path / tf action / salt state
                inventory_id INTEGER,
                credential_id INTEGER,
                extra_vars TEXT DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',  -- queued|running|success|failed|canceled
                exit_code INTEGER,
                created_by TEXT DEFAULT '',
                started INTEGER,
                finished INTEGER,
                created INTEGER NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- Durable console sessions (mirrors Controller): the raw bearer token
            -- is NEVER stored — only its SHA-256, so a leaked DB snapshot can't be
            -- replayed as a live session. Survives restarts; resolve() cross-checks
            -- the live account role so a demotion/removal revokes within the TTL.
            CREATE TABLE IF NOT EXISTS admin_tokens (
                token TEXT PRIMARY KEY,          -- sha256(token) at rest
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                expiry REAL NOT NULL
            );

            -- Per-username login throttle + durable lockout (survives restart).
            CREATE TABLE IF NOT EXISTS login_throttle (
                key TEXT PRIMARY KEY,            -- username (or ip:<addr>)
                fails TEXT NOT NULL DEFAULT '[]',-- JSON array of recent failure epochs
                until REAL NOT NULL DEFAULT 0    -- locked until this epoch
            );

            -- Recurring runs: a saved run spec + a cadence. A scheduler thread
            -- launches the same engine/target/inventory on the computed next_run.
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                project_id INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'ansible',
                target TEXT NOT NULL,
                inventory_id INTEGER,
                credential_id INTEGER,
                extra_vars TEXT DEFAULT '{}',
                cadence TEXT NOT NULL DEFAULT 'daily',   -- hourly | daily | weekly
                at TEXT NOT NULL DEFAULT '02:00',        -- HH:MM (local) for daily/weekly; MM used for hourly
                weekday INTEGER NOT NULL DEFAULT 0,      -- 0=Mon .. 6=Sun (weekly)
                enabled INTEGER NOT NULL DEFAULT 1,
                created_by TEXT DEFAULT '',
                last_run INTEGER,
                last_status TEXT DEFAULT '',
                last_run_id INTEGER,
                next_run INTEGER,
                created INTEGER NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- 'Create Infrastructure' projects: the provider + which Controller to
            -- auto-enroll the created VMs into. One row per Terraform-builder project.
            CREATE TABLE IF NOT EXISTS infra (
                project_id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                controller_id INTEGER,
                ssh_user TEXT DEFAULT '',
                environment TEXT DEFAULT '',
                created INTEGER NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- Saved pipelines: a named, ordered list of run steps for a project,
            -- so a create->configure->maintain (or any) sequence can be re-run.
            CREATE TABLE IF NOT EXISTS pipelines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                steps TEXT NOT NULL DEFAULT '[]',        -- JSON list of step dicts
                stop_on_failure INTEGER NOT NULL DEFAULT 1,
                created INTEGER NOT NULL,
                updated INTEGER NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- Tamper-evident admin/activity audit log. Each row's entry_hash chains
            -- the previous one (SHA-256 over length-prefixed fields), so any edit,
            -- reorder, or deletion in the middle breaks the chain from that point on.
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                event TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                prev_hash TEXT NOT NULL,
                entry_hash TEXT NOT NULL
            );
            """
        )
        c.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        # Lightweight migrations for DBs created before a column existed.
        inv_cols = [r["name"] for r in c.execute("PRAGMA table_info(inventories)")]
        if "bastion" not in inv_cols:
            c.execute("ALTER TABLE inventories ADD COLUMN bastion TEXT DEFAULT ''")
        cred_cols = [r["name"] for r in c.execute("PRAGMA table_info(credentials)")]
        if "become_secret" not in cred_cols:
            c.execute("ALTER TABLE credentials ADD COLUMN become_secret TEXT DEFAULT ''")
        proj_cols = [r["name"] for r in c.execute("PRAGMA table_info(projects)")]
        if "git_token" not in proj_cols:
            c.execute("ALTER TABLE projects ADD COLUMN git_token TEXT DEFAULT ''")
        # group_id links the runs of one launched pipeline so the visualizer can
        # show the whole sequence.
        run_cols = [r["name"] for r in c.execute("PRAGMA table_info(runs)")]
        if "group_id" not in run_cols:
            c.execute("ALTER TABLE runs ADD COLUMN group_id TEXT DEFAULT ''")
    # The DB holds password hashes, session tokens, encrypted vault + Controller
    # keys — keep it owner-only so a stray world-read can't harvest them.
    _restrict_db_permissions()


def _restrict_db_permissions() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except OSError:
            pass


# ---------------------------------------------------------------- sessions & security
def _token_at_rest(token: str) -> str:
    """Only the SHA-256 of a bearer token is stored, so a DB leak can't be replayed
    as a live session (same pattern Controller uses for admin/agent secrets)."""
    return hashlib.sha256(token.encode()).hexdigest()


def create_admin_token(token: str, username: str, role: str, expiry: float) -> None:
    with _connect() as c:
        c.execute("INSERT OR REPLACE INTO admin_tokens(token,username,role,expiry) VALUES(?,?,?,?)",
                  (_token_at_rest(token), username, role, expiry))


def resolve_admin_token(token: str):
    """Return {username, role} for a live token, or None. Deletes + rejects on
    expiry, on a removed account, or when the account's role changed since the
    token was minted (so a demotion/removal revokes the session within its TTL)."""
    if not token:
        return None
    th = _token_at_rest(token)
    with _connect() as c:
        r = c.execute("SELECT username,role,expiry FROM admin_tokens WHERE token=?", (th,)).fetchone()
        if not r:
            return None
        if (r["expiry"] or 0) < time.time():
            c.execute("DELETE FROM admin_tokens WHERE token=?", (th,))
            return None
        admin = c.execute("SELECT role FROM admins WHERE username=?", (r["username"],)).fetchone()
        if not admin or admin["role"] != r["role"]:
            c.execute("DELETE FROM admin_tokens WHERE token=?", (th,))
            return None
        return {"username": r["username"], "role": r["role"]}


def delete_admin_token(token: str) -> None:
    with _connect() as c:
        c.execute("DELETE FROM admin_tokens WHERE token=?", (_token_at_rest(token),))


def delete_admin_tokens_for_user(username: str) -> None:
    """Drop every live session for an account — called on role change, password
    reset, or deletion so the change takes effect immediately."""
    with _connect() as c:
        c.execute("DELETE FROM admin_tokens WHERE username=?", (username,))


def purge_expired_tokens() -> None:
    with _connect() as c:
        c.execute("DELETE FROM admin_tokens WHERE expiry < ?", (time.time(),))


# ---- per-username login throttle + durable lockout --------------------------
def login_throttle_locked_for(key: str) -> int:
    with _connect() as c:
        r = c.execute("SELECT until FROM login_throttle WHERE key=?", (key,)).fetchone()
    if not r:
        return 0
    rem = (r["until"] or 0) - time.time()
    return int(rem) if rem > 0 else 0


def login_throttle_record_failure(key: str, window_s: int, max_failures: int, lockout_s: int) -> int:
    now = time.time()
    with _connect() as c:
        r = c.execute("SELECT fails FROM login_throttle WHERE key=?", (key,)).fetchone()
        fails = json.loads(r["fails"]) if r and r["fails"] else []
        fails = [t for t in fails if now - t < window_s]
        fails.append(now)
        until = 0.0
        if len(fails) >= max_failures:
            until = now + lockout_s
            fails = []
        c.execute("INSERT INTO login_throttle(key,fails,until) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET fails=excluded.fails, until=excluded.until",
                  (key, json.dumps(fails), until))
    return int(until - now) if until else 0


def login_throttle_clear(key: str) -> None:
    with _connect() as c:
        c.execute("DELETE FROM login_throttle WHERE key=?", (key,))


# ---- tamper-evident audit log ------------------------------------------------
_AUDIT_GENESIS = "0" * 64
_audit_lock = threading.Lock()


def _audit_digest(prev_hash: str, ts: float, event: str, username: str, detail: str) -> str:
    # Length-prefix each field so no combination of field contents can be shifted
    # across boundaries to forge an identical digest.
    parts = [prev_hash, repr(ts), event, username, detail]
    msg = "\x00".join(f"{len(p)}:{p}" for p in parts)
    return hashlib.sha256(msg.encode()).hexdigest()


def log_audit(event: str, username: str = "", detail: str = "") -> None:
    """Append a hash-chained audit/activity row. Never raises into the caller —
    an audit hiccup must not fail the action it records."""
    try:
        with _audit_lock, _connect() as c:
            row = c.execute("SELECT entry_hash FROM admin_audit_log ORDER BY id DESC LIMIT 1").fetchone()
            prev = row["entry_hash"] if row else _AUDIT_GENESIS
            ts = time.time()
            eh = _audit_digest(prev, ts, event, username or "", detail or "")
            c.execute("INSERT INTO admin_audit_log(ts,event,username,detail,prev_hash,entry_hash) "
                      "VALUES(?,?,?,?,?,?)", (ts, event, username or "", detail or "", prev, eh))
    except Exception:
        pass


def list_audit(limit: int = 100, since_id: int = 0):
    limit = max(1, min(int(limit or 100), 500))
    with _connect() as c:
        rows = c.execute("SELECT id,ts,event,username,detail FROM admin_audit_log "
                         "WHERE id > ? ORDER BY id DESC LIMIT ?", (since_id, limit)).fetchall()
    return [dict(r) for r in rows]


def verify_audit_chain():
    """Recompute the chain from genesis; report the first broken row (if any)."""
    with _connect() as c:
        rows = c.execute("SELECT id,ts,event,username,detail,prev_hash,entry_hash "
                         "FROM admin_audit_log ORDER BY id").fetchall()
    prev = _AUDIT_GENESIS
    for r in rows:
        calc = _audit_digest(prev, r["ts"], r["event"], r["username"] or "", r["detail"] or "")
        if r["prev_hash"] != prev or r["entry_hash"] != calc:
            return {"ok": False, "broken_at": r["id"], "entries": len(rows)}
        prev = r["entry_hash"]
    return {"ok": True, "entries": len(rows)}


# ---------------------------------------------------------------- schedules
def compute_next_run(cadence: str, at: str = "02:00", weekday: int = 0, now_ts: float | None = None) -> int:
    """Next fire time (epoch seconds, local clock) for a cadence. hourly fires at
    minute :MM of every hour; daily at HH:MM; weekly on `weekday` at HH:MM."""
    now = datetime.fromtimestamp(now_ts if now_ts is not None else time.time())
    try:
        hh, mm = (int(x) for x in (at or "02:00").split(":"))
    except ValueError:
        hh, mm = 2, 0
    if cadence == "hourly":
        cand = now.replace(minute=mm % 60, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(hours=1)
    elif cadence == "weekly":
        cand = now.replace(hour=hh % 24, minute=mm % 60, second=0, microsecond=0)
        cand += timedelta(days=(int(weekday) - now.weekday()) % 7)
        if cand <= now:
            cand += timedelta(days=7)
    else:  # daily
        cand = now.replace(hour=hh % 24, minute=mm % 60, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
    return int(cand.timestamp())


def _schedule_row(r) -> dict:
    d = dict(r)
    try:
        d["extra_vars"] = json.loads(d.get("extra_vars") or "{}")
    except (TypeError, ValueError):
        d["extra_vars"] = {}
    return d


def list_schedules(project_id: int | None = None):
    with _connect() as c:
        if project_id:
            rows = c.execute("SELECT * FROM schedules WHERE project_id=? ORDER BY id DESC", (project_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM schedules ORDER BY id DESC").fetchall()
    return [_schedule_row(r) for r in rows]


def get_schedule(sid: int):
    with _connect() as c:
        r = c.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    return _schedule_row(r) if r else None


def create_schedule(name, project_id, kind, target, cadence, at, weekday=0,
                    inventory_id=None, credential_id=None, extra_vars=None, created_by=""):
    nxt = compute_next_run(cadence, at, weekday)
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO schedules(name,project_id,kind,target,inventory_id,credential_id,"
            "extra_vars,cadence,at,weekday,enabled,created_by,next_run,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
            (name, project_id, kind, target, inventory_id, credential_id,
             json.dumps(extra_vars or {}), cadence, at, int(weekday), created_by, nxt, _now()))
        return cur.lastrowid


def update_schedule(sid: int, **fields):
    allowed = {"name", "target", "inventory_id", "credential_id", "cadence", "at",
               "weekday", "enabled", "extra_vars"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=?")
        vals.append(json.dumps(v) if k == "extra_vars" else (int(v) if k in ("enabled", "weekday") else v))
    if not sets:
        return
    with _connect() as c:
        c.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE id=?", (*vals, sid))
        r = c.execute("SELECT cadence,at,weekday FROM schedules WHERE id=?", (sid,)).fetchone()
        if r:
            c.execute("UPDATE schedules SET next_run=? WHERE id=?",
                      (compute_next_run(r["cadence"], r["at"], r["weekday"]), sid))


def delete_schedule(sid: int):
    with _connect() as c:
        c.execute("DELETE FROM schedules WHERE id=?", (sid,))


def due_schedules(now_ts: float | None = None):
    """Enabled schedules whose next_run is in the past."""
    now = now_ts if now_ts is not None else time.time()
    with _connect() as c:
        rows = c.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL "
                         "AND next_run <= ? ORDER BY next_run", (now,)).fetchall()
    return [_schedule_row(r) for r in rows]


def mark_schedule_fired(sid: int, run_id: int, status: str = "launched"):
    """Record a firing and advance next_run past now (so a missed window doesn't
    replay for every elapsed interval — it schedules the next future slot)."""
    with _connect() as c:
        r = c.execute("SELECT cadence,at,weekday FROM schedules WHERE id=?", (sid,)).fetchone()
        nxt = compute_next_run(r["cadence"], r["at"], r["weekday"]) if r else None
        c.execute("UPDATE schedules SET last_run=?, last_status=?, last_run_id=?, next_run=? WHERE id=?",
                  (_now(), status, run_id, nxt, sid))


# ---------------------------------------------------------------- infrastructure
def set_infra(project_id, provider, controller_id=None, ssh_user="", environment=""):
    with _connect() as c:
        c.execute("INSERT OR REPLACE INTO infra(project_id,provider,controller_id,ssh_user,environment,created) "
                  "VALUES(?,?,?,?,?,?)", (project_id, provider, controller_id, ssh_user, environment, _now()))


def get_infra(project_id):
    with _connect() as c:
        r = c.execute("SELECT * FROM infra WHERE project_id=?", (project_id,)).fetchone()
    return dict(r) if r else None


def list_infra():
    """Infra projects joined with their project name/slug, for the Infrastructure view."""
    with _connect() as c:
        rows = c.execute(
            "SELECT i.*, p.name AS project_name, p.slug AS project_slug "
            "FROM infra i JOIN projects p ON p.id = i.project_id ORDER BY i.created DESC").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- admins
def count_admins() -> int:
    with _connect() as c:
        return c.execute("SELECT COUNT(*) AS n FROM admins").fetchone()["n"]


def get_admin(username: str):
    with _connect() as c:
        r = c.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
        return dict(r) if r else None


def add_admin(username, pw_hash, salt, role="admin", must_change=0):
    with _connect() as c:
        c.execute(
            "INSERT INTO admins(username,pw_hash,salt,role,must_change_password,created)"
            " VALUES(?,?,?,?,?,?)",
            (username, pw_hash, salt, role, int(must_change), _now()),
        )


def set_admin_password(username, pw_hash, salt, must_change=0):
    with _connect() as c:
        c.execute(
            "UPDATE admins SET pw_hash=?, salt=?, must_change_password=? WHERE username=?",
            (pw_hash, salt, int(must_change), username),
        )


def list_admins():
    """Users for the RBAC admin view — never the password hash/salt."""
    with _connect() as c:
        return [{"username": r["username"], "role": r["role"],
                 "must_change_password": bool(r["must_change_password"]), "created": r["created"]}
                for r in c.execute("SELECT username,role,must_change_password,created "
                                   "FROM admins ORDER BY username")]


def set_admin_role(username, role):
    with _connect() as c:
        c.execute("UPDATE admins SET role=? WHERE username=?", (role, username))


def delete_admin(username):
    with _connect() as c:
        c.execute("DELETE FROM admins WHERE username=?", (username,))


def count_superusers():
    with _connect() as c:
        return c.execute("SELECT COUNT(*) AS n FROM admins WHERE role='superuser'").fetchone()["n"]


# ---------------------------------------------------------------- projects
def list_projects():
    with _connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY name").fetchall()]
    for d in rows:
        d["has_git_token"] = bool(d.get("git_token"))
        d.pop("git_token", None)
    return rows


def get_project(pid: int, include_token=False):
    with _connect() as c:
        r = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["has_git_token"] = bool(d.get("git_token"))
    if not include_token:
        d.pop("git_token", None)
    return d


def create_project(name, slug, description="", scm_url="", scm_branch=""):
    ts = _now()
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO projects(name,slug,description,scm_url,scm_branch,created,updated)"
            " VALUES(?,?,?,?,?,?,?)",
            (name, slug, description, scm_url, scm_branch, ts, ts),
        )
        pid = cur.lastrowid
    (PROJECTS_DIR / str(pid)).mkdir(parents=True, exist_ok=True)
    return pid


def touch_project(pid: int):
    with _connect() as c:
        c.execute("UPDATE projects SET updated=? WHERE id=?", (_now(), pid))


def set_project_scm(pid: int, scm_url=None, scm_branch=None, git_token=None):
    """Update a project's git remote URL / default branch / encrypted token.
    None leaves a field unchanged."""
    sets, vals = [], []
    if scm_url is not None:
        sets.append("scm_url=?"); vals.append(scm_url)
    if scm_branch is not None:
        sets.append("scm_branch=?"); vals.append(scm_branch)
    if git_token is not None:
        sets.append("git_token=?"); vals.append(git_token)
    if not sets:
        return
    vals.append(pid)
    with _connect() as c:
        c.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", vals)


def delete_project(pid: int):
    with _connect() as c:
        c.execute("DELETE FROM projects WHERE id=?", (pid,))


def project_dir(pid: int) -> Path:
    d = PROJECTS_DIR / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- credentials
def _strip_cred_secrets(d):
    d.pop("secret", None)
    # Never leak the ciphertext; expose only whether a become password is set.
    d["has_become"] = bool(d.pop("become_secret", "") or "")
    return d


def list_credentials(include_secret=False):
    with _connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM credentials ORDER BY name").fetchall()]
    if not include_secret:
        rows = [_strip_cred_secrets(r) for r in rows]
    return rows


def get_credential(cid: int, include_secret=False):
    with _connect() as c:
        r = c.execute("SELECT * FROM credentials WHERE id=?", (cid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    if not include_secret:
        d = _strip_cred_secrets(d)
    return d


def create_credential(name, kind="ssh", username="", secret="", become_secret=""):
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO credentials(name,kind,username,secret,become_secret,created) VALUES(?,?,?,?,?,?)",
            (name, kind, username, secret, become_secret, _now()),
        )
        return cur.lastrowid


def set_credential_become(cid: int, become_secret: str):
    """Set (or clear, with '') a credential's encrypted sudo/become password."""
    with _connect() as c:
        c.execute("UPDATE credentials SET become_secret=? WHERE id=?", (become_secret, cid))


def upsert_credential(name, kind="ssh", username="", secret="", become_secret=None):
    """Create a credential, or refresh an existing one with the same name in place
    (keeping its id, so runs already pointing at it keep working). Used by the
    'distribute SSH key' flow to (re)publish the SLEP managed key credential.
    `become_secret=None` preserves any already-set sudo password on update."""
    with _connect() as c:
        row = c.execute("SELECT id FROM credentials WHERE name=?", (name,)).fetchone()
        if row:
            if become_secret is None:
                c.execute("UPDATE credentials SET kind=?, username=?, secret=? WHERE id=?",
                          (kind, username, secret, row["id"]))
            else:
                c.execute("UPDATE credentials SET kind=?, username=?, secret=?, become_secret=? WHERE id=?",
                          (kind, username, secret, become_secret, row["id"]))
            return row["id"]
        cur = c.execute(
            "INSERT INTO credentials(name,kind,username,secret,become_secret,created) VALUES(?,?,?,?,?,?)",
            (name, kind, username, secret, become_secret or "", _now()),
        )
        return cur.lastrowid


def delete_credential(cid: int):
    with _connect() as c:
        c.execute("DELETE FROM credentials WHERE id=?", (cid,))


# ---------------------------------------------------------------- vault (secrets)
def list_secrets():
    """Names + timestamps only — never the (encrypted) value."""
    with _connect() as c:
        return [{"id": r["id"], "name": r["name"], "created": r["created"]}
                for r in c.execute("SELECT id,name,created FROM secrets ORDER BY name")]


def upsert_secret(name, ciphertext):
    """Store (or replace) a secret's ciphertext by name."""
    with _connect() as c:
        row = c.execute("SELECT id FROM secrets WHERE name=?", (name,)).fetchone()
        if row:
            c.execute("UPDATE secrets SET value=? WHERE id=?", (ciphertext, row["id"]))
            return row["id"]
        cur = c.execute("INSERT INTO secrets(name,value,created) VALUES(?,?,?)",
                        (name, ciphertext, _now()))
        return cur.lastrowid


def delete_secret(sid: int):
    with _connect() as c:
        c.execute("DELETE FROM secrets WHERE id=?", (sid,))


def all_secret_ciphertexts():
    """[(name, ciphertext)] for injecting the vault into a run."""
    with _connect() as c:
        return [(r["name"], r["value"]) for r in c.execute("SELECT name,value FROM secrets")]


# ---------------------------------------------------------------- controllers
def list_controllers(include_key=False):
    with _connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM controllers ORDER BY name").fetchall()]
    if not include_key:
        for r in rows:
            r.pop("api_key", None)
    return rows


def get_controller(cid: int, include_key=False):
    with _connect() as c:
        r = c.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    if not include_key:
        d.pop("api_key", None)
    return d


def create_controller(name, base_url, api_key):
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO controllers(name,base_url,api_key,created) VALUES(?,?,?,?)",
            (name, base_url, api_key, _now()),
        )
        return cur.lastrowid


def delete_controller(cid: int):
    with _connect() as c:
        c.execute("DELETE FROM controllers WHERE id=?", (cid,))


def set_controller_last_import(cid: int):
    with _connect() as c:
        c.execute("UPDATE controllers SET last_import=? WHERE id=?", (_now(), cid))


# ---------------------------------------------------------------- inventories & hosts
def list_inventories(project_id=None):
    with _connect() as c:
        if project_id is None:
            rows = c.execute("SELECT * FROM inventories ORDER BY name").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM inventories WHERE project_id=? ORDER BY name", (project_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_inventory(iid: int):
    with _connect() as c:
        r = c.execute("SELECT * FROM inventories WHERE id=?", (iid,)).fetchone()
        return dict(r) if r else None


def create_inventory(name, project_id=None, source="manual", bastion=""):
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO inventories(project_id,name,source,bastion,created) VALUES(?,?,?,?,?)",
            (project_id, name, source, bastion, _now()),
        )
        return cur.lastrowid


def set_inventory_bastion(iid: int, bastion: str):
    with _connect() as c:
        c.execute("UPDATE inventories SET bastion=? WHERE id=?", (bastion, iid))


def delete_inventory(iid: int):
    with _connect() as c:
        c.execute("DELETE FROM inventories WHERE id=?", (iid,))


def list_hosts(inventory_id: int):
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM hosts WHERE inventory_id=? ORDER BY name", (inventory_id,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["variables"] = json.loads(d.get("variables") or "{}")
        except (TypeError, ValueError):
            d["variables"] = {}
        out.append(d)
    return out


def add_host(inventory_id, name, address, groups="", variables=None, source="manual"):
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO hosts(inventory_id,name,address,groups,variables,source,created)"
            " VALUES(?,?,?,?,?,?,?)",
            (inventory_id, name, address, groups, json.dumps(variables or {}), source, _now()),
        )
        return cur.lastrowid


def upsert_host(inventory_id, name, address, groups="", variables=None, source="controller"):
    """Insert or update a host by (inventory_id, name) — used by the Controller
    import so re-importing refreshes addresses/groups instead of duplicating."""
    with _connect() as c:
        existing = c.execute(
            "SELECT id FROM hosts WHERE inventory_id=? AND name=?", (inventory_id, name)
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE hosts SET address=?, groups=?, variables=?, source=? WHERE id=?",
                (address, groups, json.dumps(variables or {}), source, existing["id"]),
            )
            return existing["id"]
        cur = c.execute(
            "INSERT INTO hosts(inventory_id,name,address,groups,variables,source,created)"
            " VALUES(?,?,?,?,?,?,?)",
            (inventory_id, name, address, groups, json.dumps(variables or {}), source, _now()),
        )
        return cur.lastrowid


def delete_host(hid: int):
    with _connect() as c:
        c.execute("DELETE FROM hosts WHERE id=?", (hid,))


# ---------------------------------------------------------------- runs
def create_run(project_id, kind, target, inventory_id=None, credential_id=None,
               extra_vars=None, created_by="", group_id=""):
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO runs(project_id,kind,target,inventory_id,credential_id,extra_vars,"
            "status,created_by,created,group_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (project_id, kind, target, inventory_id, credential_id,
             json.dumps(extra_vars or {}), "queued", created_by, _now(), group_id or ""),
        )
        return cur.lastrowid


def runs_in_group(group_id: str):
    """All runs of one launched pipeline, in launch order — for the sequence viz."""
    if not group_id:
        return []
    with _connect() as c:
        rows = c.execute("SELECT * FROM runs WHERE group_id=? ORDER BY id ASC", (group_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- saved pipelines
def create_pipeline(project_id, name, steps, stop_on_failure=True):
    ts = _now()
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO pipelines(project_id,name,steps,stop_on_failure,created,updated)"
            " VALUES(?,?,?,?,?,?)",
            (project_id, name, json.dumps(steps or []), 1 if stop_on_failure else 0, ts, ts),
        )
        return cur.lastrowid


def update_pipeline(pipeline_id, name=None, steps=None, stop_on_failure=None):
    sets, args = [], []
    if name is not None:
        sets.append("name=?"); args.append(name)
    if steps is not None:
        sets.append("steps=?"); args.append(json.dumps(steps))
    if stop_on_failure is not None:
        sets.append("stop_on_failure=?"); args.append(1 if stop_on_failure else 0)
    if not sets:
        return
    sets.append("updated=?"); args.append(_now())
    args.append(pipeline_id)
    with _connect() as c:
        c.execute(f"UPDATE pipelines SET {', '.join(sets)} WHERE id=?", args)


def delete_pipeline(pipeline_id):
    with _connect() as c:
        c.execute("DELETE FROM pipelines WHERE id=?", (pipeline_id,))


def _pipeline_row(r):
    d = dict(r)
    try:
        d["steps"] = json.loads(d.get("steps") or "[]")
    except (TypeError, ValueError):
        d["steps"] = []
    d["stop_on_failure"] = bool(d.get("stop_on_failure"))
    return d


def get_pipeline(pipeline_id):
    with _connect() as c:
        r = c.execute("SELECT * FROM pipelines WHERE id=?", (pipeline_id,)).fetchone()
        return _pipeline_row(r) if r else None


def list_pipelines():
    with _connect() as c:
        rows = c.execute(
            "SELECT p.*, pr.name AS project_name, pr.slug AS project_slug FROM pipelines p"
            " JOIN projects pr ON pr.id=p.project_id ORDER BY p.updated DESC"
        ).fetchall()
        return [_pipeline_row(r) for r in rows]


def set_run_status(run_id, status, exit_code=None, started=None, finished=None):
    sets, args = ["status=?"], [status]
    if exit_code is not None:
        sets.append("exit_code=?"); args.append(exit_code)
    if started is not None:
        sets.append("started=?"); args.append(started)
    if finished is not None:
        sets.append("finished=?"); args.append(finished)
    args.append(run_id)
    with _connect() as c:
        c.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", args)


def get_run(run_id: int):
    with _connect() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None


def list_runs(project_id=None, limit=100):
    with _connect() as c:
        if project_id is None:
            rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM runs WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def run_log_path(run_id: int) -> Path:
    return RUNS_DIR / f"{run_id}.log"
