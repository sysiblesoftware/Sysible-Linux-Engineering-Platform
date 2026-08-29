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
                name TEXT NOT NULL,             -- unique PER org, not globally (see the UNIQUE(org_id,name) index below)
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

            -- Reusable SSH jump hosts (bastions). Defined once, prepared once (SLEP's
            -- managed key installed on them), then picked per project by name instead
            -- of retyping user@host. `prepared` is the epoch we last installed the key.
            CREATE TABLE IF NOT EXISTS jump_hosts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT 'root',
                port INTEGER NOT NULL DEFAULT 22,
                org_id INTEGER,
                prepared INTEGER,
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
                inventory_id INTEGER,          -- target inventory for applied VMs (NULL = a dedicated '<name> (VMs)')
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

            -- Organizations (multi-tenancy): the top-level container that owns
            -- projects, inventories, credentials and controllers. A user's
            -- effective role in an org is the highest of their direct grant
            -- (org_members) and any team they belong to (teams.org_role). The
            -- global 'superuser' is the system admin and bypasses org checks.
            CREATE TABLE IF NOT EXISTS organizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '',
                created INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS org_members (
                org_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',   -- admin | operator | viewer
                PRIMARY KEY (org_id, username),
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE,
                FOREIGN KEY (username) REFERENCES admins(username) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                org_role TEXT NOT NULL DEFAULT 'viewer',  -- role the team confers in its org
                created INTEGER NOT NULL,
                UNIQUE (org_id, name),
                FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS team_members (
                team_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                PRIMARY KEY (team_id, username),
                FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE,
                FOREIGN KEY (username) REFERENCES admins(username) ON DELETE CASCADE
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
        # Environment (dev / staging / prod / …) groups inventories in the list.
        if "environment" not in inv_cols:
            c.execute("ALTER TABLE inventories ADD COLUMN environment TEXT DEFAULT ''")
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
        infra_cols = [r["name"] for r in c.execute("PRAGMA table_info(infra)")]
        if "inventory_id" not in infra_cols:
            c.execute("ALTER TABLE infra ADD COLUMN inventory_id INTEGER")
        # The hypervisor's SSH jump host (user@host[:port]) for reaching the VMs:
        # freshly-applied VMs sit on the hypervisor's private network, which SLEP
        # can't route to directly — but it can reach the hypervisor, so that's the
        # bastion the built inventory uses.
        if "bastion" not in infra_cols:
            c.execute("ALTER TABLE infra ADD COLUMN bastion TEXT DEFAULT ''")
        # The Vault variable NAME (never the plaintext) of the login password set on
        # the VMs, if any. Stored so a post-apply reachability check can resolve it on
        # demand and distribute SLEP's key over the password login when key auth to a
        # fresh VM hasn't taken yet. Only the reference is kept; the secret lives in
        # the vault, encrypted.
        if "ssh_password_ref" not in infra_cols:
            c.execute("ALTER TABLE infra ADD COLUMN ssh_password_ref TEXT DEFAULT ''")
        # The wizard OPTIONS the project was generated from (JSON), so an "Edit infra"
        # can re-open the wizard pre-filled and regenerate — e.g. after a destroy, add a
        # VM type. Secrets are NOT kept here (ssh_password is stored encrypted separately).
        if "options_json" not in infra_cols:
            c.execute("ALTER TABLE infra ADD COLUMN options_json TEXT DEFAULT ''")
        # The SSH credential whose PUBLIC key is baked into the VMs (so that
        # credential can log in) — settable after create from the ⚙ Access editor,
        # not only at wizard time.
        if "deploy_credential_id" not in infra_cols:
            c.execute("ALTER TABLE infra ADD COLUMN deploy_credential_id INTEGER")
        # The VMs' login password, ENCRYPTED at rest (vault.encrypt), so a post-apply
        # reachability check / "Fix SSH" can log in and install the current key —
        # whether the operator entered a literal or a Vault variable. Never plaintext.
        if "ssh_password_enc" not in infra_cols:
            c.execute("ALTER TABLE infra ADD COLUMN ssh_password_enc TEXT DEFAULT ''")
        # A literal public key pasted in ⚙ Access (instead of, or besides, picking a
        # stored credential) — baked into the VMs' cloud-init so its holder can log in.
        if "deploy_public_key" not in infra_cols:
            c.execute("ALTER TABLE infra ADD COLUMN deploy_public_key TEXT DEFAULT ''")
        # The auto-maintained password credential for the project's login account
        # (username + password from ⚙ Access) — the Controller-style single account
        # used to log in AND for controlled sudo. Ansible/Salt default to it.
        if "login_credential_id" not in infra_cols:
            c.execute("ALTER TABLE infra ADD COLUMN login_credential_id INTEGER")
        # Multi-tenancy: every owned resource carries an org_id. Add the column
        # where missing, then adopt any orphaned rows + existing users into a
        # 'Default' organization so pre-tenancy installs keep working unchanged.
        for tbl in ("projects", "inventories", "credentials", "controllers", "secrets"):
            cols = [r["name"] for r in c.execute(f"PRAGMA table_info({tbl})")]
            if "org_id" not in cols:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN org_id INTEGER")
        # Secret identity is ORG-SCOPED: a name is unique WITHIN an org, never globally.
        # Older DBs created the table with a column-level `name TEXT UNIQUE` (an implicit
        # autoindex) that both blocked a second org reusing a name AND let an upsert-by-
        # name overwrite another tenant's secret of the same name (cross-org poisoning).
        # Rebuild the table without that global constraint, then enforce UNIQUE(org_id,
        # name) via an explicit index. Names are globally unique on legacy rows, so the
        # (org_id,name) pairs can never collide during this migration.
        sec_idx = [r["name"] for r in c.execute("PRAGMA index_list(secrets)")]
        if any(ix.startswith("sqlite_autoindex_secrets") for ix in sec_idx):
            c.executescript(
                """
                CREATE TABLE secrets_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    value TEXT NOT NULL,
                    created INTEGER NOT NULL,
                    org_id INTEGER
                );
                INSERT INTO secrets_new(id,name,value,created,org_id)
                    SELECT id,name,value,created,org_id FROM secrets;
                DROP TABLE secrets;
                ALTER TABLE secrets_new RENAME TO secrets;
                """
            )
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_secrets_org_name ON secrets(org_id, name)")
        # A pinned TLS certificate (PEM) for a self-signed / on-prem Controller, captured
        # trust-on-first-use when connecting, so SLEP verifies against it instead of the
        # public CA store — no manual cert copying, no blanket insecure mode.
        ctrl_cols = [r["name"] for r in c.execute("PRAGMA table_info(controllers)")]
        if "tls_cert" not in ctrl_cols:
            c.execute("ALTER TABLE controllers ADD COLUMN tls_cert TEXT DEFAULT ''")
        _seed_default_org(c)
        # Project hierarchy: a project may nest under a parent project (folders /
        # sub-projects). NULL parent_id = a top-level project.
        proj_cols2 = [r["name"] for r in c.execute("PRAGMA table_info(projects)")]
        if "parent_id" not in proj_cols2:
            c.execute("ALTER TABLE projects ADD COLUMN parent_id INTEGER")
        # One-time: encrypt any credential/controller secrets still stored as
        # plaintext (rows written before at-rest encryption). Idempotent.
        _encrypt_legacy_secrets(c)
    # The DB holds password hashes, session tokens, encrypted vault + Controller
    # keys — keep it owner-only so a stray world-read can't harvest them.
    _restrict_db_permissions()


def _seed_default_org(c) -> int:
    """Ensure a 'Default' organization exists, and adopt any resources or users
    that predate multi-tenancy into it. Idempotent — safe on every boot. Returns
    the Default org's id."""
    row = c.execute("SELECT id FROM organizations WHERE slug='default'").fetchone()
    if row:
        oid = row["id"]
    else:
        c.execute("INSERT INTO organizations(name, slug, description, created) "
                  "VALUES('Default','default','Default organization',?)", (_now(),))
        oid = c.execute("SELECT id FROM organizations WHERE slug='default'").fetchone()["id"]
    # Adopt any resource rows written before org_id existed.
    for tbl in ("projects", "inventories", "credentials", "controllers", "secrets"):
        c.execute(f"UPDATE {tbl} SET org_id=? WHERE org_id IS NULL", (oid,))
    # Adopt existing admins as members of Default, mapping their global role:
    # superuser/legacy-admin -> org admin, operator -> operator, viewer -> viewer.
    for a in c.execute("SELECT username, role FROM admins").fetchall():
        role = "operator"
        if a["role"] in ("superuser", "admin"):
            role = "admin"
        elif a["role"] == "viewer":
            role = "viewer"
        c.execute("INSERT OR IGNORE INTO org_members(org_id, username, role) VALUES(?,?,?)",
                  (oid, a["username"], role))
    return oid


def _encrypt_legacy_secrets(c) -> None:
    """Encrypt credential/controller secrets that are still stored as plaintext
    (rows written before at-rest encryption landed). A value that already decrypts
    is left as-is, so this is safe to run on every startup."""
    from . import vault

    def _needs_enc(v):
        if not v:
            return False
        try:
            vault.decrypt(v)
            return False            # already ciphertext
        except Exception:  # noqa: BLE001
            return True             # legacy plaintext
    for tbl, cols in (("credentials", ("secret", "become_secret")), ("controllers", ("api_key",))):
        for r in c.execute(f"SELECT id, {', '.join(cols)} FROM {tbl}").fetchall():
            updates = {col: vault.encrypt(r[col]) for col in cols if _needs_enc(r[col])}
            if updates:
                sets = ", ".join(f"{k}=?" for k in updates)
                c.execute(f"UPDATE {tbl} SET {sets} WHERE id=?", (*updates.values(), r["id"]))


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


def list_schedules(project_id: int | None = None, org_ids=None):
    """Schedules, newest first. `org_ids` (a list) restricts to schedules whose project
    is in those orgs (or legacy-unowned); None = unscoped (system admin / the scheduler
    thread); empty list = nothing. Without this a viewer could read every tenant's
    schedules (targets, cred/inventory ids, unmasked extra_vars) via GET /schedules."""
    with _connect() as c:
        where, params = [], []
        if project_id:
            where.append("project_id=?")
            params.append(project_id)
        if org_ids is not None:
            if not org_ids:
                return []
            ph = ",".join("?" * len(org_ids))
            where.append(f"project_id IN (SELECT id FROM projects WHERE org_id IN ({ph}) OR org_id IS NULL)")
            params.extend(org_ids)
        sql = "SELECT * FROM schedules"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        rows = c.execute(sql, tuple(params)).fetchall()
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
def set_infra(project_id, provider, controller_id=None, ssh_user="", environment="", inventory_id=None, bastion=""):
    with _connect() as c:
        c.execute("INSERT OR REPLACE INTO infra(project_id,provider,controller_id,ssh_user,environment,inventory_id,bastion,created) "
                  "VALUES(?,?,?,?,?,?,?,?)", (project_id, provider, controller_id, ssh_user, environment, inventory_id, bastion, _now()))


def get_infra(project_id):
    with _connect() as c:
        r = c.execute("SELECT * FROM infra WHERE project_id=?", (project_id,)).fetchone()
    return dict(r) if r else None


def set_infra_inventory(project_id, inventory_id):
    """Pin the infra project's inventory without disturbing its other fields — so
    the first inventory the builder resolves becomes canonical and every later
    apply/enroll/pipeline step reuses it instead of creating a second one."""
    with _connect() as c:
        c.execute("UPDATE infra SET inventory_id=? WHERE project_id=?", (inventory_id, project_id))


def set_infra_ssh_user(project_id, ssh_user):
    """Set the infra project's login user without disturbing its other fields."""
    with _connect() as c:
        c.execute("UPDATE infra SET ssh_user=? WHERE project_id=?", (ssh_user or "", project_id))


def set_infra_environment(project_id, environment):
    """Set the infra project's environment tag without disturbing its other fields."""
    with _connect() as c:
        c.execute("UPDATE infra SET environment=? WHERE project_id=?", (environment or "", project_id))


def set_infra_controller(project_id, controller_id):
    """Point the infra project at a Controller (or None to clear a dangling reference)
    without disturbing its other fields."""
    with _connect() as c:
        c.execute("UPDATE infra SET controller_id=? WHERE project_id=?", (controller_id, project_id))


def set_infra_options(project_id, options_json):
    """Persist the wizard options JSON (no secrets) so an Edit can pre-fill + regenerate.
    A targeted UPDATE — never the lossy INSERT-OR-REPLACE set_infra."""
    with _connect() as c:
        c.execute("UPDATE infra SET options_json=? WHERE project_id=?", (options_json or "", project_id))


def set_infra_bastion(project_id, bastion):
    """Set the infra project's SSH jump host (hypervisor) without disturbing its
    other fields. Designated once at the project level and pushed to its
    inventories by the caller."""
    with _connect() as c:
        c.execute("UPDATE infra SET bastion=? WHERE project_id=?", (bastion or "", project_id))


def set_infra_ssh_password_ref(project_id, ref):
    """Remember the Vault variable NAME of the VMs' login password (never the
    plaintext) so a post-apply reachability check can resolve it and distribute
    SLEP's key over the password login. Empty clears it."""
    with _connect() as c:
        c.execute("UPDATE infra SET ssh_password_ref=? WHERE project_id=?", (ref or "", project_id))


def set_infra_deploy_credential(project_id, credential_id):
    """Set the SSH credential whose public key is baked into the VMs. None clears it."""
    with _connect() as c:
        c.execute("UPDATE infra SET deploy_credential_id=? WHERE project_id=?", (credential_id, project_id))


def set_infra_ssh_password_enc(project_id, ciphertext):
    """Store the VMs' login password ENCRYPTED (caller encrypts) so reachability /
    'Fix SSH' can reuse it. Empty clears it."""
    with _connect() as c:
        c.execute("UPDATE infra SET ssh_password_enc=? WHERE project_id=?", (ciphertext or "", project_id))


def set_infra_deploy_public_key(project_id, pubkey):
    """Store a literal public key to bake into the VMs (alongside/instead of a
    credential). Empty clears it."""
    with _connect() as c:
        c.execute("UPDATE infra SET deploy_public_key=? WHERE project_id=?", (pubkey or "", project_id))


def set_infra_login_credential(project_id, credential_id):
    """Point the infra at its auto-maintained login (username+password) credential."""
    with _connect() as c:
        c.execute("UPDATE infra SET login_credential_id=? WHERE project_id=?", (credential_id, project_id))


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
def list_projects(org_ids=None):
    """All projects, or — when `org_ids` is a list — only those in the given
    organizations (empty list → nothing). None means unscoped (system view)."""
    with _connect() as c:
        if org_ids is None:
            rows = c.execute("SELECT * FROM projects ORDER BY name").fetchall()
        elif not org_ids:
            return []
        else:
            ph = ",".join("?" * len(org_ids))
            rows = c.execute(f"SELECT * FROM projects WHERE (org_id IN ({ph}) OR org_id IS NULL) ORDER BY name",
                             tuple(org_ids)).fetchall()
        rows = [dict(r) for r in rows]
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


def create_project(name, slug, description="", scm_url="", scm_branch="", org_id=None, parent_id=None):
    ts = _now()
    # A sub-project lives in the same org as its parent; only a top-level project
    # takes an explicit (or Default) org.
    if parent_id is not None:
        parent = get_project(parent_id)
        org_id = (parent or {}).get("org_id") or org_id
    if org_id is None:
        org_id = default_org_id()
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO projects(name,slug,description,scm_url,scm_branch,org_id,parent_id,created,updated)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (name, slug, description, scm_url, scm_branch, org_id, parent_id, ts, ts),
        )
        pid = cur.lastrowid
    (PROJECTS_DIR / str(pid)).mkdir(parents=True, exist_ok=True)
    return pid


def set_project_parent(pid: int, parent_id):
    """Move a project under a new parent (None = make it top-level). Refuses to
    create a cycle (a project can't become its own ancestor)."""
    if parent_id is not None:
        if int(parent_id) == int(pid):
            raise ValueError("A project can't be its own parent.")
        # Walk up from the proposed parent; if we reach pid, this would loop.
        seen, cur_id = set(), parent_id
        while cur_id is not None and cur_id not in seen:
            seen.add(cur_id)
            row = get_project(cur_id)
            if not row:
                break
            if row.get("parent_id") == pid:
                raise ValueError("That move would create a project cycle.")
            cur_id = row.get("parent_id")
    with _connect() as c:
        c.execute("UPDATE projects SET parent_id=?, updated=? WHERE id=?", (parent_id, _now(), pid))


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
        # Promote any sub-projects up to this project's parent so they aren't
        # orphaned (deleting a folder shouldn't silently delete its contents).
        row = c.execute("SELECT parent_id FROM projects WHERE id=?", (pid,)).fetchone()
        new_parent = row["parent_id"] if row else None
        c.execute("UPDATE projects SET parent_id=? WHERE parent_id=?", (new_parent, pid))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))


