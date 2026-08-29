"""Salt runner — apply Salt states over SSH with `salt-ssh` (agentless, so it
fits SLEP's SSH-native model; no minions to install).

Rendered per run from the SLEP database:
  * a ROSTER (which hosts, and how to reach them) from the selected inventory +
    SSH credential;
  * a minimal master CONFIG whose `file_roots` is the project dir, so the SLS
    states authored in the IDE are what salt applies.

The run's `target` is the state to apply (`state.apply <target>`); the literal
`highstate` runs `state.highstate`. Secrets (the SSH key) go to a 0600 temp file
that's removed when the run ends.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path

import yaml

from .. import db
from . import _common

# Render-time injection guards (defence-in-depth; the API validates on input too).
_SAFE_HOST = re.compile(r"^(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)$")
_SAFE_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def _render_roster(hosts, credential, key_path, dest: Path, bastion: str = "",
                   bastion_key: str = "") -> None:
    cred_user = (credential or {}).get("username") or ""
    kind = (credential or {}).get("kind") if credential else None
    # The jump hop authenticates with SLEP's managed key via an explicit ProxyCommand —
    # the SAME way the Ansible runner does it — because the bastion only trusts that key
    # ("Prepare jump host" installs it). A bare ProxyJump offers the bastion nothing
    # usable and the hop is refused (this was why Salt runs failed through a jump host).
    #
    # The ProxyCommand's forward target is the CONCRETE host address (built per host
    # below), NOT the `%h:%p` tokens: salt-ssh doesn't percent-expand those inside a
    # roster ProxyCommand the way a config-file ProxyCommand does, so the inner ssh
    # received the literal string and died with "Bad stdio forwarding specification
    # '%h:%p'". We know each target's address at render time, so substitute it directly.
    def _proxy_for(target_addr: str) -> str:
        if not (bastion and bastion_key):
            return ""
        # ssh runs this ProxyCommand via /bin/sh — shell-quote the untrusted bastion
        # (and the key path). The API charset-validates a bastion, so a real value is
        # unchanged; this is the last-line defence against a value that slipped through.
        return (f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                f"-o BatchMode=yes -o ConnectTimeout=15 -i {shlex.quote(str(bastion_key))} "
                f"-W {target_addr}:22 {shlex.quote(bastion)}")
    # Build the roster as a dict and serialise with yaml.safe_dump: it quotes/escapes
    # every value, so a host address, username, ProxyCommand, or password can never
    # inject a sibling roster key (the salt-ssh equivalent of the INI-injection RCE).
    roster: dict = {}
    for h in hosts:
        addr = str(h.get("address") or "")
        if not _SAFE_HOST.fullmatch(addr):
            continue   # skip a host whose address isn't a plain hostname/IP
        # Target the inventory's ansible_user (the cloud-init login account) FIRST, so
        # Salt logs in as the same user Ansible does; then the credential username, then
        # root. Previously Salt used only credential.username/root and diverged from
        # Ansible whenever the credential had no username.
        huser = (h.get("variables") or {}).get("ansible_user") or cred_user or "root"
        if not _SAFE_USER.fullmatch(str(huser)):
            huser = "root"
        entry: dict = {"host": addr, "user": str(huser), "host_key_checking": False}
        if credential and kind == "ssh" and key_path:
            entry["priv"] = str(key_path)
            entry["sudo"] = True   # key logins need sudo too (VMs grant NOPASSWD)
        elif credential and kind == "ssh_password" and credential.get("secret"):
            entry["passwd"] = str(credential["secret"])
            entry["sudo"] = True
        if bastion:
            proxy = _proxy_for(addr)
            # The ProxyCommand VALUE must be double-quoted inside the roster string:
            # salt-ssh emits each ssh_options entry as a raw `-o {opt}` into a shell
            # command with no quoting of its own, so an unquoted `ProxyCommand=ssh -o …`
            # is split on the first space — the outer ssh reads ProxyCommand as the bare
            # word "ssh", runs it with no destination, and dies with an ssh usage error
            # ("Connection closed by UNKNOWN port 65535"). Quoting keeps the whole nested
            # ssh together as one option value (this is the form salt's own docs show).
            entry["ssh_options"] = ([f'ProxyCommand="{proxy}"'] if proxy else [f"ProxyJump={shlex.quote(bastion)}"]) \
                + ["StrictHostKeyChecking=no"]
        roster[h["name"]] = entry
    dest.write_text(yaml.safe_dump(roster, default_flow_style=False))


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
    bastion = (db.get_inventory(run["inventory_id"]) or {}).get("bastion") or "" if run.get("inventory_id") else ""
    credential = (
        db.get_credential(run["credential_id"], include_secret=True)
        if run.get("credential_id") else None
    )
    state = (run["target"] or "").strip()
    try:
        extra_vars = json.loads(run.get("extra_vars") or "{}")
    except (TypeError, ValueError):
        extra_vars = {}

    tmp = Path(tempfile.mkdtemp(prefix=f"slep-salt-{run_id}-"))
    roster = tmp / "roster"
    key_file = tmp / "id_key"
    conf_dir = tmp / "conf"
    conf_dir.mkdir()

    db.set_run_status(run_id, "running", started=int(time.time()))
    with log_path.open("w", buffering=1) as log:
        def emit(m):
            log.write(m if m.endswith("\n") else m + "\n")

        emit(f"== SLEP run #{run_id} · project '{project['name']}' · salt-ssh {state or 'highstate'} ==")
        try:
            if not hosts:
                emit("!! No hosts in the selected inventory — nothing to target.")
                raise RuntimeError("empty inventory")

            key_path = None
            if credential and credential.get("kind") == "ssh" and credential.get("secret"):
                _write_key(credential["secret"], key_file)
                key_path = str(key_file)
            from .. import keydist
            _render_roster(hosts, credential, key_path, roster, bastion=bastion,
                           bastion_key=keydist.managed_key_path())

            # Minimal master config: states come from the project dir; keep all
            # of salt's scratch dirs inside the per-run temp so no root paths are
            # touched and nothing leaks between runs.
            #
            # ssh_log_file is critical: salt-ssh defaults it to /var/log/salt/ssh
            # (root-owned), so a non-root SLEP process dies with "No permissions to
            # access /var/log/salt/ssh". Redirect it — plus the general log_file
            # (/var/log/salt/master) — into the per-run temp so no root path is
            # touched and the logs are cleaned up with the run.
            (conf_dir / "master").write_text(
                "file_roots:\n  base:\n    - %s\n"
                "cachedir: %s\npki_dir: %s\nroster_file: %s\n"
                "log_file: %s\nssh_log_file: %s\n"
                % (workdir, tmp / "cache", tmp / "pki", roster,
                   tmp / "salt-master.log", tmp / "salt-ssh.log")
            )

            func = "state.highstate" if state.lower() == "highstate" or not state else "state.apply"
            # Target glob is a bare `*` (all roster hosts). It must NOT be quoted here:
            # we exec via subprocess with an argv list (no shell), so "'*'" would be
            # passed literally — salt-ssh then matches a glob named `'*'` against the
            # roster keys and finds nothing ("No matching targets found in roster").
            cmd = ["salt-ssh", "-c", str(conf_dir), "-i", "--no-color", "*", func]
            if func == "state.apply":
                # Salt state names are DOTTED and extension-less: the file maintain.sls
                # is the state `maintain`, states/web.sls is `states.web`. Passing the raw
                # filename (maintain.sls) makes Salt look for maintain/sls.sls → "No
                # matching sls found for 'maintain.sls'". Normalise: drop a trailing .sls
                # and turn path separators into dots.
                sls = re.sub(r"\.sls$", "", state).strip("/").replace("/", ".")
                cmd.append(sls)
            # extra_vars become salt kwargs (e.g. test=True for a dry run, or
            # pillar overrides key=value). Salt parses `k=v` trailing args itself.
            # Constrain the KEY to a plain identifier: a key beginning with '-' (or
            # carrying spaces) would otherwise be tokenised by salt-ssh as an OPTION
            # rather than a kwarg, smuggling flags into the command.
            for k, v in extra_vars.items():
                if not re.fullmatch(r"[A-Za-z0-9_]+", str(k)):
                    emit(f"-- skipped variable with an unsafe name: {k!r}")
                    continue
                cmd.append(f"{k}={v}")

            dry = str(extra_vars.get("test", "")).lower() in ("true", "1", "yes")
            emit(f"-- roster: {len(hosts)} host(s); credential: "
                 f"{credential['name'] if credential else 'none'}"
                 f"{'; test mode (dry-run)' if dry else ''} --")
            # Secret pillar/kwarg values must not be echoed into the viewer-readable log.
            rc = _common.stream(cmd, workdir, dict(os.environ), log,
                                redact=[str(v) for v in extra_vars.values() if str(v)], run_id=run_id)
            if _common.is_stopped(run_id):
                _common.clear_stop(run_id)
                emit("\n== canceled by operator ==")
                db.set_run_status(run_id, "canceled", exit_code=rc or 130, finished=int(time.time()))
                return
            emit(f"\n== finished: exit code {rc} ==")
            db.set_run_status(run_id, "success" if rc == 0 else "failed",
                              exit_code=rc, finished=int(time.time()))
        except Exception as e:  # noqa: BLE001
            emit(f"\n!! run aborted: {e}")
            cur = db.get_run(run_id)
            if cur and cur.get("status") == "running":
                db.set_run_status(run_id, "failed", exit_code=1, finished=int(time.time()))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
