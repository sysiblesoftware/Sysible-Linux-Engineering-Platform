"""Per-project role and collection content — AAP's "project sync", for real.

SLEP could install a fixed list of five collections into the shared engine venv
from the Engines page, and that was the whole story: nothing per project, no
roles at all, no private Galaxy. So a playbook that depended on a role simply did
not run, and the operator's only fix was a shell on the SLEP host.

AAP's role management is project sync: on every sync it installs the project's
own `roles/requirements.yml` and `collections/requirements.yml` INTO the project,
from an ordered list of servers, with a token for private content. That is what
this does.

Into the project, not the venv, and that is the point. Two projects pinning
different versions of the same role is normal; installing both into one shared
venv means whichever synced last wins and the other project silently runs the
wrong code. `ansible-galaxy -p <project>/roles` keeps them apart, and
ansible-playbook already looks in `roles/` beside the playbook.

Nothing here takes a path from a caller. The requirements files are at fixed
names inside the project directory the allowlist chose, and the install root is
derived from it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml

# Where Ansible itself looks, so an installed role needs no configuration to be
# found: roles/ beside the playbook, and collections/ as a configured path.
ROLES_SUBDIR = "roles"
COLLECTIONS_SUBDIR = "collections"
# The conventional names. AAP reads exactly these two.
ROLE_REQS = ("roles/requirements.yml", "roles/requirements.yaml", "requirements.yml")
COLLECTION_REQS = ("collections/requirements.yml", "collections/requirements.yaml")

TIMEOUT = 900


class GalaxyError(RuntimeError):
    pass


def _first_existing(root: Path, names) -> Path | None:
    for n in names:
        p = root / n
        if p.is_file():
            return p
    return None


def requirements(root: Path) -> dict:
    """Which requirements files this project has, and what they ask for.

    Read for display, so a parse error is reported rather than raised: a project
    with a malformed requirements.yml must still open in the console, with the
    reason on screen.
    """
    out = {"roles": None, "collections": None, "errors": []}
    for key, names in (("roles", ROLE_REQS), ("collections", COLLECTION_REQS)):
        path = _first_existing(root, names)
        if not path:
            continue
        rel = str(path.relative_to(root))
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{rel}: {e}")
            continue
        out[key] = {"path": rel, "entries": _entries(data, key)}
    return out


def _entries(data, key) -> list[dict]:
    """The requirement list, in the two shapes the file is written in.

    A requirements.yml is either a bare list (the old roles format) or a mapping
    with `roles:` and `collections:` keys (the one a single file uses for both).
    """
    if isinstance(data, dict):
        data = data.get(key) or []
    if not isinstance(data, list):
        return []
    rows = []
    for item in data:
        if isinstance(item, str):
            rows.append({"name": item})
        elif isinstance(item, dict):
            rows.append({k: item[k] for k in ("name", "src", "version", "source", "type")
                         if item.get(k)})
    return rows


def installed(root: Path) -> dict:
    """What is actually present in the project right now."""
    out = {"roles": [], "collections": []}
    rdir = root / ROLES_SUBDIR
    if rdir.is_dir():
        out["roles"] = sorted(
            d.name for d in rdir.iterdir()
            if d.is_dir() and (d / "tasks").is_dir() or (d.is_dir() and (d / "meta").is_dir()))
    cdir = root / COLLECTIONS_SUBDIR / "ansible_collections"
    if cdir.is_dir():
        for ns in sorted(p for p in cdir.iterdir() if p.is_dir()):
            for coll in sorted(p for p in ns.iterdir() if p.is_dir()):
                out["collections"].append(f"{ns.name}.{coll.name}")
    return out


def sync_commands(root: Path, force: bool = False) -> list[list[str]]:
    """The argv for each install this project needs. Empty when it needs none.

    Built here rather than in the runner so it can be asserted without running
    ansible-galaxy: the -p/-r pairing is the whole correctness of this feature,
    and getting it wrong installs into the shared venv instead of the project.
    """
    galaxy = shutil.which("ansible-galaxy")
    if not galaxy:
        raise GalaxyError("ansible-galaxy is not installed — install Ansible first "
                          "(Engines → Ansible).")
    cmds: list[list[str]] = []
    rpath = _first_existing(root, ROLE_REQS)
    if rpath:
        cmd = [galaxy, "role", "install", "-r", str(rpath), "-p",
               str(root / ROLES_SUBDIR)]
        if force:
            cmd.append("--force")
        cmds.append(cmd)
    cpath = _first_existing(root, COLLECTION_REQS)
    if cpath:
        cmd = [galaxy, "collection", "install", "-r", str(cpath), "-p",
               str(root / COLLECTIONS_SUBDIR)]
        if force:
            cmd.append("--force")
        cmds.append(cmd)
    return cmds


def server_env(servers=None, token: str = "") -> dict:
    """Environment for a private Galaxy / Automation Hub.

    The token goes in the ENVIRONMENT, never argv: argv is visible in `ps` to
    any local user, which is the same reason extra-vars go through a 0600 file.
    """
    env = dict(os.environ)
    servers = [s for s in (servers or []) if str(s).strip()]
    if servers:
        names = []
        for i, url in enumerate(servers, 1):
            name = f"slep{i}"
            names.append(name)
            env[f"ANSIBLE_GALAXY_SERVER_{name.upper()}_URL"] = str(url).strip()
            if token:
                env[f"ANSIBLE_GALAXY_SERVER_{name.upper()}_TOKEN"] = token
        env["ANSIBLE_GALAXY_SERVER_LIST"] = ",".join(names)
    elif token:
        env["ANSIBLE_GALAXY_TOKEN"] = token
    return env


def sync(root: Path, emit, force: bool = False, servers=None, token: str = "") -> int:
    """Run the installs, streaming to `emit`. Returns a shell-style exit code."""
    cmds = sync_commands(root, force=force)
    if not cmds:
        emit("-- no roles/requirements.yml or collections/requirements.yml in this "
             "project; nothing to install.\n")
        return 0
    env = server_env(servers, token)
    for cmd in cmds:
        # The token is in the environment, so the command is safe to show.
        emit(f"$ {' '.join(cmd)}\n")
        try:
            proc = subprocess.Popen(cmd, cwd=str(root), env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
        except FileNotFoundError as e:
            emit(f"!! {e}\n")
            return 127
        assert proc.stdout is not None
        for line in proc.stdout:
            emit(line)
        rc = proc.wait(timeout=TIMEOUT)
        if rc != 0:
            emit(f"!! ansible-galaxy exited {rc}\n")
            return rc
    return 0