def project_dir(pid: int) -> Path:
    d = PROJECTS_DIR / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- credentials
# Secrets are encrypted at rest (the DB only ever holds ciphertext) via the vault
# key. Callers pass/consume PLAINTEXT — encryption happens on write, decryption on
# an include_secret/include_key read. Legacy rows written before at-rest encryption
# are plaintext; _dec returns those as-is (no migration needed).
def _enc(s) -> str:
    if not s:
        return ""
    from . import vault
    return vault.encrypt(str(s))


def _dec(s) -> str:
    if not s:
        return ""
    from . import vault
    s = str(s)
    try:
        return vault.decrypt(s)
    except Exception:  # noqa: BLE001
        # A value that LOOKS like a Fernet token (starts with the version+timestamp
        # prefix) but won't decrypt is corruption or a wrong/rotated key — fail CLOSED
        # (empty) rather than hand back ciphertext as if it were the secret. A value
        # that isn't a token is genuine legacy pre-encryption plaintext, returned as-is.
        if s.startswith("gAAAAA"):
            return ""
        return s


def _strip_cred_secrets(d):
    d.pop("secret", None)
    # Never leak the ciphertext; expose only whether a become password is set.
    d["has_become"] = bool(d.pop("become_secret", "") or "")
    return d


def _dec_cred(d):
    """Decrypt a credential's stored secrets in place (for include_secret reads)."""
    d["secret"] = _dec(d.get("secret", ""))
    d["become_secret"] = _dec(d.get("become_secret", ""))
    return d


