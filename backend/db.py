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
        return [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY name").fetchall()]


def get_project(pid: int):
    with _connect() as c:
        r = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


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


def delete_project(pid: int):
    with _connect() as c:
        c.execute("DELETE FROM projects WHERE id=?", (pid,))


def project_dir(pid: int) -> Path:
    d = PROJECTS_DIR / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- credentials
def list_credentials(include_secret=False):
    with _connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM credentials ORDER BY name").fetchall()]
    if not include_secret:
        for r in rows:
            r.pop("secret", None)
    return rows


def get_credential(cid: int, include_secret=False):
    with _connect() as c:
        r = c.execute("SELECT * FROM credentials WHERE id=?", (cid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    if not include_secret:
        d.pop("secret", None)
    return d


def create_credential(name, kind="ssh", username="", secret=""):
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO credentials(name,kind,username,secret,created) VALUES(?,?,?,?,?)",
            (name, kind, username, secret, _now()),
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
               extra_vars=None, created_by=""):
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO runs(project_id,kind,target,inventory_id,credential_id,extra_vars,"
            "status,created_by,created) VALUES(?,?,?,?,?,?,?,?,?)",
            (project_id, kind, target, inventory_id, credential_id,
             json.dumps(extra_vars or {}), "queued", created_by, _now()),
        )
        return cur.lastrowid


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
