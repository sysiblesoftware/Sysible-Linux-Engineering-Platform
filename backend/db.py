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

import json
import os
import sqlite3
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
            """
        )
        c.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


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


def create_inventory(name, project_id=None, source="manual"):
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO inventories(project_id,name,source,created) VALUES(?,?,?,?)",
            (project_id, name, source, _now()),
        )
        return cur.lastrowid


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