def list_credentials(include_secret=False, org_ids=None):
    with _connect() as c:
        if org_ids is None:
            rows = c.execute("SELECT * FROM credentials ORDER BY name").fetchall()
        elif not org_ids:
            return []
        else:
            ph = ",".join("?" * len(org_ids))
            rows = c.execute(f"SELECT * FROM credentials WHERE (org_id IN ({ph}) OR org_id IS NULL) ORDER BY name",
                             tuple(org_ids)).fetchall()
        rows = [dict(r) for r in rows]
    if include_secret:
        return [_dec_cred(r) for r in rows]
    return [_strip_cred_secrets(r) for r in rows]


def get_credential(cid: int, include_secret=False):
    with _connect() as c:
        r = c.execute("SELECT * FROM credentials WHERE id=?", (cid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    return _dec_cred(d) if include_secret else _strip_cred_secrets(d)


def create_credential(name, kind="ssh", username="", secret="", become_secret="", org_id=None):
    if org_id is None:
        org_id = default_org_id()
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO credentials(name,kind,username,secret,become_secret,org_id,created) VALUES(?,?,?,?,?,?,?)",
            (name, kind, username, _enc(secret), _enc(become_secret), org_id, _now()),
        )
        return cur.lastrowid


def set_credential_become(cid: int, become_secret: str):
    """Set (or clear, with '') a credential's sudo/become password (encrypted at rest)."""
    with _connect() as c:
        c.execute("UPDATE credentials SET become_secret=? WHERE id=?", (_enc(become_secret), cid))


def set_credential_secret(cid: int, secret: str):
    """Replace a credential's private key/secret (encrypted at rest), leaving its
    name/username/id untouched — used to keep the managed-key credential in step
    with the on-disk key without disturbing runs that point at it."""
    with _connect() as c:
        c.execute("UPDATE credentials SET secret=? WHERE id=?", (_enc(secret), cid))


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
                          (kind, username, _enc(secret), row["id"]))
            else:
                c.execute("UPDATE credentials SET kind=?, username=?, secret=?, become_secret=? WHERE id=?",
                          (kind, username, _enc(secret), _enc(become_secret), row["id"]))
            return row["id"]
        cur = c.execute(
            "INSERT INTO credentials(name,kind,username,secret,become_secret,created) VALUES(?,?,?,?,?,?)",
            (name, kind, username, _enc(secret), _enc(become_secret or ""), _now()),
        )
        return cur.lastrowid


