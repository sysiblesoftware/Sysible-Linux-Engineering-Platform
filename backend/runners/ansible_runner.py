"""Ansible runner — turn a SLEP run row into an actual `ansible-playbook`
invocation and stream its output to the run log.

Design goals (the "friendlier than AAP" part):
  * No execution environment, no container per run — just run `ansible-playbook`
    from the project's own directory so roles/relative paths/`ansible.cfg` all
    resolve the way the author expects.
  * The inventory is RENDERED from the SLEP database into a throwaway INI file,
    so what runs always matches what the console shows.
  * Credentials never touch the browser or the project dir: the SSH key/password
    is written to a 0600 file in a per-run temp dir and removed when the run ends.

`launch(run_id)` blocks until the run finishes; the API layer calls it on a
background thread and the console tails the log file.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .. import db


def _ansible_group(name: str) -> str:
    """Ansible INI group names allow only letters, digits and underscores — a
    Controller environment like "Sysible Labs" (with a space) would otherwise
    write an invalid `[Sysible Labs]` section and the whole inventory fails to
    parse. Map every other character to '_'."""
    g = re.sub(r"[^A-Za-z0-9_]", "_", name.strip())
    if g and g[0].isdigit():
        g = "g_" + g            # groups can't start with a digit
    return g or "ungrouped"


def _render_inventory(hosts, credential, dest: Path) -> None:
    """Write an Ansible INI inventory from SLEP hosts. Hosts are grouped by their
    comma-separated `groups`; every host also lands in the implicit `all`. SSH
    connection vars come from the attached credential."""
    # group name -> list of "hostline"
    groups: dict[str, list[str]] = {}
    ungrouped: list[str] = []
    conn_user = (credential or {}).get("username") or ""

    for h in hosts:
        parts = [h["name"], f"ansible_host={h['address']}"]
        if conn_user:
            parts.append(f"ansible_user={conn_user}")
        # ssh_password credentials pass the password as a host var (server-side
        # only — never rendered into the console). Key creds use --private-key.
        if credential and credential.get("kind") == "ssh_password" and credential.get("secret"):
            parts.append(f"ansible_password={credential['secret']}")
            parts.append(f"ansible_become_password={credential['secret']}")
        for k, v in (h.get("variables") or {}).items():
            parts.append(f"{k}={json.dumps(v) if not isinstance(v, str) else v}")
        line = " ".join(parts)
        gs = [_ansible_group(g) for g in (h.get("groups") or "").split(",") if g.strip()]
        if gs:
            for g in gs:
                groups.setdefault(g, []).append(line)
        else:
            ungrouped.append(line)

    with dest.open("w") as f:
        for line in ungrouped:
            f.write(line + "\n")
        for g, lines in groups.items():
            f.write(f"\n[{g}]\n")
            for line in lines:
                f.write(line + "\n")


def _write_key(secret: str, dest: Path) -> None:
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret.encode())
        if not secret.endswith("\n"):
            os.write(fd, b"\n")
    finally:
        os.close(fd)


def launch(run_id: int) -> None:
    run = db.get_run(run_id)
    if not run:
        return
    log_path = db.run_log_path(run_id)
    project = db.get_project(run["project_id"])
    workdir = db.project_dir(run["project_id"])
    hosts = db.list_hosts(run["inventory_id"]) if run.get("inventory_id") else []
    credential = (
        db.get_credential(run["credential_id"], include_secret=True)
        if run.get("credential_id") else None
    )
    try:
        extra_vars = json.loads(run.get("extra_vars") or "{}")
    except (TypeError, ValueError):
        extra_vars = {}

    tmp = Path(tempfile.mkdtemp(prefix=f"slep-run-{run_id}-"))
    inv_file = tmp / "inventory.ini"
    key_file = tmp / "id_key"

    db.set_run_status(run_id, "running", started=int(time.time()))

    with log_path.open("w", buffering=1) as log:
        def emit(msg: str):
            log.write(msg if msg.endswith("\n") else msg + "\n")

        try:
            if not hosts:
                emit("!! No hosts in the selected inventory — nothing to target.")
                raise RuntimeError("empty inventory")

            playbook = (workdir / run["target"]).resolve()
            # Refuse to escape the project dir (the target comes from the console).
            if not str(playbook).startswith(str(workdir.resolve())):
                emit(f"!! Playbook path escapes the project directory: {run['target']}")
                raise RuntimeError("invalid playbook path")
            if not playbook.is_file():
                emit(f"!! Playbook not found: {run['target']}")
                raise RuntimeError("playbook missing")

            _render_inventory(hosts, credential, inv_file)

            cmd = ["ansible-playbook", "-i", str(inv_file), str(playbook)]
            if credential and credential.get("kind") == "ssh" and credential.get("secret"):
                _write_key(credential["secret"], key_file)
                cmd += ["--private-key", str(key_file)]
            for k, v in extra_vars.items():
                cmd += ["-e", f"{k}={v}"]

            env = dict(os.environ)
            # First-run friendliness: don't wedge on unknown host keys. Documented,
            # and overridable by shipping an ansible.cfg in the project.
            env.setdefault("ANSIBLE_HOST_KEY_CHECKING", "False")
            env.setdefault("ANSIBLE_FORCE_COLOR", "1")

            emit(f"== SLEP run #{run_id} · project '{project['name']}' ==")
            emit(f"$ {' '.join(cmd)}")
            emit(f"-- inventory: {len(hosts)} host(s); credential: "
                 f"{credential['name'] if credential else 'none'} --\n")
            log.flush()

            proc = subprocess.Popen(
                cmd, cwd=str(workdir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            for line in proc.stdout:      # live stream
                log.write(line)
                log.flush()
            rc = proc.wait()

            emit(f"\n== finished: exit code {rc} ==")
            db.set_run_status(
                run_id, "success" if rc == 0 else "failed",
                exit_code=rc, finished=int(time.time()),
            )
        except FileNotFoundError:
            emit("!! `ansible-playbook` is not installed on the SLEP host. "
                 "Install ansible (apt install ansible / pipx install ansible).")
            db.set_run_status(run_id, "failed", exit_code=127, finished=int(time.time()))
        except Exception as e:  # noqa: BLE001 — surface any failure into the log
            emit(f"\n!! run aborted: {e}")
            cur = db.get_run(run_id)
            if cur and cur.get("status") == "running":
                db.set_run_status(run_id, "failed", exit_code=1, finished=int(time.time()))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