def delete_credential(cid: int):
    with _connect() as c:
        c.execute("DELETE FROM credentials WHERE id=?", (cid,))


# ---------------------------------------------------------------- vault (secrets)
def _org_filter(org_ids):
    """(sql_fragment, params) restricting to org_ids (+ legacy NULL-org rows). Returns
    (None, None) to mean 'no rows' for an empty list, and ('', ()) for None = all."""
    if org_ids is None:
        return "", ()
    if not org_ids:
        return None, None
    ph = ",".join("?" * len(org_ids))
    return f"(org_id IN ({ph}) OR org_id IS NULL)", tuple(org_ids)


def list_secrets(org_ids=None):
    """Names + timestamps only — never the (encrypted) value. Scoped to org_ids (a
    system admin passes None = all)."""
    frag, params = _org_filter(org_ids)
    if frag is None:
        return []
    with _connect() as c:
        q = "SELECT id,name,created FROM secrets" + (f" WHERE {frag}" if frag else "") + " ORDER BY name"
        return [{"id": r["id"], "name": r["name"], "created": r["created"]}
                for r in c.execute(q, params)]


def upsert_secret(name, ciphertext, org_id=None):
    """Store (or replace) a secret's ciphertext by (org_id, name). Secret identity is
    ORG-SCOPED (UNIQUE(org_id, name)): the match is on BOTH name and org, so an upsert
    only ever touches a row in the SAME org and can never overwrite another tenant's
    secret of the same name. `org_id IS ?` matches a legacy NULL-org row correctly."""
    with _connect() as c:
        row = c.execute("SELECT id FROM secrets WHERE name=? AND org_id IS ?",
                        (name, org_id)).fetchone()
        if row:
            c.execute("UPDATE secrets SET value=? WHERE id=?", (ciphertext, row["id"]))
            return row["id"]
        cur = c.execute("INSERT INTO secrets(name,value,created,org_id) VALUES(?,?,?,?)",
                        (name, ciphertext, _now(), org_id))
        return cur.lastrowid


# ---------------------------------------------------------------- jump hosts
def _jump_bastion(row) -> str:
    """The user@host[:port] string a row represents (port omitted when 22)."""
    if not row:
        return ""
    hp = row["host"] if int(row["port"] or 22) == 22 else f"{row['host']}:{row['port']}"
    return f"{row['username']}@{hp}" if row["username"] else hp


def list_jump_hosts(org_ids=None):
    frag, params = _org_filter(org_ids)
    if frag is None:
        return []
    with _connect() as c:
        q = "SELECT * FROM jump_hosts" + (f" WHERE {frag}" if frag else "") + " ORDER BY name"
        rows = [dict(r) for r in c.execute(q, params)]
    for r in rows:
        r["bastion"] = _jump_bastion(r)
    return rows


def get_jump_host(jid: int):
    with _connect() as c:
        r = c.execute("SELECT * FROM jump_hosts WHERE id=?", (jid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["bastion"] = _jump_bastion(d)
    return d


def create_jump_host(name, host, username="root", port=22, org_id=None):
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO jump_hosts(name,host,username,port,org_id,created) VALUES(?,?,?,?,?,?)",
            (name, host, username or "root", int(port or 22), org_id, _now()))
        return cur.lastrowid


def set_jump_host_prepared(jid: int, ts=None):
    with _connect() as c:
        c.execute("UPDATE jump_hosts SET prepared=? WHERE id=?", (ts if ts is not None else _now(), jid))


def delete_jump_host(jid: int):
    with _connect() as c:
        c.execute("DELETE FROM jump_hosts WHERE id=?", (jid,))


def get_secret_org(sid: int):
    """The org_id of a secret (None if the secret doesn't exist or is legacy-unowned)."""
    with _connect() as c:
        r = c.execute("SELECT org_id FROM secrets WHERE id=?", (sid,)).fetchone()
        return (r["org_id"] if r else None)


def delete_secret(sid: int):
    with _connect() as c:
        c.execute("DELETE FROM secrets WHERE id=?", (sid,))


def all_secret_ciphertexts(org_ids=None):
    """[(name, ciphertext)] for injecting the vault into a run — scoped to org_ids so a
    run only ever sees its own org's secrets. None = all (system-internal callers)."""
    frag, params = _org_filter(org_ids)
    if frag is None:
        return []
    with _connect() as c:
        q = "SELECT name,value FROM secrets" + (f" WHERE {frag}" if frag else "")
        return [(r["name"], r["value"]) for r in c.execute(q, params)]


# ---------------------------------------------------------------- controllers
def list_controllers(include_key=False, org_ids=None):
    with _connect() as c:
        if org_ids is None:
            rows = c.execute("SELECT * FROM controllers ORDER BY name").fetchall()
        elif not org_ids:
            return []
        else:
            ph = ",".join("?" * len(org_ids))
            rows = c.execute(f"SELECT * FROM controllers WHERE (org_id IN ({ph}) OR org_id IS NULL) ORDER BY name",
                             tuple(org_ids)).fetchall()
        rows = [dict(r) for r in rows]
    if not include_key:
        for r in rows:
            r.pop("api_key", None)
        return rows
    for r in rows:
        r["api_key"] = _dec(r.get("api_key", ""))
    return rows


def get_controller(cid: int, include_key=False):
    with _connect() as c:
        r = c.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    if not include_key:
        d.pop("api_key", None)
    else:
        d["api_key"] = _dec(d.get("api_key", ""))
    return d


def create_controller(name, base_url, api_key, org_id=None, tls_cert=""):
    if org_id is None:
        org_id = default_org_id()
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO controllers(name,base_url,api_key,org_id,created,tls_cert) VALUES(?,?,?,?,?,?)",
            (name, base_url, _enc(api_key), org_id, _now(), tls_cert or ""),   # api_key encrypted at rest
        )
        return cur.lastrowid


def set_controller_tls_cert(cid: int, tls_cert: str):
    """Pin (or clear) the Controller's TLS certificate PEM, without touching other fields."""
    with _connect() as c:
        c.execute("UPDATE controllers SET tls_cert=? WHERE id=?", (tls_cert or "", cid))


def delete_controller(cid: int):
    with _connect() as c:
        c.execute("DELETE FROM controllers WHERE id=?", (cid,))


def set_controller_last_import(cid: int):
    with _connect() as c:
        c.execute("UPDATE controllers SET last_import=? WHERE id=?", (_now(), cid))


# ---------------------------------------------------------------- inventories & hosts
def list_inventories(project_id=None, org_ids=None):
    with _connect() as c:
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id=?"); params.append(project_id)
        if org_ids is not None:
            if not org_ids:
                return []
            clauses.append(f"(org_id IN ({','.join('?' * len(org_ids))}) OR org_id IS NULL)"); params.extend(org_ids)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = c.execute(f"SELECT * FROM inventories{where} ORDER BY name", tuple(params)).fetchall()
        return [dict(r) for r in rows]


def get_inventory(iid: int):
    with _connect() as c:
        r = c.execute("SELECT * FROM inventories WHERE id=?", (iid,)).fetchone()
        return dict(r) if r else None


def create_inventory(name, project_id=None, source="manual", bastion="", org_id=None, environment=""):
    if org_id is None:
        # Inherit the project's org when attached, else the Default org.
        if project_id is not None:
            p = get_project(project_id)
            org_id = (p or {}).get("org_id") or default_org_id()
        else:
            org_id = default_org_id()
    with _connect() as c:
        cur = c.execute(
            "INSERT INTO inventories(project_id,name,source,bastion,org_id,environment,created) VALUES(?,?,?,?,?,?,?)",
            (project_id, name, source, bastion, org_id, environment or "", _now()),
        )
        return cur.lastrowid


def set_inventory_environment(iid: int, environment: str):
    with _connect() as c:
        c.execute("UPDATE inventories SET environment=? WHERE id=?", (environment or "", iid))


def find_inventory(project_id, source):
    """First inventory for a project with a given source (e.g. 'infra') — lets the
    'build inventory from applied VMs' action reuse/refresh one instead of piling
    up duplicates on every apply."""
    with _connect() as c:
        r = c.execute(
            "SELECT * FROM inventories WHERE project_id=? AND source=? ORDER BY id LIMIT 1",
            (project_id, source),
        ).fetchone()
        return dict(r) if r else None


def find_inventory_by_name(project_id, name):
    """First inventory in a project with an exact name — a second dedup axis so the
    infra builder can get-or-create its canonical '<project> (VMs)' inventory and
    never spawn a duplicate even if the source tag differs."""
    with _connect() as c:
        r = c.execute(
            "SELECT * FROM inventories WHERE project_id=? AND name=? ORDER BY id LIMIT 1",
            (project_id, name),
        ).fetchone()
        return dict(r) if r else None


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


def get_host(hid: int):
    """One host row (incl. its inventory_id) or None — used to resolve a host to its
    inventory's org for the delete guard."""
    with _connect() as c:
        r = c.execute("SELECT * FROM hosts WHERE id=?", (hid,)).fetchone()
        return dict(r) if r else None


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


def set_run_inventory(run_id, inventory_id):
    """Point an already-queued run at an inventory. Used by the pipeline's
    auto-inventory step to back-fill the inventory built from freshly-applied VMs
    into the Ansible/Salt steps that follow it in the same sequence."""
    with _connect() as c:
        c.execute("UPDATE runs SET inventory_id=? WHERE id=?", (inventory_id, run_id))


def get_run(run_id: int):
    with _connect() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None


def list_runs(project_id=None, limit=100, org_ids=None):
    """Runs, newest first. `org_ids` (a list) restricts to runs whose project is in
    those orgs (or legacy-unowned); None = unscoped (system admin / internal callers
    like the scheduler); empty list = nothing. Without this an operator could read
    every tenant's runs (and their extra_vars) via GET /runs."""
    with _connect() as c:
        where, params = [], []
        if project_id is not None:
            where.append("project_id=?")
            params.append(project_id)
        if org_ids is not None:
            if not org_ids:
                return []
            ph = ",".join("?" * len(org_ids))
            where.append(f"project_id IN (SELECT id FROM projects WHERE org_id IN ({ph}) OR org_id IS NULL)")
            params.extend(org_ids)
        sql = "SELECT * FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def run_log_path(run_id: int) -> Path:
    return RUNS_DIR / f"{run_id}.log"


# =========================================================
# ORGANIZATIONS / TEAMS / per-org RBAC
# =========================================================
# Org roles are the same three tiers as the global roles, ranked so the highest
# grant wins when a user has several (direct + via teams).
_ORG_RANK = {"viewer": 1, "operator": 2, "admin": 3}


def default_org_id() -> int:
    with _connect() as c:
        r = c.execute("SELECT id FROM organizations WHERE slug='default'").fetchone()
        return r["id"] if r else _seed_default_org(c)


def list_orgs() -> list:
    with _connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM organizations ORDER BY name").fetchall()]


def get_org(oid: int):
    with _connect() as c:
        r = c.execute("SELECT * FROM organizations WHERE id=?", (oid,)).fetchone()
        return dict(r) if r else None


def get_org_by_slug(slug: str):
    with _connect() as c:
        r = c.execute("SELECT * FROM organizations WHERE slug=?", (slug,)).fetchone()
        return dict(r) if r else None


def create_org(name: str, slug: str, description: str = "") -> int:
    with _connect() as c:
        c.execute("INSERT INTO organizations(name, slug, description, created) VALUES(?,?,?,?)",
                  (name, slug, description, _now()))
        return c.execute("SELECT id FROM organizations WHERE slug=?", (slug,)).fetchone()["id"]


def update_org(oid: int, name=None, description=None) -> None:
    sets, vals = [], []
    if name is not None:
        sets.append("name=?"); vals.append(name)
    if description is not None:
        sets.append("description=?"); vals.append(description)
    if not sets:
        return
    vals.append(oid)
    with _connect() as c:
        c.execute(f"UPDATE organizations SET {', '.join(sets)} WHERE id=?", vals)


def delete_org(oid: int) -> None:
    with _connect() as c:
        c.execute("DELETE FROM organizations WHERE id=?", (oid,))


# ---- membership ----
def list_org_members(org_id: int) -> list:
    with _connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT username, role FROM org_members WHERE org_id=? ORDER BY username", (org_id,)).fetchall()]


def set_org_member(org_id: int, username: str, role: str) -> None:
    with _connect() as c:
        c.execute("INSERT INTO org_members(org_id, username, role) VALUES(?,?,?) "
                  "ON CONFLICT(org_id, username) DO UPDATE SET role=excluded.role",
                  (org_id, username, role))


def remove_org_member(org_id: int, username: str) -> None:
    with _connect() as c:
        c.execute("DELETE FROM org_members WHERE org_id=? AND username=?", (org_id, username))


# ---- teams ----
def list_teams(org_id=None) -> list:
    with _connect() as c:
        if org_id is None:
            rows = c.execute("SELECT * FROM teams ORDER BY name").fetchall()
        else:
            rows = c.execute("SELECT * FROM teams WHERE org_id=? ORDER BY name", (org_id,)).fetchall()
        return [dict(r) for r in rows]


def get_team(tid: int):
    with _connect() as c:
        r = c.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None


def create_team(org_id: int, name: str, org_role: str = "viewer") -> int:
    with _connect() as c:
        c.execute("INSERT INTO teams(org_id, name, org_role, created) VALUES(?,?,?,?)",
                  (org_id, name, org_role, _now()))
        return c.execute("SELECT id FROM teams WHERE org_id=? AND name=?", (org_id, name)).fetchone()["id"]


def update_team(tid: int, name=None, org_role=None) -> None:
    sets, vals = [], []
    if name is not None:
        sets.append("name=?"); vals.append(name)
    if org_role is not None:
        sets.append("org_role=?"); vals.append(org_role)
    if not sets:
        return
    vals.append(tid)
    with _connect() as c:
        c.execute(f"UPDATE teams SET {', '.join(sets)} WHERE id=?", vals)


def delete_team(tid: int) -> None:
    with _connect() as c:
        c.execute("DELETE FROM teams WHERE id=?", (tid,))


def list_team_members(team_id: int) -> list:
    with _connect() as c:
        return [r["username"] for r in c.execute(
            "SELECT username FROM team_members WHERE team_id=? ORDER BY username", (team_id,)).fetchall()]


def add_team_member(team_id: int, username: str) -> None:
    with _connect() as c:
        c.execute("INSERT OR IGNORE INTO team_members(team_id, username) VALUES(?,?)", (team_id, username))


def remove_team_member(team_id: int, username: str) -> None:
    with _connect() as c:
        c.execute("DELETE FROM team_members WHERE team_id=? AND username=?", (team_id, username))


# ---- effective role resolution ----
def effective_org_role(username: str, org_id: int):
    """The user's highest role in an org, combining their direct grant and any
    team they belong to in that org. Returns 'admin'|'operator'|'viewer' or None
    when the user has no access to the org."""
    best = 0
    with _connect() as c:
        r = c.execute("SELECT role FROM org_members WHERE org_id=? AND username=?",
                      (org_id, username)).fetchone()
        if r:
            best = max(best, _ORG_RANK.get(r["role"], 0))
        for t in c.execute(
                "SELECT t.org_role AS role FROM teams t JOIN team_members m ON m.team_id=t.id "
                "WHERE t.org_id=? AND m.username=?", (org_id, username)).fetchall():
            best = max(best, _ORG_RANK.get(t["role"], 0))
    if best <= 0:
        return None
    return {1: "viewer", 2: "operator", 3: "admin"}[best]


def orgs_for_user(username: str) -> list:
    """Org ids the user can access (direct membership or via a team)."""
    with _connect() as c:
        rows = c.execute(
            "SELECT org_id FROM org_members WHERE username=? "
            "UNION SELECT t.org_id FROM teams t JOIN team_members m ON m.team_id=t.id "
            "WHERE m.username=?", (username, username)).fetchall()
        return [r["org_id"] for r in rows]
